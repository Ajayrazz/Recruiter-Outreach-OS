from datetime import timedelta

import pytest

from recruiter_os.constants import S
from recruiter_os.db import Database
from recruiter_os.models import AuditLog, Campaign, Contact, EmailEvent, EmailMessage, GmailAccount
from recruiter_os.services import campaigns as camp
from recruiter_os.services import gmail as g
from recruiter_os.services.dispatcher import Dispatcher
from recruiter_os.services.settings import set_setting
from recruiter_os.services.suppression import suppress
from tests.conftest import NOW
from tests.helpers import build_campaign

LATER = NOW + timedelta(minutes=5)


def statuses(app):
    with app.db.scope() as s:
        return [m.status for m in s.query(EmailMessage).order_by(EmailMessage.id)]


def test_nothing_sends_in_test_mode(app, gmail, make_contacts):
    build_campaign(app, make_contacts(2))
    with app.db.scope() as s:
        set_setting(s, "test_mode", True)
    r = Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert gmail.send_calls == 0 and "TEST MODE" in r["held"]


def test_unapproved_never_scheduled_or_sent(app, gmail, make_contacts):
    build_campaign(app, make_contacts(2), approve=False)
    assert set(statuses(app)) == {"Awaiting Approval"}
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert gmail.send_calls == 0


def test_inactive_campaign_never_sends(app, gmail, make_contacts):
    build_campaign(app, make_contacts(2), start=False)
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert gmail.send_calls == 0


def test_send_stores_ids_and_uses_attachment(app, gmail, make_contacts, resume_id):
    build_campaign(app, make_contacts(2), resume_id=resume_id)
    r = Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert r["sent"] == 2 and gmail.send_calls == 2
    assert gmail.sent[0]["attachments"] == ["resume.pdf"]
    with app.db.scope() as s:
        for m in s.query(EmailMessage):
            assert m.status == S.SUBMITTED.value and m.gmail_message_id and m.thread_id and m.submitted_at
            assert m.contact.last_contacted_at and m.contact.campaign_status == "Contacted"
        assert s.query(EmailEvent).filter_by(event_type="submitted").count() == 2
        assert s.query(AuditLog).filter_by(action="email_submitted").count() == 2


def test_no_resume_attached_unless_selected(app, gmail, make_contacts, resume_id):
    build_campaign(app, make_contacts(1), resume_id=None)
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert gmail.sent[0]["attachments"] == []


def test_duplicate_prevention_repeated_ticks(app, gmail, make_contacts):
    build_campaign(app, make_contacts(3))
    d = Dispatcher(app.db, lambda: gmail)
    for _ in range(5):
        d.tick(LATER)
    assert gmail.send_calls == 3


def test_atomic_claim_second_worker_cannot_double_send(app, gmail, make_contacts):
    build_campaign(app, make_contacts(1))
    a, b = Dispatcher(app.db, lambda: gmail), Dispatcher(app.db, lambda: gmail)
    # worker A claims, then B ticks before A completes delivery
    sent_b = []
    orig = a._deliver

    def slow(mid, now, cfg):
        sent_b.append(b.tick(LATER))
        return orig(mid, now, cfg)
    a._deliver = slow
    a.tick(LATER)
    assert gmail.send_calls == 1


def test_restart_does_not_duplicate_or_lose(app, gmail, make_contacts, tmp_path):
    build_campaign(app, make_contacts(3))
    Dispatcher(app.db, lambda: gmail).tick(NOW + timedelta(minutes=1), ) if False else None
    first = Dispatcher(app.db, lambda: gmail, max_per_tick=1)
    first.tick(LATER)
    assert gmail.send_calls == 1
    db2 = Database(app.cfg.db_url)  # "restart": fresh engine, same file
    second = Dispatcher(db2, lambda: gmail)
    second.tick(LATER)
    second.tick(LATER)
    assert gmail.send_calls == 3
    assert len({r["message_id"] for r in gmail.sent}) == 3


