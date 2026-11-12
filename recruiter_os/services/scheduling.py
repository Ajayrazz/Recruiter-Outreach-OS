"""Deterministic send-slot planning (no randomization). Pure functions + DB usage counting."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from recruiter_os.config import HARD_DAILY_CEILING
from recruiter_os.constants import S
from recruiter_os.models import EmailMessage


def _t(v: str) -> time:
    h, m = v.split(":")
    return time(int(h), int(m))


def effective_cap(cfg: dict[str, Any]) -> int:
    return max(1, min(int(cfg["daily_cap"]), HARD_DAILY_CEILING))


def to_local(dt_utc_naive: datetime, tz: ZoneInfo) -> datetime:
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(tz)


def to_utc_naive(dt_local: datetime) -> datetime:
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def day_bounds_utc(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0, 0), tz)
    return to_utc_naive(start), to_utc_naive(start + timedelta(days=1))


COUNTED = [S.SCHEDULED.value, S.SENDING.value, S.SUBMITTED.value, S.UNKNOWN.value, S.BOUNCED.value, S.REPLIED.value]


def count_for_day(s: Session, d: date, tz: ZoneInfo, exclude_ids: set[int] | None = None) -> int:
    lo, hi = day_bounds_utc(d, tz)
    ts = func.coalesce(EmailMessage.submitted_at, EmailMessage.scheduled_at)
    q = s.query(func.count(EmailMessage.id)).filter(EmailMessage.status.in_(COUNTED), ts >= lo, ts < hi)
    if exclude_ids:
        q = q.filter(~EmailMessage.id.in_(exclude_ids))
    return q.scalar() or 0


def in_window(now_utc: datetime, cfg: dict[str, Any]) -> bool:
    tz = ZoneInfo(cfg["timezone"])
    loc = to_local(now_utc, tz)
    return loc.weekday() in cfg["sending_days"] and _t(cfg["window_start"]) <= loc.time() < _t(cfg["window_end"])


def plan_slots(s: Session | None, n: int, now_utc: datetime, cfg: dict[str, Any], *, used_by_day: dict[date, int] | None = None,
               max_days: int = 120) -> list[datetime]:
    """Return up to n UTC-naive datetimes. Respects sending days, window, batch size/interval, per-email pause,
    campaign start/end dates and the (global) daily cap. If capacity runs out, fewer than n slots are returned."""
    tz = ZoneInfo(cfg["timezone"])
    cap = effective_cap(cfg)
    batch, interval = max(1, int(cfg["batch_size"])), timedelta(minutes=int(cfg["batch_interval_minutes"]))
    gap = timedelta(seconds=int(cfg["pause_between_emails_seconds"]))
    start_d = date.fromisoformat(cfg["campaign_start_date"]) if cfg.get("campaign_start_date") else None
    end_d = date.fromisoformat(cfg["campaign_end_date"]) if cfg.get("campaign_end_date") else None
    w0, w1 = _t(cfg["window_start"]), _t(cfg["window_end"])
    cursor = to_local(now_utc, tz) + timedelta(minutes=1)
    slots: list[datetime] = []
    used = dict(used_by_day or {})
    day = cursor.date()
    for _ in range(max_days):
        if len(slots) >= n:
            break
        if (end_d and day > end_d):
            break
        allowed = day.weekday() in cfg["sending_days"] and (not start_d or day >= start_d)
        if allowed:
            if day not in used:
                used[day] = count_for_day(s, day, tz) if s is not None else 0
            t = datetime.combine(day, w0, tz)
            if cursor > t and cursor.date() == day:
                t = cursor
            end_t = datetime.combine(day, w1, tz)
            in_batch = 0
            while t < end_t and used[day] < cap and len(slots) < n:
                slots.append(to_utc_naive(t))
                used[day] += 1
                in_batch += 1
                if in_batch >= batch:
                    in_batch = 0
                    t += max(interval, gap)
                else:
                    t += gap
        day += timedelta(days=1)
    return slots
