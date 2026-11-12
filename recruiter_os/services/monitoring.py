"""Bounce parsing, reply detection/classification. Only *observed* Gmail data is recorded.

Limitations (see README): Gmail provides no open/read/Primary-tab information via API; a message
accepted by Gmail with no bounce is reported as "no bounce observed", never as "delivered to inbox".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Any

from sqlalchemy.orm import Session

from recruiter_os.constants import S
from recruiter_os.db import Database
from recruiter_os.models import Campaign, Contact, EmailMessage, GmailAccount, utcnow
from recruiter_os.services import messages as msgs
from recruiter_os.services.audit import audit
from recruiter_os.services.emailnorm import normalize_email
from recruiter_os.services.gmail import GmailTransport, header, message_text
from recruiter_os.services.suppression import suppress

log = logging.getLogger("recruiter_os.monitoring")


# ------------------------------------------------------------------ bounce parsing
@dataclass
class Bounce:
    recipient: str | None
    status_code: str | None
    kind: str  # hard | soft | policy
    reason: str


_RCPT_PATTERNS = [
    r"X-Failed-Recipients:\s*<?([^\s<>,;]+@[^\s<>,;]+)>?",
    r"Final-Recipient:\s*rfc822;\s*<?([^\s<>;]+@[^\s<>;]+)>?",
    r"Original-Recipient:\s*rfc822;\s*<?([^\s<>;]+@[^\s<>;]+)>?",
    r"(?:wasn't|was not|couldn't be|could not be) delivered to\s+<?([^\s<>]+@[^\s<>]+)>?(?:\s|$)",
    r"delivery to the following recipient[s]? failed[^@]{0,80}?<?([^\s<>]+@[^\s<>]+)>?(?:\s|$)",
]
_HARD_TEXT = re.compile(r"address (?:not found|couldn'?t be found)|user unknown|no such user|does not exist|unknown recipient|"
                        r"mailbox (?:not found|unavailable)|recipient address rejected", re.I)


def parse_bounce(text: str) -> Bounce | None:
    """Parse a delivery-status notification. Returns None if it doesn't look like a bounce."""
    if not re.search(r"delivery status notification|undeliverable|delivery has failed|wasn't delivered|couldn't be delivered|"
                     r"mail delivery|returned mail|failure notice|Final-Recipient", text, re.I):
        return None
    rcpt = None
    for p in _RCPT_PATTERNS:
        m = re.search(p, text, re.I)
        if m:
            rcpt = m.group(1).rstrip(".,;:>)")
            break
    code_m = re.search(r"Status:\s*([245]\.\d{1,3}\.\d{1,3})", text) or re.search(r"\b([245]\.\d{1,3}\.\d{1,3})\b", text)
    code = code_m.group(1) if code_m else None
    kind = "soft"
    if code:
        if code.startswith("5.1") or code == "5.2.1" or code.startswith("5.5.0") or (code.startswith("5.") and _HARD_TEXT.search(text)):
            kind = "hard"
        elif code.startswith("5.7"):
            kind = "policy"
        elif code.startswith("5.") and code not in ("5.2.2", ) and not code.startswith("5.4"):
            kind = "hard"
    elif _HARD_TEXT.search(text):
        kind = "hard"
    return Bounce(rcpt.lower() if rcpt else None, code, kind, (re.search(r"(?m)^.*(?:550|5\.\d\.\d|couldn't be found).*$", text) or [""])[0][:300])


# ------------------------------------------------------------------ reply classification (keyword heuristics)
OPT_OUT = re.compile(r"\b(unsubscribe|remove me|do not (?:contact|email|write)|don'?t (?:contact|email|write)|stop (?:emailing|writing|contacting)|"
                     r"take me off|no further (?:emails|contact))\b", re.I)
RULES: list[tuple[str, re.Pattern]] = [
    ("Out of Office", re.compile(r"out of (?:the )?office|automatic reply|auto[- ]?reply|on (?:annual )?leave|away from (?:the )?office", re.I)),
    ("Rejection", re.compile(r"unfortunately|not (?:able|in a position) to|we (?:will not|won'?t) be (?:moving|proceeding)|regret to inform|no longer (?:hiring|considering)", re.I)),
    ("Not Interested", re.compile(r"not interested|no openings?|no (?:current )?(?:vacanc|opportunit|internship)|not hiring|we are not (?:currently )?hiring|hiring freeze", re.I)),
    ("Request for Resume", re.compile(r"(?:share|send|attach|forward)[^.\n]{0,40}(?:resume|cv)\b|(?:resume|cv)[^.\n]{0,30}(?:attach|share|send)", re.I)),
    ("Opportunity Shared", re.compile(r"(?:apply|application|openings?|job description|jd|careers? (?:page|portal)|portal|vacanc|link)[^.\n]{0,60}(?:https?://|below|following)|https?://\S*(?:career|jobs|apply)", re.I)),
    ("Request for More Information", re.compile(r"(?:could|can|please) you (?:share|tell|provide|let me know|elaborate)|more (?:details|information)|\?\s", re.I)),
    ("Positive Response", re.compile(r"\b(thank(?:s| you)|interested|happy to|will (?:forward|share|connect)|shortlist|schedule|call|interview|looks? good|great profile)\b", re.I)),
]


def strip_quoted(text: str) -> str:
    out = []
    for line in text.splitlines():
        if re.match(r"\s*On .{5,120}wrote:\s*$", line) or line.strip().startswith(">") or re.match(r"-{2,}\s*Original Message", line, re.I):
            if line.strip().startswith(">"):
                continue
            break
        out.append(line)
    return "\n".join(out).strip()


