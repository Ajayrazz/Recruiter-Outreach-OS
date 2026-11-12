"""Campaign creation, contact selection, generation, activation and controls."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from recruiter_os.constants import S
from recruiter_os.models import Campaign, CampaignContact, Contact, EmailMessage, EmailTemplate, utcnow
from recruiter_os.services import messages as msgs
from recruiter_os.services.audit import audit
from recruiter_os.services.contacts import eligible_contacts
from recruiter_os.services.scheduling import plan_slots
from recruiter_os.services.settings import all_settings, get_profile

CONFIRM_PHRASE = "START CAMPAIGN"


class CampaignError(ValueError):
    pass


def create_campaign(s: Session, name: str, *, description: str = "", role_category: str = "SDE Intern", mode: str = "A",
                    template_id: int | None = None, resume_id: int | None = None, target_roles: list[str] | None = None,
                    schedule: dict[str, Any] | None = None, require_approval: bool = True) -> Campaign:
    if not name.strip():
        raise CampaignError("Campaign name is required")
    if s.query(Campaign.id).filter_by(name=name.strip()).first():
        raise CampaignError("A campaign with this name already exists")
    c = Campaign(name=name.strip(), description=description, role_category=role_category, mode=mode, template_id=template_id,
                 resume_id=resume_id, target_roles=target_roles or [role_category], schedule=schedule or {},
                 require_approval=True)  # approval is mandatory: nothing unapproved is ever sent
    s.add(c)
    s.flush()
    audit(s, "campaign_created", "campaign", c.id)
    return c


def add_contacts(s: Session, campaign: Campaign, contact_ids: list[int], exclude_previously_contacted: bool = True) -> dict:
    ok = {c.id for c in eligible_contacts(s, exclude_previously_contacted)}
    existing = {r[0] for r in s.query(CampaignContact.contact_id).filter_by(campaign_id=campaign.id)}
    added = skipped = 0
    for cid in contact_ids:
        if cid in ok and cid not in existing:
            s.add(CampaignContact(campaign_id=campaign.id, contact_id=cid))
            added += 1
        else:
            skipped += 1
    s.flush()
    return {"added": added, "skipped_ineligible_or_duplicate": skipped}


def generate_drafts(s: Session, campaign: Campaign, sender_email: str = "") -> dict:
    tpl = s.get(EmailTemplate, campaign.template_id) if campaign.template_id else None
    if not tpl:
        raise CampaignError("Select a template for this campaign first")
    profile = get_profile(s)
    made = skipped = 0
    for cm in s.query(CampaignContact).filter_by(campaign_id=campaign.id).order_by(CampaignContact.id).all():
        if s.query(EmailMessage.id).filter(EmailMessage.campaign_id == campaign.id, EmailMessage.contact_id == cm.contact_id,
                                           EmailMessage.kind == "initial",
                                           EmailMessage.status.notin_([S.CANCELLED.value, S.REJECTED.value])).first():
            skipped += 1
            continue
        m = msgs.create_draft(s, campaign, cm.contact, tpl, profile, sender_email)
        made += bool(m)
        skipped += not m
    audit(s, "drafts_generated", "campaign", campaign.id, made=made, skipped=skipped)
    return {"created": made, "skipped": skipped}


def schedule_approved(s: Session, campaign: Campaign, now=None) -> dict:
    """Assign send slots to approved emails. Only valid while the campaign is Active."""
    if campaign.status != "Active":
        raise CampaignError("Campaign must be Active to schedule emails")
    now = now or utcnow()
    cfg = {**all_settings(s), **(campaign.schedule or {})}
    approved = (s.query(EmailMessage).filter(EmailMessage.campaign_id == campaign.id, EmailMessage.status == S.APPROVED.value)
                .order_by(EmailMessage.kind.desc(), EmailMessage.id).all())  # follow-ups first
    slots = plan_slots(s, len(approved), now, cfg)
    for m, t in zip(approved, slots):
        msgs.transition(s, m, S.SCHEDULED, scheduled_at=t)
        m.contact.campaign_status = "Scheduled"
    audit(s, "emails_scheduled", "campaign", campaign.id, scheduled=len(slots), unscheduled=len(approved) - len(slots))
    return {"scheduled": len(slots), "left_unscheduled": len(approved) - len(slots)}


def start_campaign(s: Session, campaign: Campaign, confirmation: str, now=None) -> dict:
    """Explicit authorization is required: user must type the confirmation phrase."""
    if confirmation.strip() != CONFIRM_PHRASE:
        raise CampaignError(f'Type "{CONFIRM_PHRASE}" to authorize this campaign')
    if campaign.status in ("Active",):
        raise CampaignError("Campaign is already active")
    if campaign.status == "Stopped":
        raise CampaignError("A stopped campaign cannot be restarted; duplicate it instead")
    campaign.status, campaign.activated_at, campaign.pause_reason, campaign.consecutive_failures = "Active", utcnow(), "", 0
    audit(s, "campaign_started", "campaign", campaign.id, authorized_phrase=True)
    return schedule_approved(s, campaign, now)


def pause_campaign(s: Session, campaign: Campaign, reason: str = "paused by user", actor: str = "user") -> None:
    if campaign.status != "Active":
        raise CampaignError("Only active campaigns can be paused")
    campaign.status, campaign.pause_reason = "Paused", reason
    audit(s, "campaign_paused", "campaign", campaign.id, actor=actor, reason=reason)


def resume_campaign(s: Session, campaign: Campaign, now=None) -> dict:
    if campaign.status != "Paused":
        raise CampaignError("Only paused campaigns can be resumed")
    campaign.status, campaign.pause_reason, campaign.consecutive_failures = "Active", "", 0
    audit(s, "campaign_resumed", "campaign", campaign.id)
    # Emails whose slot elapsed during the pause are re-planned, never burst-sent.
    now = now or utcnow()
    stale = s.query(EmailMessage).filter(EmailMessage.campaign_id == campaign.id, EmailMessage.status == S.SCHEDULED.value,
                                         EmailMessage.scheduled_at < now).all()
    for m in stale:
        msgs.transition(s, m, S.APPROVED, scheduled_at=None)
    return schedule_approved(s, campaign, now)


def cancel_pending(s: Session, campaign: Campaign) -> int:
    n = 0
    for m in s.query(EmailMessage).filter(EmailMessage.campaign_id == campaign.id,
                                          EmailMessage.status.in_([S.SCHEDULED.value, S.APPROVED.value])).all():
        msgs.cancel(s, m)
        n += 1
    audit(s, "pending_cancelled", "campaign", campaign.id, count=n)
    return n


def stop_campaign(s: Session, campaign: Campaign) -> int:
    campaign.status = "Stopped"
    n = cancel_pending(s, campaign)
    audit(s, "campaign_stopped", "campaign", campaign.id)
    return n


def retry_failures(s: Session, campaign: Campaign, now=None) -> int:
    """Only transient failures are eligible. Permanent failures are never auto-retried."""
    n = 0
    for m in s.query(EmailMessage).filter(EmailMessage.campaign_id == campaign.id, EmailMessage.status == S.FAILED.value,
                                          EmailMessage.error_permanent.is_(False)).all():
        msgs.transition(s, m, S.SCHEDULED, scheduled_at=now or utcnow(), attempts=0, next_attempt_at=None, last_error="")
        n += 1
    return n


def campaign_stats(s: Session, campaign_id: int) -> dict[str, int]:
    from sqlalchemy import func
    rows = dict(s.query(EmailMessage.status, func.count()).filter(EmailMessage.campaign_id == campaign_id)
                .group_by(EmailMessage.status).all())
    return {k.value: rows.get(k.value, 0) for k in S}
