"""Application settings (DB-backed, with defaults) and the editable user profile."""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.orm import Session

from recruiter_os.models import AppSetting, UserProfile

DEFAULT_SETTINGS: dict[str, Any] = {
    "test_mode": True,                    # safe by default: nothing goes to recruiters until switched off
    "test_recipient": "",                 # your own address for test sends
    "timezone": "Asia/Kolkata",
    "sending_days": [0, 1, 2, 3, 4],      # Mon..Fri
    "window_start": "10:00",
    "window_end": "17:00",
    "daily_cap": 20,                      # conservative default (global, across all campaigns)
    "batch_size": 5,
    "batch_interval_minutes": 60,
    "pause_between_emails_seconds": 60,
    "max_retries": 3,
    "retry_backoff_minutes": 10,
    "pause_after_consecutive_failures": 3,
    "followup_wait_days": 8,
    "max_followups": 1,
    "sync_interval_minutes": 30,
    "gmail_monitoring_enabled": False,   # requests the gmail.readonly scope on next connect
    "campaign_start_date": "",
    "campaign_end_date": "",
    "ai_enabled": False,
}

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "", "education": "", "university": "", "graduation_year": "", "status": "",
    "linkedin": "", "github": "", "portfolio": "", "phone": "",
    "languages": [], "frontend": [], "backend": [], "databases": [], "other_skills": [],
    "achievements": [],          # [{"text": "...", "tags": ["dsa"]}]
    "experience": [],            # [{"company","role","points":[...], "tags":[...]}]
    "projects": [],              # [{"name","description","tags":[...]}]
    "stats": {"dsa_problems": "", "leetcode_streak_days": "", "leetcode_rating": ""},
}


def get_setting(s: Session, key: str) -> Any:
    row = s.get(AppSetting, key)
    return copy.deepcopy(row.value) if row else copy.deepcopy(DEFAULT_SETTINGS.get(key))


def all_settings(s: Session) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULT_SETTINGS)
    for row in s.query(AppSetting).all():
        out[row.key] = row.value
    return out


def set_setting(s: Session, key: str, value: Any) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(f"Unknown setting {key}")
    row = s.get(AppSetting, key)
    if row:
        row.value = value
    else:
        s.add(AppSetting(key=key, value=value))
    s.flush()


def get_profile(s: Session) -> dict[str, Any]:
    row = s.query(UserProfile).first()
    out = copy.deepcopy(DEFAULT_PROFILE)
    if row:
        out.update(row.data)
    return out


def save_profile(s: Session, data: dict[str, Any]) -> None:
    row = s.query(UserProfile).first()
    if row:
        row.data = data
    else:
        s.add(UserProfile(data=data))
    s.flush()