def test_crash_mid_send_recovers_via_gmail_lookup_without_resending(app, gmail, make_contacts):
    build_campaign(app, make_contacts(1))
    d = Dispatcher(app.db, lambda: gmail)
    # simulate: message handed to Gmail, process died before DB update
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        m.status, m.rfc822_message_id, m.updated_at = S.SENDING.value, "<crash@x>", NOW
    gmail.sent.append({"id": "mX", "threadId": "tX", "message_id": "<crash@x>"})
    assert d.recover_stale(NOW + timedelta(hours=1)) == 1
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        assert m.status == S.SUBMITTED.value and m.gmail_message_id == "mX"
    assert gmail.send_calls == 0


def test_crash_with_no_trace_becomes_delivery_unknown_not_resent(app, gmail, make_contacts):
    build_campaign(app, make_contacts(1))
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        m.status, m.rfc822_message_id, m.updated_at = S.SENDING.value, "<nope@x>", NOW
    Dispatcher(app.db, lambda: gmail).tick(NOW + timedelta(hours=1))
    assert statuses(app) == [S.UNKNOWN.value] and gmail.send_calls == 0


def test_transient_failure_retries_with_backoff_then_succeeds(app, gmail, make_contacts):
    build_campaign(app, make_contacts(1))
    gmail.fail_with = [g.TransientGmailError("503")]
    d = Dispatcher(app.db, lambda: gmail)
    d.tick(LATER)
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        assert m.status == S.SCHEDULED.value and m.attempts == 1 and m.next_attempt_at > LATER
        assert m.last_error
    assert d.tick(LATER)["sent"] == 0                       # backoff respected
    assert d.tick(LATER + timedelta(minutes=30))["sent"] == 1


def test_backoff_is_exponential_and_bounded(app, gmail, make_contacts):
    build_campaign(app, make_contacts(1))
    gmail.fail_with = [g.TransientGmailError("x")] * 10
    d = Dispatcher(app.db, lambda: gmail)
    d.tick(LATER)
    with app.db.scope() as s:
        first = s.query(EmailMessage).one().next_attempt_at
    assert first == LATER + timedelta(minutes=10)
    d.tick(LATER + timedelta(minutes=11))
    with app.db.scope() as s:
        assert s.query(EmailMessage).one().next_attempt_at == LATER + timedelta(minutes=11) + timedelta(minutes=20)  # doubled
    for mins in (32, 60, 120):
        d.tick(LATER + timedelta(minutes=mins))
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        assert m.status == S.FAILED.value and m.attempts == 3 and not m.error_permanent
        assert m.last_error  # never silent
    assert gmail.send_calls == 3


def test_permanent_failure_not_retried_and_visible(app, gmail, make_contacts):
    build_campaign(app, make_contacts(1))
    gmail.fail_with = [g.PermanentGmailError("invalid recipient")]
    d = Dispatcher(app.db, lambda: gmail)
    d.tick(LATER)
    d.tick(LATER + timedelta(days=2))
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        assert m.status == S.FAILED.value and m.error_permanent and "invalid recipient" in m.last_error
        assert camp.retry_failures(s, s.query(Campaign).one()) == 0  # permanent failures aren't retry-eligible
    assert gmail.send_calls == 1


def test_consecutive_failures_pause_campaign(app, gmail, make_contacts):
    build_campaign(app, make_contacts(5))
    gmail.fail_with = [g.PermanentGmailError("rejected")] * 5
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    with app.db.scope() as s:
        c = s.query(Campaign).one()
        assert c.status == "Paused" and "consecutive" in c.pause_reason
    assert gmail.send_calls == 3  # stopped at the threshold


def test_quota_error_pauses_without_burning_attempts(app, gmail, make_contacts):
    build_campaign(app, make_contacts(2))
    gmail.fail_with = [g.QuotaGmailError("limit")]
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    with app.db.scope() as s:
        assert s.query(Campaign).one().status == "Paused"
        assert {m.attempts for m in s.query(EmailMessage)} == {0}
    assert gmail.send_calls == 1


