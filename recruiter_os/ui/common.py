"""Shared UI helpers, design tokens (CSS) and cached app container."""
from __future__ import annotations

import time
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from recruiter_os.bootstrap import App, create_app
from recruiter_os.constants import S
from recruiter_os.models import EmailMessage
from recruiter_os.services.settings import get_setting

ASSETS = Path(__file__).parent / "assets"



@st.cache_resource(show_spinner="Starting Recruiter Outreach OS…")
def get_app() -> App:
    return create_app()


def inject_css() -> None:
    link = ('<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">')
    st.markdown(link + "<style>" + (ASSETS / "theme.css").read_text() + "</style>", unsafe_allow_html=True)


def hero(title: str, accent: str, sub: str, pill: str = "Recruiter Outreach OS") -> None:
    """Animated 3D hero (three.js from cdnjs; falls back to a CSS cube offline)."""
    import streamlit.components.v1 as components
    import html as _h
    page = (ASSETS / "hero3d.html").read_text()
    for k, v in {"__PILL__": pill, "__TITLE__": title, "__ACCENT__": accent, "__SUB__": sub}.items():
        page = page.replace(k, _h.escape(v))
    components.html(page, height=270)


def header(title: str, sub: str = "") -> None:
    st.markdown(f"# {title}")
    if sub:
        st.markdown(f'<p class="ros-sub">{sub}</p>', unsafe_allow_html=True)


def badge(text: str, kind: str = "info") -> str:
    return f'<span class="ros-badge ros-{kind}">{text}</span>'


def empty(title: str, hint: str = "") -> None:
    st.markdown(f'<div class="ros-empty"><b>{title}</b>{hint}</div>', unsafe_allow_html=True)


def kpi(label: str, value) -> str:
    return f'<div class="ros-card"><div class="ros-kpi-label">{label}</div><div class="ros-kpi-value">{value}</div></div>'


def email_preview(subject: str, body: str) -> None:
    import html
    st.markdown(f'<div class="ros-email"><b>{html.escape(subject)}</b>\n\n{html.escape(body)}</div>', unsafe_allow_html=True)


@contextmanager
def db_scope() -> Iterator:
    with get_app().db.scope() as s:
        yield s


def guarded(fn, *args, success: str | None = None, **kw):
    """Run an action, surfacing errors in the UI instead of crashing. Returns (ok, result)."""
    try:
        res = fn(*args, **kw)
        if success:
            st.success(success)
        return True, res
    except Exception as exc:  # noqa: BLE001
        st.error(f"{type(exc).__name__}: {exc}")
        return False, None


def tz_name() -> str:
    with db_scope() as s:
        return get_setting(s, "timezone")


def fmt_dt(dt: datetime | None, tz: str | None = None) -> str:
    if not dt:
        return ""
    from datetime import timezone
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz or tz_name())).strftime("%d %b %Y %H:%M")


STATUS_KIND = {"Submitted to Gmail": "info", "Replied": "ok", "Bounced": "bad", "Failed": "bad", "Delivery Unknown": "warn",
               "Scheduled": "info", "Approved": "ok", "Awaiting Approval": "warn", "Suppressed": "bad", "Cancelled": "warn"}


def messages_frame(s, statuses: list[str] | None = None, campaign_id: int | None = None) -> pd.DataFrame:
    from recruiter_os.models import Contact
    q = s.query(EmailMessage, Contact).join(Contact, Contact.id == EmailMessage.contact_id)
    if statuses:
        q = q.filter(EmailMessage.status.in_(statuses))
    if campaign_id:
        q = q.filter(EmailMessage.campaign_id == campaign_id)
    tz = get_setting(s, "timezone")
    rows = [{
        "id": m.id, "recruiter": c.name, "email": m.to_email, "company": c.company, "kind": m.kind, "status": m.status,
        "subject": m.subject, "scheduled": fmt_dt(m.scheduled_at, tz), "submitted": fmt_dt(m.submitted_at, tz),
        "attempts": m.attempts, "error": m.last_error, "gmail_id": m.gmail_message_id or "", "thread_id": m.thread_id or "",
        "reply": m.reply_category, "bounce": m.bounce_type, "campaign_id": m.campaign_id,
    } for m, c in q.order_by(EmailMessage.id.desc()).limit(5000).all()]
    cols = ["id", "recruiter", "email", "company", "kind", "status", "subject", "scheduled", "submitted", "attempts", "error",
            "gmail_id", "thread_id", "reply", "bounce", "campaign_id"]
    return pd.DataFrame(rows, columns=cols)


def worker_status() -> tuple[str, str]:
    hb = get_app().cfg.data_dir / "worker.heartbeat"
    try:
        age = time.time() - int(hb.read_text())
    except Exception:  # noqa: BLE001
        return "Not running", "bad"
    return ("Running", "ok") if age < 120 else (f"Stale ({int(age // 60)} min)", "warn")


def select_rows(df: pd.DataFrame, key: str, **kw) -> list[int]:
    """Dataframe with row multi-select; returns selected 'id' values."""
    if df.empty:
        return []
    ev = st.dataframe(df, key=key, on_select="rerun", selection_mode="multi-row", hide_index=True, width="stretch", **kw)
    return [int(df.iloc[i]["id"]) for i in ev.selection.rows]
