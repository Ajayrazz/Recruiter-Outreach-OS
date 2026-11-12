"""SQLAlchemy ORM models.

Design note: ``email_messages`` is the single record for an email across its whole life
(draft -> approval -> queue -> Gmail submission -> reply/bounce). Separate draft/queue/sent
tables would duplicate state and invite inconsistency; ``email_events`` keeps the history.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC everywhere in the DB


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserProfile(Base, Timestamped):
    __tablename__ = "user_profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Contact(Base, Timestamped):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(320))
    email_normalized: Mapped[str] = mapped_column(String(320), unique=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(200), default="")
    verification_status: Mapped[str] = mapped_column(String(60), default="Unreviewed")
    relevance_status: Mapped[str] = mapped_column(String(40), default="Unreviewed")
    campaign_status: Mapped[str] = mapped_column(String(40), default="Not Contacted")
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_email_subject: Mapped[str] = mapped_column(String(500), default="")
    reply_status: Mapped[str] = mapped_column(String(60), default="")
    followup_status: Mapped[str] = mapped_column(String(40), default="")
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    original_record: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # preserved as imported
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))

    __table_args__ = (Index("ix_contacts_company", "company"), Index("ix_contacts_verif", "verification_status"),
                      Index("ix_contacts_campaign_status", "campaign_status"))


class ImportBatch(Base, Timestamped):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rejected: Mapped[list[Any]] = mapped_column(JSON, default=list)


class Campaign(Base, Timestamped):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    role_category: Mapped[str] = mapped_column(String(60), default="SDE Intern")
    mode: Mapped[str] = mapped_column(String(2), default="A")
    template_id: Mapped[int | None] = mapped_column(ForeignKey("email_templates.id"))
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resume_files.id"))
    status: Mapped[str] = mapped_column(String(20), default="Draft")
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    pause_reason: Mapped[str] = mapped_column(String(300), default="")
    members: Mapped[list["CampaignContact"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")


class CampaignContact(Base, Timestamped):
    __tablename__ = "campaign_contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    campaign: Mapped[Campaign] = relationship(back_populates="members")
    contact: Mapped[Contact] = relationship()
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id"),)


class EmailTemplate(Base, Timestamped):
    __tablename__ = "email_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    category: Mapped[str] = mapped_column(String(60), default="General")  # mode letter or role
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)  # Jinja2 plain text
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_followup: Mapped[bool] = mapped_column(Boolean, default=False)


class EmailMessage(Base, Timestamped):
    __tablename__ = "email_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    kind: Mapped[str] = mapped_column(String(12), default="initial")  # initial | followup | test
    followup_number: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="Draft")
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(500))
    body_text: Mapped[str] = mapped_column(Text)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resume_files.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("email_templates.id"))
    sender_email: Mapped[str] = mapped_column(String(320), default="")
    thread_id: Mapped[str | None] = mapped_column(String(100))
    parent_message_id: Mapped[int | None] = mapped_column(ForeignKey("email_messages.id"))
    rfc822_message_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="")
    error_permanent: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_category: Mapped[str] = mapped_column(String(60), default="")
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    bounce_type: Mapped[str] = mapped_column(String(10), default="")  # hard | soft
    contact: Mapped[Contact] = relationship()
    campaign: Mapped[Campaign | None] = relationship()

    __table_args__ = (
        Index("ix_msg_status_sched", "status", "scheduled_at"),
        Index("ix_msg_thread", "thread_id"),
        Index("ix_msg_campaign", "campaign_id"),
        Index("ix_msg_contact", "contact_id"),
        # A contact may only ever have ONE live/sent initial outreach, across all campaigns.
        Index("uq_one_initial_per_contact", "contact_id", unique=True,
              sqlite_where=text("kind = 'initial' AND status NOT IN ('Cancelled','Rejected')")),
        # At most one follow-up per (contact, number) that is not cancelled/rejected.
        Index("uq_one_followup_per_number", "contact_id", "followup_number", unique=True,
              sqlite_where=text("kind = 'followup' AND status NOT IN ('Cancelled','Rejected')")),
    )


class EmailEvent(Base):
    """Observed (not inferred) events: submitted, failed, bounce, reply, ..."""
    __tablename__ = "email_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("email_messages.id"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), default="observed")  # observed | inferred
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    gmail_message_id: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (Index("ix_event_type_time", "event_type", "occurred_at"),
                      UniqueConstraint("event_type", "gmail_message_id", "message_id", name="uq_event_dedupe"))


class GmailAccount(Base, Timestamped):
    __tablename__ = "gmail_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="connected")  # connected | needs_reauth | disconnected
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_ref: Mapped[str] = mapped_column(String(300), default="")  # path of encrypted blob; NOT the token
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="")
    history_cursor: Mapped[str] = mapped_column(String(100), default="")


class ResumeFile(Base, Timestamped):
    __tablename__ = "resume_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    original_name: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    role_category: Mapped[str] = mapped_column(String(60), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class FollowUp(Base, Timestamped):
    __tablename__ = "follow_ups"
    id: Mapped[int] = mapped_column(primary_key=True)
    original_message_id: Mapped[int] = mapped_column(ForeignKey("email_messages.id"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    number: Mapped[int] = mapped_column(Integer, default=1)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="Due")  # Due | Drafted | Dismissed | Done
    draft_message_id: Mapped[int | None] = mapped_column(ForeignKey("email_messages.id"))
    __table_args__ = (UniqueConstraint("original_message_id", "number"), Index("ix_fu_due", "status", "due_at"))


class Suppression(Base):
    __tablename__ = "suppression_list"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_normalized: Mapped[str] = mapped_column(String(320), unique=True)
    reason: Mapped[str] = mapped_column(String(40))  # hard_bounce | opt_out | manual | invalid
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "application_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """Append-only. UPDATE/DELETE are blocked by SQLite triggers (see db.py)."""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(40), default="user")
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(60), default="")
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_audit_action_at", "action", "at"),)
