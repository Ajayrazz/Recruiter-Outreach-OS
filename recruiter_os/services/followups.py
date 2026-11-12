"""Follow-up eligibility and drafting. Every follow-up needs manual approval; none is ever auto-sent."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recruiter_os.constants import S
from recruiter_os.models import EmailMessage, EmailTemplate, FollowUp, utcnow
from recruiter_os.services import messages as msgs
from recruiter_os.services.audit import audit
from recruiter_os.services.settings import all_settings
from recruiter_os.services.suppression import is_suppressed


def is_followup_eligible(s: Session, m: EmailMessage, cfg: dict, now: datetime) -> tuple[bool, str]:
    """Pure-ish rule check. Returns (eligible, reason)."""
    if m.status != S.SUBMITTED.value:
        return False, f"status is {m.status}"
    c = m.contact
    if c.opted_out or is_suppressed(s, c.email):
        return False, "opted out / suppressed"
    if c.reply_status and c.reply_status != "Out of Office":
        return False, "recipient has replied"
    sent_count = s.query(EmailMessage).filter(EmailMessage.contact_id == c.id, EmailMessage.kind == "followup",
                                              EmailMessage.status.notin_([S.CANCELLED.value, S.REJECTED.value])).count()
    if sent_count >= int(cfg["max_followups"]):
        return False, "follow-up limit reached"
    if not m.submitted_at or now < m.submitted_at + timedelta(days=int(cfg["followup_wait_days"])):
        return False, "waiting period not elapsed"
    return True, ""


def refresh_followups(s: Session, now: datetime | None = None) -> int:
    """Create 'Due' follow-up reminders for eligible sent emails (latest message in each chain only)."""
    now = now or utcnow()
    cfg = all_settings(s)
    created = 0
    latest: dict[int, EmailMessage] = {}
    for m in s.query(EmailMessage).filter(EmailMessage.status == S.SUBMITTED.value, EmailMessage.kind.in_(["initial", "followup"])).order_by(EmailMessage.id):
        latest[m.contact_id] = m
    for m in latest.values():
        ok, _ = is_followup_eligible(s, m, cfg, now)
        if not ok:
            continue
        number = (m.followup_number or 0) + 1
        if s.query(FollowUp.id).filter_by(original_message_id=m.id, number=number).first():
            continue
        s.add(FollowUp(original_message_id=m.id, contact_id=m.contact_id, number=number,
                       due_at=m.submitted_at + timedelta(days=int(cfg["followup_wait_days"]))))
        m.contact.followup_status = "Due"
        created += 1
    s.flush()
    return created


def cleanup_stale(s: Session) -> int:
    """Dismiss reminders that are no longer valid (replied / bounced / opted out)."""
    n = 0
    for f in s.query(FollowUp).filter_by(status="Due").all():
        m = s.get(EmailMessage, f.original_message_id)
        c = m.contact
        if m.status != S.SUBMITTED.value or c.opted_out or (c.reply_status and c.reply_status != "Out of Office"):
            f.status = "Dismissed"
            c.followup_status = ""
            n += 1
    s.flush()
    return n


def draft_followup(s: Session, followup_id: int, template_id: int | None = None) -> EmailMessage:
    f = s.get(FollowUp, followup_id)
    if f.status != "Due":
        raise ValueError("Follow-up is not due")
    orig = s.get(EmailMessage, f.original_message_id)
    ok, why = is_followup_eligible(s, orig, all_settings(s), utcnow() + timedelta(days=3650))
    if not ok:
        raise ValueError(f"No longer eligible: {why}")
    tpl = (s.get(EmailTemplate, template_id) if template_id else
           s.query(EmailTemplate).filter_by(is_followup=True).first())
    if not tpl:
        raise ValueError("No follow-up template available")
    camp = orig.campaign
    subject, body = msgs.compose(s, orig.contact, tpl, camp.role_category, camp.mode, extra_ctx={"original_subject": orig.subject.removeprefix("Re: ")})
    m = EmailMessage(campaign_id=orig.campaign_id, contact_id=orig.contact_id, kind="followup", followup_number=f.number,
                     status=S.AWAITING.value, to_email=orig.to_email, subject=subject, body_text=body, resume_id=None,
                     template_id=tpl.id, sender_email=orig.sender_email, thread_id=orig.thread_id, parent_message_id=orig.id,
                     idempotency_key=uuid.uuid4().hex)
    try:
        with s.begin_nested():
            s.add(m)
            s.flush()
    except IntegrityError as exc:
        raise ValueError("A follow-up with this number already exists") from exc
    f.status, f.draft_message_id = "Drafted", m.id
    orig.contact.followup_status = "Drafted"
    audit(s, "followup_drafted", "email", m.id, parent=orig.id)
    return m


def dismiss(s: Session, followup_id: int) -> None:
    f = s.get(FollowUp, followup_id)
    f.status = "Dismissed"
    s.get(EmailMessage, f.original_message_id).contact.followup_status = ""
    s.flush()
