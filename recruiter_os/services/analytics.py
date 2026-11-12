"""Dashboard metrics computed from stored data only. Unavailable metrics are labeled, never invented."""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from recruiter_os.constants import S
from recruiter_os.models import Campaign, Contact, EmailEvent, EmailMessage, FollowUp, Suppression, utcnow

UNAVAILABLE = {
    "Open rate": "Not available: tracking pixels are intentionally not used.",
    "Inbox / Primary placement": "Not available: Gmail's API does not reveal the recipient's tab or spam folder.",
    "Spam complaints": "Not available: Gmail does not expose complaint/feedback-loop data for personal accounts.",
    "Sender reputation": "Not available: Google Postmaster Tools requires a custom domain.",
}


def kpis(s: Session) -> dict[str, int]:
    cnt = lambda st: s.query(EmailMessage).filter(EmailMessage.status.in_([x.value for x in st])).count()  # noqa: E731
    return {
        "Total contacts": s.query(Contact).count(),
        "Reviewed contacts": s.query(Contact).filter(Contact.verification_status != "Unreviewed").count(),
        "Eligible contacts": s.query(Contact).filter(Contact.verification_status == "Ready for Outreach", Contact.opted_out.is_(False)).count(),
        "Draft emails": cnt([S.DRAFT, S.AWAITING]),
        "Approved emails": cnt([S.APPROVED]),
        "Scheduled emails": cnt([S.SCHEDULED]),
        "Submitted to Gmail": s.query(EmailMessage).filter(EmailMessage.submitted_at.isnot(None)).count(),
        "Detected bounces": cnt([S.BOUNCED]),
        "Replies received": s.query(EmailMessage).filter(EmailMessage.reply_category != "", EmailMessage.reply_category.isnot(None),
                                                         EmailMessage.status == S.REPLIED.value).count(),
        "Follow-ups due": s.query(FollowUp).filter_by(status="Due").count(),
        "Suppressed contacts": s.query(Suppression).count(),
    }


def _daily(s: Session, event_types: list[str], days: int = 60) -> pd.DataFrame:
    since = utcnow() - timedelta(days=days)
    rows = s.query(EmailEvent.occurred_at, EmailEvent.event_type).filter(EmailEvent.event_type.in_(event_types), EmailEvent.occurred_at >= since).all()
    if not rows:
        return pd.DataFrame(columns=["date", "event_type", "count"])
    df = pd.DataFrame(rows, columns=["at", "event_type"])
    df["date"] = df["at"].dt.date
    return df.groupby(["date", "event_type"]).size().reset_index(name="count")


def outreach_over_time(s: Session) -> pd.DataFrame:
    return _daily(s, ["submitted", "submitted_recovered"])


def replies_over_time(s: Session) -> pd.DataFrame:
    return _daily(s, ["reply"])


def bounce_trend(s: Session) -> pd.DataFrame:
    return _daily(s, ["bounce_hard", "bounce_soft", "bounce_policy"])


def contact_status_counts(s: Session) -> pd.DataFrame:
    return pd.DataFrame(s.query(Contact.verification_status, func.count()).group_by(Contact.verification_status).all(), columns=["status", "count"])


def company_outreach(s: Session, top: int = 15) -> pd.DataFrame:
    rows = (s.query(Contact.company, func.count(EmailMessage.id)).join(EmailMessage, EmailMessage.contact_id == Contact.id)
            .filter(EmailMessage.submitted_at.isnot(None)).group_by(Contact.company).order_by(func.count(EmailMessage.id).desc()).limit(top).all())
    return pd.DataFrame(rows, columns=["company", "emails"])


def campaign_comparison(s: Session) -> pd.DataFrame:
    out = []
    for c in s.query(Campaign).all():
        base = s.query(EmailMessage).filter(EmailMessage.campaign_id == c.id, EmailMessage.kind == "initial")
        sub = base.filter(EmailMessage.submitted_at.isnot(None)).count()
        rep = base.filter(EmailMessage.status == S.REPLIED.value).count()
        bnc = base.filter(EmailMessage.status == S.BOUNCED.value).count()
        out.append({"campaign": c.name, "status": c.status, "submitted": sub, "replies": rep, "bounces": bnc,
                    "reply_rate_%": round(100 * rep / sub, 1) if sub else None,
                    "bounce_rate_%": round(100 * bnc / sub, 1) if sub else None})
    return pd.DataFrame(out)


def deliverability_summary(s: Session) -> dict:
    sub = s.query(EmailMessage).filter(EmailMessage.submitted_at.isnot(None)).count()
    hard = s.query(EmailMessage).filter(EmailMessage.bounce_type == "hard").count()
    policy = s.query(EmailMessage).filter(EmailMessage.bounce_type == "policy").count()
    soft = s.query(EmailMessage).filter(EmailMessage.bounce_type == "soft").count()
    failed = s.query(EmailMessage).filter(EmailMessage.status == S.FAILED.value).count()
    unknown = s.query(EmailMessage).filter(EmailMessage.status == S.UNKNOWN.value).count()
    return {"submitted": sub, "hard_bounces": hard, "policy_rejections": policy, "soft_bounces": soft, "api_failures": failed,
            "delivery_unknown": unknown, "hard_bounce_rate": (hard / sub) if sub else None}
