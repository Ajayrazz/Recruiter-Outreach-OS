"""Contact import, validation, de-duplication, segmentation and review workflow."""
from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from sqlalchemy.orm import Session

from recruiter_os.constants import PENDING_STATES, SENT_STATES, S
from recruiter_os.models import Contact, EmailMessage, ImportBatch
from recruiter_os.services.audit import audit
from recruiter_os.services.emailnorm import normalize_email, validate_syntax
from recruiter_os.services.suppression import is_suppressed

FIELD_SYNONYMS = {
    "name": ["name", "full name", "fullname", "recruiter", "recruiter name", "contact", "contact name", "first name"],
    "email": ["email", "e-mail", "email address", "mail", "email id", "work email"],
    "title": ["title", "designation", "job title", "position", "role"],
    "company": ["company", "organization", "organisation", "employer", "company name"],
}
REQUIRED = ("email",)


class ImportError_(ValueError):
    pass


def read_table(source: str | Path | BinaryIO, filename: str | None = None) -> pd.DataFrame:
    """Read CSV/XLSX into an all-string DataFrame (nothing silently coerced)."""
    name = (filename or (str(source) if isinstance(source, (str, Path)) else "")).lower()
    try:
        if name.endswith((".xlsx", ".xlsm")):
            df = pd.read_excel(source, dtype=str, keep_default_na=False, engine="openpyxl")
        elif name.endswith((".csv", ".txt")):
            if hasattr(source, "read"):
                raw = source.read()
                raw = raw.encode() if isinstance(raw, str) else raw
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
            else:
                df = pd.read_csv(source, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        else:
            raise ImportError_("Unsupported file type. Use .csv or .xlsx")
    except ImportError_:
        raise
    except Exception as exc:
        raise ImportError_(f"Could not read file: {exc}") from exc
    df.columns = [str(c).strip() for c in df.columns]
    return df


def auto_map_columns(columns: list[str]) -> dict[str, str | None]:
    lowered = {c.lower().strip(): c for c in columns}
    mapping: dict[str, str | None] = {}
    for fld, syns in FIELD_SYNONYMS.items():
        mapping[fld] = next((lowered[s] for s in syns if s in lowered), None)
    return mapping


def clean_company(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").strip()).strip(" ,;")


def clean_text(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").strip())


@dataclass
class ImportResult:
    batch_id: int | None = None
    total_rows: int = 0
    imported: int = 0
    rejected: list[dict] = field(default_factory=list)
    missing_name: int = 0
    missing_company: int = 0
    missing_title: int = 0
    already_in_db: int = 0
    duplicates_in_file: int = 0
    invalid_syntax: int = 0
    suppressed: int = 0

    def summary(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("rejected",)} | {"rejected_count": len(self.rejected)}


def import_contacts(s: Session, df: pd.DataFrame, mapping: dict[str, str | None], filename: str,
                    source_label: str | None = None) -> ImportResult:
    if not mapping.get("email") or mapping["email"] not in df.columns:
        raise ImportError_("An email column must be mapped.")
    res = ImportResult(total_rows=len(df))
    batch = ImportBatch(filename=filename)
    s.add(batch)
    s.flush()
    res.batch_id = batch.id
    seen: set[str] = set()
    existing = {r[0] for r in s.query(Contact.email_normalized).all()}
    get = lambda row, f: (row[mapping[f]] if mapping.get(f) and mapping[f] in df.columns else "")  # noqa: E731

    for idx, row in df.iterrows():
        original = {str(k): str(v) for k, v in row.to_dict().items()}
        cleaned, err = validate_syntax(get(row, "email"))
        rej = lambda reason: res.rejected.append({"row": int(idx) + 2, "reason": reason, **original})  # noqa: E731
        if err:
            res.invalid_syntax += 1
            rej(err)
            continue
        norm = normalize_email(cleaned) or cleaned.lower()
        if norm in seen:
            res.duplicates_in_file += 1
            rej("duplicate within file")
            continue
        seen.add(norm)
        if norm in existing:
            res.already_in_db += 1
            rej("already in database")
            continue
        name, title, company = clean_text(get(row, "name")), clean_text(get(row, "title")), clean_company(get(row, "company"))
        res.missing_name += not name
        res.missing_company += not company
        res.missing_title += not title
        suppressed = is_suppressed(s, norm)
        res.suppressed += suppressed
        mapped_cols = {c for c in mapping.values() if c}
        s.add(Contact(
            name=name, email=cleaned, email_normalized=norm, title=title, company=company,
            source=source_label or filename, original_record=original, import_batch_id=batch.id,
            extra={k: v for k, v in original.items() if k not in mapped_cols and v},
            verification_status="Suppressed" if suppressed else "Syntax Valid"))
        res.imported += 1
    batch.summary = res.summary()
    batch.rejected = res.rejected
    audit(s, "import_contacts", "import_batch", batch.id, filename=filename, **res.summary())
    s.flush()
    return res


def rejected_to_csv(rejected: list[dict]) -> bytes:
    return pd.DataFrame(rejected).to_csv(index=False).encode()


# ---------- segmentation ----------
SEGMENTS = {
    "Talent Acquisition / Recruiting": r"talent|recruit|sourcing|hiring|acquisition",
    "HR Leadership (Head/VP/Director)": r"head|vice president|\bvp\b|director|chief|chro|cpo|\bsvp\b|\bavp\b",
    "HR Generalist / Manager": r"manager|business partner|hrbp|generalist|executive|associate|lead",
    "Founder / CEO": r"founder|\bceo\b|owner|co-founder|managing",
}


def segment_of(title: str) -> str:
    t = (title or "").lower()
    for name, pat in SEGMENTS.items():
        if re.search(pat, t):
            return name
    return "Other"


def contacts_frame(s: Session) -> pd.DataFrame:
    rows = s.query(Contact).all()
    df = pd.DataFrame([{
        "id": c.id, "name": c.name, "email": c.email, "title": c.title, "company": c.company,
        "verification": c.verification_status, "relevance": c.relevance_status,
        "campaign_status": c.campaign_status, "reply_status": c.reply_status,
        "followup_status": c.followup_status, "opted_out": c.opted_out, "source": c.source,
        "last_contacted": c.last_contacted_at, "last_subject": c.last_email_subject, "notes": c.notes,
    } for c in rows])
    if df.empty:
        return pd.DataFrame(columns=["id", "name", "email", "title", "company", "verification", "relevance",
                                     "campaign_status", "reply_status", "followup_status", "opted_out", "source",
                                     "last_contacted", "last_subject", "notes", "segment"])
    df["segment"] = df["title"].map(segment_of)
    return df


# ---------- review workflow ----------
def check_domain(domain: str, timeout: float = 4.0) -> bool | None:
    """True = domain has MX/A records; False = definitively no such domain; None = inconclusive."""
    import dns.exception
    import dns.resolver
    res = dns.resolver.Resolver()
    res.lifetime = timeout
    try:
        try:
            res.resolve(domain, "MX")
            return True
        except dns.resolver.NoAnswer:
            res.resolve(domain, "A")
            return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except (dns.exception.Timeout, dns.resolver.NoNameservers, Exception):  # noqa: BLE001
        return None


def run_domain_checks(s: Session, contact_ids: list[int] | None = None, checker=check_domain, workers: int = 16) -> dict:
    q = s.query(Contact).filter(Contact.verification_status.in_(["Unreviewed", "Syntax Valid"]))
    if contact_ids is not None:
        q = q.filter(Contact.id.in_(contact_ids))
    contacts = q.all()
    domains = sorted({c.email_normalized.split("@")[1] for c in contacts})
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = dict(zip(domains, ex.map(checker, domains)))
    out = {"checked": len(domains), "ok": 0, "bad": 0, "inconclusive": 0}
    for c in contacts:
        r = results[c.email_normalized.split("@")[1]]
        if r is True:
            c.verification_status = "Domain Checked"
            out["ok"] += 1
        elif r is False:
            c.verification_status = "Invalid"
            out["bad"] += 1
        else:
            out["inconclusive"] += 1
    audit(s, "domain_check", "contacts", "bulk", **out)
    s.flush()
    return out


def bulk_update(s: Session, ids: list[int], *, relevance: str | None = None, verification: str | None = None,
                notes: str | None = None) -> int:
    n = 0
    for c in s.query(Contact).filter(Contact.id.in_(ids)).all():
        if verification == "Ready for Outreach":
            if c.opted_out or c.verification_status in ("Invalid", "Suppressed") or is_suppressed(s, c.email):
                continue
        if relevance:
            c.relevance_status = relevance
        if verification:
            c.verification_status = verification
        if notes is not None:
            c.notes = notes
        n += 1
    audit(s, "bulk_update_contacts", "contacts", "bulk", count=n, relevance=relevance, verification=verification)
    s.flush()
    return n


def has_outreach_history(s: Session, contact_id: int) -> bool:
    return s.query(EmailMessage.id).filter(
        EmailMessage.contact_id == contact_id, EmailMessage.kind == "initial",
        EmailMessage.status.in_([x.value for x in SENT_STATES | PENDING_STATES | {S.FAILED, S.SUPPRESSED}])).first() is not None


def eligible_contacts(s: Session, exclude_previously_contacted: bool = True) -> list[Contact]:
    """Contacts that may be put in a campaign: reviewed, ready, not suppressed/opted-out, not unrelated."""
    out = []
    for c in s.query(Contact).filter(Contact.verification_status == "Ready for Outreach",
                                     Contact.opted_out.is_(False), Contact.relevance_status != "Not Relevant").all():
        if is_suppressed(s, c.email):
            continue
        if exclude_previously_contacted and has_outreach_history(s, c.id):
            continue
        out.append(c)
    return out
