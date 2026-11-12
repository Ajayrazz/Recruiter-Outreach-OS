"""Email message lifecycle: generation, linting, edit/approve/reject, validated state transitions."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recruiter_os.constants import S, TRANSITIONS
from recruiter_os.models import Campaign, Contact, EmailEvent, EmailMessage, EmailTemplate, utcnow
from recruiter_os.services import personalize
from recruiter_os.services.audit import audit
from recruiter_os.services.settings import get_profile
from recruiter_os.services.suppression import is_suppressed


class InvalidTransition(ValueError):
    pass


def transition(s: Session, m: EmailMessage, new: S, *, actor: str = "user", **fields: Any) -> None:
    cur = S(m.status)
    if new not in TRANSITIONS[cur]:
        raise InvalidTransition(f"{cur.value} -> {new.value} is not allowed")
    m.status = new.value
    for k, v in fields.items():
        setattr(m, k, v)
    audit(s, f"email_{new.name.lower()}", "email", m.id, actor=actor, from_status=cur.value)
    s.flush()


def log_event(s: Session, m: EmailMessage | None, event_type: str, source: str = "observed",
              gmail_message_id: str | None = None, contact_id: int | None = None, **detail: Any) -> bool:
    """Insert an event; returns False when it is a duplicate of an already recorded event."""
    ev = EmailEvent(message_id=m.id if m else None, contact_id=contact_id or (m.contact_id if m else None),
                    event_type=event_type, source=source, detail=detail, gmail_message_id=gmail_message_id)
    try:
        with s.begin_nested():
            s.add(ev)
    except IntegrityError:
        return False
    return True


# ------------------------------------------------------------------ quality lint
SPAM_PHRASES = ["act now", "limited time", "100% free", "guaranteed", "click here", "urgent!!!", "make money",
                "winner", "no obligation", "buy now", "risk-free"]
SHORTENERS = ("bit.ly", "tinyurl.com", "t.co/", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly")


def lint_email(subject: str, body: str, has_attachment: bool = False) -> list[str]:
    warns = []
    words = len(re.findall(r"\b\w+\b", body))
    if words > 220:
        warns.append(f"Body is {words} words; aim for roughly 120–180.")
    if words < 70:
        warns.append(f"Body is only {words} words; it may lack context.")
    low = (subject + " " + body).lower()
    warns += [f"Spam-like phrase: '{p}'" for p in SPAM_PHRASES if p in low]
    urls = re.findall(r"https?://\S+", body)
    if len(urls) > 4:
        warns.append(f"{len(urls)} links; keep to a few (LinkedIn/GitHub/portfolio).")
    if any(sh in u.lower() for u in urls for sh in SHORTENERS):
        warns.append("URL shortener detected; use full links.")
    if subject.isupper() and len(subject) > 6:
        warns.append("Subject is ALL CAPS.")
    if "!" in subject or subject.count("?") > 1:
        warns.append("Avoid exclamation marks / repeated punctuation in the subject.")
    if re.search(r"[\U0001F300-\U0001FAFF]", subject + body):
        warns.append("Emoji present; unnecessary in professional outreach.")
    if re.search(r"\{\{|\}\}|\[Name\]|\[Company\]", subject + body):
        warns.append("Unrendered placeholder detected.")
    if not subject.strip():
        warns.append("Subject is empty.")
    if len(subject) > 90:
        warns.append("Subject is long.")
    return warns


# ------------------------------------------------------------------ generation
def compose(s: Session, contact: Contact, template: EmailTemplate, role: str, mode: str,
            profile: dict[str, Any] | None = None, extra_ctx: dict[str, Any] | None = None) -> tuple[str, str]:
    profile = profile or get_profile(s)
    ctx = personalize.build_context(profile, {"name": contact.name, "title": contact.title, "company": contact.company}, role, mode)
    ctx.update(extra_ctx or {})
    return personalize.render_subject(template.subject, ctx), personalize.render(template.body, ctx)


def create_draft(s: Session, campaign: Campaign, contact: Contact, template: EmailTemplate,
                 profile: dict[str, Any] | None = None, sender_email: str = "") -> EmailMessage | None:
    """Create an initial-outreach draft. Returns None if the contact already has an active/sent initial email
    (global duplicate protection, enforced again by a DB unique index) or is suppressed."""
    if contact.opted_out or is_suppressed(s, contact.email):
        return None
    subject, body = compose(s, contact, template, campaign.role_category, campaign.mode, profile)
    m = EmailMessage(campaign_id=campaign.id, contact_id=contact.id, kind="initial", status=S.AWAITING.value,
                     to_email=contact.email, subject=subject, body_text=body, resume_id=campaign.resume_id,
                     template_id=template.id, sender_email=sender_email, idempotency_key=uuid.uuid4().hex)
    try:
        with s.begin_nested():
            s.add(m)
            s.flush()
    except IntegrityError:
        return None
    contact.campaign_status = "Drafted"
    audit(s, "draft_created", "email", m.id, campaign=campaign.id)
    return m


def regenerate(s: Session, m: EmailMessage) -> None:
    if m.status not in (S.DRAFT.value, S.AWAITING.value):
        raise InvalidTransition("Only drafts/awaiting emails can be regenerated")
    camp, tpl = s.get(Campaign, m.campaign_id), s.get(EmailTemplate, m.template_id)
    extra = {}
    if m.kind == "followup" and m.parent_message_id:
        extra["original_subject"] = s.get(EmailMessage, m.parent_message_id).subject
    m.subject, m.body_text = compose(s, m.contact, tpl, camp.role_category, camp.mode, extra_ctx=extra)
    m.status = S.AWAITING.value
    s.flush()


def edit_message(s: Session, m: EmailMessage, subject: str, body: str, *, save_as_draft: bool = True) -> None:
    """Editing always revokes approval: edited content must be re-approved."""
    if m.status in (S.SENDING.value, S.SUBMITTED.value, S.BOUNCED.value, S.REPLIED.value, S.UNKNOWN.value):
        raise InvalidTransition("Sent emails cannot be edited")
    if not subject.strip() or not body.strip():
        raise ValueError("Subject and body are required")
    m.subject, m.body_text = subject.strip(), body.strip() + "\n"
    m.approved_at = None
    if m.status in (S.APPROVED.value, S.SCHEDULED.value):
        m.status, m.scheduled_at = (S.DRAFT if save_as_draft else S.AWAITING).value, None
    elif m.status in (S.AWAITING.value, S.DRAFT.value):
        m.status = (S.DRAFT if save_as_draft else S.AWAITING).value
    audit(s, "email_edited", "email", m.id)
    s.flush()


def approve(s: Session, m: EmailMessage) -> None:
    if m.contact.opted_out or is_suppressed(s, m.to_email):
        transition(s, m, S.SUPPRESSED)
        raise InvalidTransition("Recipient is suppressed; email suppressed.")
    if m.contact.verification_status in ("Invalid", "Suppressed"):
        raise InvalidTransition("Contact failed verification and cannot be approved.")
    transition(s, m, S.APPROVED, approved_at=utcnow())


def reject(s: Session, m: EmailMessage) -> None:
    transition(s, m, S.REJECTED, approved_at=None)
    if m.kind == "initial":
        m.contact.campaign_status = "Not Contacted"


def cancel(s: Session, m: EmailMessage) -> None:
    transition(s, m, S.CANCELLED, scheduled_at=None)
    if m.kind == "initial":
        m.contact.campaign_status = "Not Contacted"