def test_auth_error_flags_reauth_and_pauses(app, gmail, make_contacts):
    build_campaign(app, make_contacts(2))
    gmail.fail_with = [g.AuthGmailError("revoked")]
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    with app.db.scope() as s:
        assert s.query(GmailAccount).one().status == "needs_reauth"
        assert s.query(Campaign).one().status == "Paused"


def test_daily_cap_enforced_at_send_time(app, gmail, make_contacts):
    with app.db.scope() as s:
        set_setting(s, "daily_cap", 2)
    build_campaign(app, make_contacts(5))
    Dispatcher(app.db, lambda: gmail).tick(NOW + timedelta(hours=1))
    Dispatcher(app.db, lambda: gmail).tick(NOW + timedelta(hours=2))
    assert gmail.send_calls <= 2


def test_suppressed_after_approval_is_not_sent(app, gmail, make_contacts):
    build_campaign(app, make_contacts(2))
    with app.db.scope() as s:
        suppress(s, "p0@co0.com", "opt_out")
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert gmail.send_calls == 1 and gmail.sent[0]["to"] == "p1@co1.com"
    assert S.SUPPRESSED.value in statuses(app)


def test_missing_resume_fails_clearly(app, gmail, make_contacts, resume_id):
    build_campaign(app, make_contacts(1), resume_id=resume_id)
    from pathlib import Path
    with app.db.scope() as s:
        from recruiter_os.models import ResumeFile
        Path(s.get(ResumeFile, resume_id).stored_path).unlink()
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        assert m.status == S.FAILED.value and "Resume problem" in m.last_error
    assert gmail.send_calls == 0


def test_test_copy_goes_only_to_self_and_records_id(app, gmail, make_contacts):
    build_campaign(app, make_contacts(1), start=False)
    with app.db.scope() as s:
        mid = s.query(EmailMessage).one().id
    sent = Dispatcher(app.db, lambda: gmail).send_test_copy(mid, "me@gmail.com")
    assert gmail.sent[0]["to"] == "me@gmail.com" and gmail.sent[0]["subject"].startswith("[TEST]")
    with app.db.scope() as s:
        ev = s.query(EmailEvent).filter_by(event_type="test_sent").one()
        assert ev.gmail_message_id == sent["id"]
        assert s.query(EmailMessage).one().status == "Approved"  # real message untouched


def test_followup_uses_thread_and_reply_headers(app, gmail, make_contacts):
    from recruiter_os.services import followups
    build_campaign(app, make_contacts(1))
    d = Dispatcher(app.db, lambda: gmail)
    d.tick(LATER)
    later = LATER + timedelta(days=9)
    with app.db.scope() as s:
        assert followups.refresh_followups(s, later) == 1
        fu = s.query(followups.FollowUp).one()
        m = followups.draft_followup(s, fu.id)
        from recruiter_os.services import messages as msgs
        msgs.approve(s, m)
        camp.schedule_approved(s, s.query(Campaign).one(), later)
    d.tick(later + timedelta(minutes=5))
    assert gmail.send_calls == 2
    assert gmail.sent[1]["threadId"] == gmail.sent[0]["threadId"]
    assert gmail.sent[1]["mime"]["In-Reply-To"] == gmail.sent[0]["message_id"]


def test_missed_slots_are_replanned_not_drained(app, gmail, make_contacts):
    with app.db.scope() as s:
        set_setting(s, "batch_size", 2)
        set_setting(s, "batch_interval_minutes", 60)
    build_campaign(app, make_contacts(6))
    # Mac was asleep for two days: every slot is now far in the past
    r = Dispatcher(app.db, lambda: gmail).tick(NOW + timedelta(days=2))
    assert r["replanned"] == 6
    assert gmail.send_calls <= 2           # one batch at most, then batch interval applies
    with app.db.scope() as s:
        later = [m.scheduled_at for m in s.query(EmailMessage).filter_by(status=S.SCHEDULED.value)]
        assert later and min(later) > NOW + timedelta(days=2)
