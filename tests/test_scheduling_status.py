from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from recruiter_os.constants import S, TRANSITIONS
from recruiter_os.models import EmailMessage
from recruiter_os.services import messages as msgs
from recruiter_os.services.scheduling import in_window, plan_slots, to_local
from recruiter_os.services.settings import DEFAULT_SETTINGS

TZ = ZoneInfo("Asia/Kolkata")


def cfg(**kw):
    return {**DEFAULT_SETTINGS, **kw}


def test_default_cap_is_conservative():
    assert DEFAULT_SETTINGS["daily_cap"] <= 30 and DEFAULT_SETTINGS["test_mode"] is True


def test_slots_respect_days_window_and_cap():
    now = datetime(2026, 11, 13, 4, 0)  # Fri 09:30 IST, before window opens
    slots = plan_slots(None, 45, now, cfg(daily_cap=20, batch_size=5, batch_interval_minutes=60, pause_between_emails_seconds=60))
    assert len(slots) == 45
    locs = [to_local(x, TZ) for x in slots]
    assert all(l.weekday() < 5 for l in locs)                      # no weekend
    assert all(10 <= l.hour < 17 for l in locs)                    # inside window
    per_day = {}
    for l in locs:
        per_day[l.date()] = per_day.get(l.date(), 0) + 1
    assert max(per_day.values()) == 20                              # cap enforced per day
    assert date(2026, 11, 14) not in per_day and date(2026, 11, 15) not in per_day
    assert slots == sorted(slots)


def test_batch_50_every_2h_is_configurable_but_capped():
    """The user's preferred 50/2h schedule is allowed as a setting, but the daily cap still binds."""
    now = datetime(2026, 11, 16, 4, 0)
    slots = plan_slots(None, 200, now, cfg(batch_size=50, batch_interval_minutes=120, daily_cap=30, pause_between_emails_seconds=60))
    per_day = {}
    for x in slots:
        d = to_local(x, TZ).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= 30


def test_hard_ceiling_applies_even_if_user_sets_huge_cap():
    slots = plan_slots(None, 1000, datetime(2026, 11, 16, 4, 0), cfg(daily_cap=100000, batch_size=500, pause_between_emails_seconds=1,
                                                                     window_start="00:00", window_end="23:59"))
    per_day = {}
    for x in slots:
        d = to_local(x, TZ).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= 150


def test_end_date_limits_slots():
    slots = plan_slots(None, 500, datetime(2026, 11, 16, 4, 0), cfg(campaign_end_date="2026-11-16", daily_cap=20))
    assert len(slots) <= 20


def test_start_date_defers():
    slots = plan_slots(None, 3, datetime(2026, 11, 16, 4, 0), cfg(campaign_start_date="2026-11-18"))
    assert to_local(slots[0], TZ).date() == date(2026, 11, 18)


def test_no_randomization_is_deterministic():
    a = plan_slots(None, 10, datetime(2026, 11, 16, 4, 0), cfg())
    b = plan_slots(None, 10, datetime(2026, 11, 16, 4, 0), cfg())
    assert a == b


def test_in_window():
    assert in_window(datetime(2026, 11, 16, 5, 0), cfg())        # Mon 10:30 IST
    assert not in_window(datetime(2026, 11, 15, 5, 0), cfg())    # Sunday
    assert not in_window(datetime(2026, 11, 16, 15, 0), cfg())   # Mon 20:30 IST


def test_status_transition_table_is_closed():
    for terminal in (S.BOUNCED, S.CANCELLED, S.SUPPRESSED):
        assert not (TRANSITIONS[terminal] - {S.REPLIED})
    assert S.SENDING not in TRANSITIONS[S.DRAFT] and S.SENDING not in TRANSITIONS[S.APPROVED]  # must be Scheduled first
    assert S.SCHEDULED not in TRANSITIONS[S.AWAITING]  # approval can't be skipped


def test_invalid_transition_rejected(app, make_contacts):
    ids = make_contacts(1)
    with app.db.scope() as s:
        m = EmailMessage(contact_id=ids[0], status="Awaiting Approval", to_email="a@b.com", subject="s", body_text="b", idempotency_key="k")
        s.add(m)
        s.flush()
        with pytest.raises(msgs.InvalidTransition):
            msgs.transition(s, m, S.SCHEDULED)
        with pytest.raises(msgs.InvalidTransition):
            msgs.transition(s, m, S.SUBMITTED)
