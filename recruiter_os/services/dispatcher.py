"""Queue dispatcher: claims due emails atomically and submits them to Gmail, one at a time.

Safety properties:
* Claim is an atomic conditional UPDATE (Scheduled -> Sending); two ticks/workers can't both claim a row.
* A unique Message-ID is stored *before* the API call. If the process dies mid-send, recovery looks the
  message up in Gmail by that ID instead of guessing, and never re-sends blindly (-> Delivery Unknown).
* Hard gates re-checked at send time: test mode, campaign Active, window/days, daily cap, suppression,
  approval, resume integrity.
* Permanent errors are never retried; transient errors use exponential backoff up to max_retries.
* Quota/auth problems pause campaigns rather than hammering Gmail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from recruiter_os.constants import S
from recruiter_os.db import Database
from recruiter_os.models import Campaign, EmailMessage, GmailAccount, ResumeFile, utcnow
from recruiter_os.services import messages as msgs
from recruiter_os.services.audit import audit
from recruiter_os.services.campaigns import pause_campaign
from recruiter_os.services.gmail import (AuthGmailError, GmailTransport, PermanentGmailError, QuotaGmailError, TransientGmailError,
                                         build_mime, mime_to_raw, new_rfc822_id, mark_needs_reauth)
from recruiter_os.services.resumes import ResumeError, read_resume_bytes
from recruiter_os.services.scheduling import count_for_day, effective_cap, in_window, to_local
from recruiter_os.services.settings import all_settings, get_profile
from recruiter_os.services.suppression import is_suppressed

log = logging.getLogger("recruiter_os.dispatcher")
STALE_SENDING = timedelta(minutes=10)
OVERDUE = timedelta(minutes=30)


class Dispatcher:
    def __init__(self, db: Database, transport_factory: Callable[[], GmailTransport], max_per_tick: int = 10):
        self.db, self.transport_factory, self.max_per_tick = db, transport_factory, max_per_tick

    # ---------------------------------------------------------------- recovery
    def recover_stale(self, now: datetime | None = None) -> int:
        """Rows stuck in 'Sending' (crash mid-send) are resolved by asking Gmail, never by re-sending."""
        now = now or utcnow()
        fixed = 0
        with self.db.scope() as s:
            stale = s.query(EmailMessage).filter(EmailMessage.status == S.SENDING.value,
                                                 EmailMessage.updated_at < now - STALE_SENDING).all()
            if not stale:
                return 0
            try:
                tr = self.transport_factory()
            except Exception:  # noqa: BLE001
                tr = None
            for m in stale:
                found = None
                try:
                    found = tr.find_by_rfc822_id(m.rfc822_message_id) if tr and m.rfc822_message_id else None
                except Exception as exc:  # noqa: BLE001
                    log.warning("recovery lookup failed", extra={"ctx": {"id": m.id, "err": type(exc).__name__}})
                if found:
                    msgs.transition(s, m, S.SUBMITTED, actor="recovery", gmail_message_id=found["id"],
                                    thread_id=found["threadId"], submitted_at=m.updated_at)
                    msgs.log_event(s, m, "submitted_recovered", gmail_message_id=found["id"])
                else:
                    msgs.transition(s, m, S.UNKNOWN, actor="recovery",
                                    last_error="Interrupted while sending; Gmail lookup found no message. Verify in Gmail Sent folder.")
                    msgs.log_event(s, m, "delivery_unknown", source="inferred")
                fixed += 1
        return fixed

    # ---------------------------------------------------------------- main tick
    def tick(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        report = {"sent": 0, "failed": 0, "held": "", "recovered": self.recover_stale(now)}
        with self.db.scope() as s:
            cfg = all_settings(s)
            if cfg["test_mode"]:
                due = s.query(EmailMessage).filter(EmailMessage.status == S.SCHEDULED.value, EmailMessage.scheduled_at <= now).count()
                report["held"] = f"TEST MODE: {due} due email(s) held; nothing is sent to recruiters."
                return report
            acct = s.query(GmailAccount).filter_by(status="connected").first()
            if not acct:
                report["held"] = "Gmail not connected"
                return report
        report["replanned"] = self.replan_overdue(now)
        for _ in range(self.max_per_tick):
            outcome = self._send_next(now, acct.email)
            if outcome in ("none", "gate"):
                report["held"] = report["held"] or ("" if outcome == "none" else "gated (window/cap/pause)")
                break
            report["sent" if outcome == "sent" else "failed"] += 1
            if outcome == "halt":
                break
        return report

    def replan_overdue(self, now: datetime) -> int:
        """Slots missed (Mac asleep, window closed, pause) are re-planned with normal batch spacing and caps
        rather than being drained as a backlog."""
        from recruiter_os.services.campaigns import schedule_approved
        n = 0
        with self.db.scope() as s:
            overdue = (s.query(EmailMessage).join(Campaign, Campaign.id == EmailMessage.campaign_id)
                       .filter(EmailMessage.status == S.SCHEDULED.value, EmailMessage.scheduled_at < now - OVERDUE,
                               Campaign.status == "Active").all())
            camps = {}
            for m in overdue:
                msgs.transition(s, m, S.APPROVED, actor="dispatcher", scheduled_at=None)
                camps[m.campaign_id] = m.campaign
                n += 1
            for c in camps.values():
                schedule_approved(s, c, now)
        return n

    def _send_next(self, now: datetime, sender_email: str) -> str:
        """Returns none | gate | sent | failed | halt."""
        with self.db.scope() as s:
            cfg = all_settings(s)
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(cfg["timezone"])
            q = (s.query(EmailMessage).join(Campaign, Campaign.id == EmailMessage.campaign_id)
                 .filter(EmailMessage.status == S.SCHEDULED.value, EmailMessage.scheduled_at <= now, Campaign.status == "Active")
                 .order_by(EmailMessage.scheduled_at, EmailMessage.id))
            m = q.first()
            if not m:
                return "none"
            ccfg = {**cfg, **(m.campaign.schedule or {})}
            if not in_window(now, ccfg):
                return "gate"
            if count_for_day(s, to_local(now, tz).date(), tz, exclude_ids={m.id}) >= effective_cap(ccfg):
                return "gate"
            last = s.query(EmailMessage.submitted_at).filter(EmailMessage.submitted_at.isnot(None)).order_by(EmailMessage.submitted_at.desc()).first()
            if last and last[0] and now - last[0] < timedelta(seconds=int(cfg["pause_between_emails_seconds"])):
                return "gate"
            mid, rfc_id = m.id, m.rfc822_message_id or new_rfc822_id()
            # ---- atomic claim ----
            res = s.execute(update(EmailMessage).where(EmailMessage.id == mid, EmailMessage.status == S.SCHEDULED.value)
                            .values(status=S.SENDING.value, rfc822_message_id=rfc_id, sender_email=sender_email,
                                    attempts=EmailMessage.attempts + 1, updated_at=now))
            if res.rowcount != 1:
                return "gate"
        # claim committed; now do the work in a fresh transaction
        return self._deliver(mid, now, cfg)

    def _fail(self, s: Session, m: EmailMessage, err: str, permanent: bool, cfg: dict, now: datetime) -> str:
        camp = m.campaign
        camp.consecutive_failures += 1
        if not permanent and m.attempts < int(cfg["max_retries"]):
            delay = timedelta(minutes=int(cfg["retry_backoff_minutes"]) * 2 ** (m.attempts - 1))
            msgs.transition(s, m, S.SCHEDULED, actor="dispatcher", scheduled_at=now + delay, next_attempt_at=now + delay, last_error=err[:500])
            msgs.log_event(s, m, "retry_scheduled", error=err[:200], attempt=m.attempts)
        else:
            msgs.transition(s, m, S.FAILED, actor="dispatcher", last_error=err[:500], error_permanent=permanent)
            msgs.log_event(s, m, "failed", error=err[:200], permanent=permanent)
        if camp.status == "Active" and camp.consecutive_failures >= int(cfg["pause_after_consecutive_failures"]):
            pause_campaign(s, camp, f"auto-paused after {camp.consecutive_failures} consecutive failures", actor="dispatcher")
        log.error("send failed", extra={"ctx": {"id": m.id, "permanent": permanent, "err": err[:200]}})
        return "failed"

    def _deliver(self, mid: int, now: datetime, cfg: dict) -> str:
        with self.db.scope() as s:
            m = s.get(EmailMessage, mid)
            # ---- pre-send safety checks ----
            if m.approved_at is None:
                msgs.transition(s, m, S.FAILED, actor="dispatcher", last_error="Not approved", error_permanent=True)
                return "failed"
            if m.contact.opted_out or is_suppressed(s, m.to_email) or m.contact.verification_status in ("Invalid", "Suppressed"):
                msgs.transition(s, m, S.SCHEDULED, actor="dispatcher")  # allowed transition, then suppress
                msgs.transition(s, m, S.SUPPRESSED, actor="dispatcher")
                return "failed"
            attachment = None
            if m.resume_id:
                r = s.get(ResumeFile, m.resume_id)
                try:
                    attachment = (r.original_name, read_resume_bytes(r))
                except (FileNotFoundError, ResumeError) as exc:
                    return self._fail(s, m, f"Resume problem: {exc}", True, cfg, now)
            prof = get_profile(s)
            in_reply_to = refs = None
            if m.parent_message_id:
                parent = s.get(EmailMessage, m.parent_message_id)
                in_reply_to = parent.rfc822_message_id
            try:
                mime = build_mime(sender=m.sender_email, sender_name=prof.get("name", ""), to=m.to_email, subject=m.subject,
                                  body_text=m.body_text, rfc822_id=m.rfc822_message_id, attachment=attachment,
                                  in_reply_to=in_reply_to, references=refs)
                tr = self.transport_factory()
                sent = tr.send_raw(mime_to_raw(mime), thread_id=m.thread_id)
            except AuthGmailError as exc:
                acct = s.query(GmailAccount).filter_by(status="connected").first()
                if acct:
                    mark_needs_reauth(acct, str(exc))
                msgs.transition(s, m, S.SCHEDULED, actor="dispatcher", last_error=str(exc)[:300], attempts=max(0, m.attempts - 1))
                for c in s.query(Campaign).filter_by(status="Active").all():
                    pause_campaign(s, c, "Gmail needs re-authorization", actor="dispatcher")
                return "halt"
            except QuotaGmailError as exc:
                msgs.transition(s, m, S.SCHEDULED, actor="dispatcher", last_error=str(exc)[:300], attempts=max(0, m.attempts - 1),
                                scheduled_at=now + timedelta(hours=1))
                pause_campaign(s, m.campaign, "Gmail quota/rate limit reached", actor="dispatcher")
                msgs.log_event(s, m, "quota_limit", error=str(exc)[:200])
                return "halt"
            except TransientGmailError as exc:
                return self._fail(s, m, str(exc), False, cfg, now)
            except PermanentGmailError as exc:
                return self._fail(s, m, str(exc), True, cfg, now)
            # Submitted. NOTE: this is acceptance by Gmail's API only, not inbox/Primary delivery.
            msgs.transition(s, m, S.SUBMITTED, actor="dispatcher", gmail_message_id=sent["id"], thread_id=sent["threadId"] or m.thread_id,
                            submitted_at=utcnow(), last_error="")
            msgs.log_event(s, m, "submitted", gmail_message_id=sent["id"])
            m.campaign.consecutive_failures = 0
            c = m.contact
            c.last_contacted_at, c.last_email_subject = m.submitted_at, m.subject
            c.campaign_status = "Contacted" if m.kind == "initial" else "Followed Up"
            return "sent"

    def send_test_copy(self, message_id: int, test_recipient: str) -> dict:
        """Send a copy of a message to the user's OWN address for visual checking. Records an event only."""
        with self.db.scope() as s:
            m = s.get(EmailMessage, message_id)
            cfg = all_settings(s)
            acct = s.query(GmailAccount).filter_by(status="connected").first()
            if not acct:
                raise AuthGmailError("Gmail not connected")
            if not test_recipient:
                raise ValueError("Set a test recipient (your own address) in Gmail Settings first")
            prof = get_profile(s)
            attachment = None
            if m.resume_id:
                r = s.get(ResumeFile, m.resume_id)
                attachment = (r.original_name, read_resume_bytes(r))
            mime = build_mime(sender=acct.email, sender_name=prof.get("name", ""), to=test_recipient,
                              subject=f"[TEST] {m.subject}", body_text=m.body_text, rfc822_id=new_rfc822_id(), attachment=attachment)
            sent = self.transport_factory().send_raw(mime_to_raw(mime))
            msgs.log_event(s, m, "test_sent", gmail_message_id=sent["id"], to=test_recipient)
            audit(s, "test_send", "email", m.id, to=test_recipient)
            return sent