def classify_reply(subject: str, body: str, headers: dict[str, str] | None = None) -> str:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if headers.get("auto-submitted", "no").lower() != "no" or "autoreply" in headers.get("x-autoreply", "").lower():
        return "Out of Office"
    text = f"{subject}\n{strip_quoted(body)}"
    for cat, pat in RULES:
        if pat.search(text):
            return cat
    return "Unclassified"


# ------------------------------------------------------------------ sync
class MonitoringService:
    def __init__(self, db: Database, transport: GmailTransport):
        self.db, self.tr = db, transport

    def sync(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        out = {"bounces": 0, "replies": 0, "error": ""}
        with self.db.scope() as s:
            acct = s.query(GmailAccount).filter_by(status="connected").first()
            if not acct:
                out["error"] = "Gmail not connected"
                return out
            since = (acct.last_sync_at or now - timedelta(days=30)) - timedelta(days=1)
            me = acct.email.lower()
            try:
                out["bounces"] = self._sync_bounces(s, since)
                out["replies"] = self._sync_replies(s, since, me)
            except Exception as exc:  # noqa: BLE001 - never silently swallow: persisted + logged
                acct.last_error = f"Sync failed: {type(exc).__name__}: {exc}"[:300]
                out["error"] = acct.last_error
                log.error("monitoring sync failed", exc_info=True)
                return out
            acct.last_sync_at, acct.last_error = now, ""
        return out

    def _sync_bounces(self, s: Session, since: datetime) -> int:
        import calendar
        ts = calendar.timegm(since.timetuple())
        ids = self.tr.list_ids(f"(from:mailer-daemon OR from:postmaster) after:{ts}", max_results=200)
        n = 0
        for ref in ids:
            msg = self.tr.get_message(ref["id"], "full")
            b = parse_bounce(f"{header(msg, 'Subject')}\n{header(msg, 'X-Failed-Recipients')}\n{message_text(msg)}")
            if not b:
                continue
            rcpt_header = header(msg, "X-Failed-Recipients")
            rcpt = normalize_email(b.recipient or rcpt_header or "")
            target = None
            if msg.get("threadId"):
                target = s.query(EmailMessage).filter(EmailMessage.thread_id == msg["threadId"], EmailMessage.status.in_(
                    [S.SUBMITTED.value, S.UNKNOWN.value, S.REPLIED.value])).order_by(EmailMessage.id.desc()).first()
            if not target and rcpt:
                contact = s.query(Contact).filter_by(email_normalized=rcpt).first()
                if contact:
                    target = s.query(EmailMessage).filter(EmailMessage.contact_id == contact.id, EmailMessage.status.in_(
                        [S.SUBMITTED.value, S.UNKNOWN.value, S.REPLIED.value])).order_by(EmailMessage.id.desc()).first()
            if not target:
                continue
            if not msgs.log_event(s, target, "bounce_" + b.kind, gmail_message_id=ref["id"], code=b.status_code, reason=b.reason):
                continue  # already processed
            n += 1
            target.bounce_type = b.kind
            if b.kind in ("hard", "policy") and S(target.status) in (S.SUBMITTED, S.UNKNOWN, S.REPLIED):
                msgs.transition(s, target, S.BOUNCED, actor="monitor", last_error=(b.reason or b.status_code or "bounce")[:300])
            if b.kind == "hard":
                suppress(s, target.to_email, "hard_bounce", note=b.status_code or "", actor="monitor")
            elif b.kind == "policy":
                target.last_error = f"Provider policy rejection {b.status_code}: review content/sending rate"
        return n

    def _sync_replies(self, s: Session, since: datetime, me: str) -> int:
        import calendar
        ts = calendar.timegm(since.timetuple())
        refs = self.tr.list_ids(f"in:anywhere -from:me -from:mailer-daemon -from:postmaster after:{ts}", max_results=300)
        if not refs:
            return 0
        by_thread: dict[str, EmailMessage] = {}
        for m in s.query(EmailMessage).filter(EmailMessage.thread_id.isnot(None), EmailMessage.status.in_(
                [S.SUBMITTED.value, S.UNKNOWN.value, S.REPLIED.value])).order_by(EmailMessage.id).all():
            by_thread[m.thread_id] = m  # latest message in a thread wins
        n = 0
        for ref in refs:
            orig = by_thread.get(ref.get("threadId", ""))
            if not orig:
                continue
            msg = self.tr.get_message(ref["id"], "full")
            sender = parseaddr(header(msg, "From"))[1].lower()
            if sender == me:
                continue
            body = message_text(msg)
            hdrs = {"auto-submitted": header(msg, "Auto-Submitted") or "no", "x-autoreply": header(msg, "X-Autoreply")}
            cat = classify_reply(header(msg, "Subject"), body, hdrs)
            if not msgs.log_event(s, orig, "reply", gmail_message_id=ref["id"], category=cat, from_addr=sender,
                                  snippet=strip_quoted(body)[:400]):
                continue
            n += 1
            contact = orig.contact
            if cat != "Out of Office":  # an auto-reply is not a human reply
                if S(orig.status) in (S.SUBMITTED, S.UNKNOWN):
                    msgs.transition(s, orig, S.REPLIED, actor="monitor", reply_category=cat, replied_at=utcnow())
                contact.reply_status = cat
                if OPT_OUT.search(strip_quoted(body)):
                    suppress(s, contact.email, "opt_out", note="detected in reply", actor="monitor")
            else:
                orig.reply_category = orig.reply_category or cat
                contact.reply_status = contact.reply_status or cat
        return n
