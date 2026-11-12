"""Suppression list: hard bounces, opt-outs, manual blocks. Always checked before send."""
from __future__ import annotations

from sqlalchemy.orm import Session

from recruiter_os.constants import PENDING_STATES, S, TRANSITIONS
from recruiter_os.models import Contact, EmailMessage, Suppression
from recruiter_os.services.audit import audit
from recruiter_os.services.emailnorm import normalize_email


def is_suppressed(s: Session, email: str) -> bool:
    norm = normalize_email(email)
    return bool(norm and s.query(Suppression.id).filter_by(email_normalized=norm).first())


def suppress(s: Session, email: str, reason: str, note: str = "", actor: str = "user") -> Suppression | None:
    """Add to suppression list and pull every pending email to this recipient."""
    norm = normalize_email(email)
    if not norm:
        return None
    row = s.query(Suppression).filter_by(email_normalized=norm).first()
    if not row:
        row = Suppression(email_normalized=norm, reason=reason, note=note)
        s.add(row)
    contact = s.query(Contact).filter_by(email_normalized=norm).first()
    if contact:
        contact.verification_status = "Suppressed"
        if reason == "opt_out":
            contact.opted_out = True
        pend = s.query(EmailMessage).filter(EmailMessage.contact_id == contact.id,
                                            EmailMessage.status.in_([p.value for p in PENDING_STATES])).all()
        for m in pend:
            if m.status != S.SENDING and S.SUPPRESSED in TRANSITIONS[S(m.status)]:
                m.status = S.SUPPRESSED.value
    audit(s, "suppress", "contact", contact.id if contact else norm, actor=actor, reason=reason)
    s.flush()
    return row


def unsuppress(s: Session, email: str) -> None:
    norm = normalize_email(email)
    row = s.query(Suppression).filter_by(email_normalized=norm).first()
    if row and row.reason != "opt_out":  # opt-outs are permanent
        s.delete(row)
        c = s.query(Contact).filter_by(email_normalized=norm).first()
        if c:
            c.verification_status = "Syntax Valid"
        audit(s, "unsuppress", "contact", norm)
        s.flush()
    elif row:
        raise ValueError("Opt-outs cannot be removed: honoring recipient requests is permanent.")
