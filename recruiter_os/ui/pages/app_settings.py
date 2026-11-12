from datetime import date
from zoneinfo import available_timezones

import pandas as pd
import streamlit as st

from recruiter_os.config import HARD_DAILY_CEILING
from recruiter_os.models import AuditLog, Suppression
from recruiter_os.services import backup
from recruiter_os.services.settings import all_settings, set_setting
from recruiter_os.services.suppression import suppress, unsuppress
from recruiter_os.ui.common import db_scope, empty, get_app, guarded, header

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _scheduler():
    with db_scope() as s:
        c = all_settings(s)
        with st.form("sched"):
            a = st.columns(3)
            tz = a[0].selectbox("Time zone", sorted(available_timezones()), index=sorted(available_timezones()).index(c["timezone"]))
            days = a[1].multiselect("Sending days", range(7), default=c["sending_days"], format_func=lambda i: DAYS[i])
            ws = a[2].text_input("Window start (HH:MM)", c["window_start"]); we = a[2].text_input("Window end (HH:MM)", c["window_end"])
            b = st.columns(4)
            cap = b[0].number_input(f"Daily cap (ceiling {HARD_DAILY_CEILING})", 1, HARD_DAILY_CEILING, int(c["daily_cap"]))
            bs = b[1].number_input("Batch size", 1, 100, int(c["batch_size"]))
            bi = b[2].number_input("Batch interval (min)", 1, 1440, int(c["batch_interval_minutes"]))
            pb = b[3].number_input("Pause between emails (sec)", 0, 3600, int(c["pause_between_emails_seconds"]))
            d = st.columns(4)
            mr = d[0].number_input("Max retry attempts", 1, 10, int(c["max_retries"]))
            bo = d[1].number_input("Retry backoff base (min)", 1, 240, int(c["retry_backoff_minutes"]))
            pf = d[2].number_input("Auto-pause after N consecutive failures", 1, 20, int(c["pause_after_consecutive_failures"]))
            si = d[3].number_input("Reply sync interval (min)", 5, 720, int(c["sync_interval_minutes"]))
            e = st.columns(4)
            fw = e[0].number_input("Follow-up wait (days)", 1, 60, int(c["followup_wait_days"]))
            fm = e[1].number_input("Max follow-ups per recruiter", 0, 5, int(c["max_followups"]))
            sd = e[2].text_input("Campaign start date (YYYY-MM-DD, optional)", c["campaign_start_date"])
            ed = e[3].text_input("Campaign end date (optional)", c["campaign_end_date"])
            st.caption("Slots are deterministic (no randomization). Daily cap is global across campaigns and enforced again at send time. "
                       "Restart the worker after changing the sync interval.")
            if st.form_submit_button("Save scheduler settings", type="primary"):
                try:
                    for x in (sd, ed):
                        if x: date.fromisoformat(x)
                    for x in (ws, we):
                        h, m = x.split(":"); assert 0 <= int(h) < 24 and 0 <= int(m) < 60
                    assert ws < we, "window start must be before end"
                    vals = dict(timezone=tz, sending_days=sorted(days), window_start=ws, window_end=we, daily_cap=int(cap), batch_size=int(bs),
                                batch_interval_minutes=int(bi), pause_between_emails_seconds=int(pb), max_retries=int(mr), retry_backoff_minutes=int(bo),
                                pause_after_consecutive_failures=int(pf), sync_interval_minutes=int(si), followup_wait_days=int(fw),
                                max_followups=int(fm), campaign_start_date=sd, campaign_end_date=ed)
                    for k, v in vals.items(): set_setting(s, k, v)
                    st.success("Saved")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Invalid value: {exc}")
        if int(bs) >= 50 or int(cap) > 50:
            st.warning("Large batches / daily caps are risky for a personal Gmail account. A long interval does not make bulk sending safe. Start with a pilot of 5–10.")


def _suppression():
    with db_scope() as s:
        rows = s.query(Suppression).order_by(Suppression.id.desc()).all()
        c = st.columns([3, 1])
        em = c[0].text_input("Add email to suppression list")
        if c[1].button("Suppress", disabled=not em):
            suppress(s, em, "manual"); st.rerun()
        if not rows:
            empty("Suppression list is empty"); return
        st.dataframe(pd.DataFrame([{"email": r.email_normalized, "reason": r.reason, "note": r.note, "added": r.created_at} for r in rows]), hide_index=True, width="stretch")
        pick = st.selectbox("Remove (manual entries only; opt-outs are permanent)", [r.email_normalized for r in rows if r.reason != "opt_out"] or [""])
        if st.button("Remove from list", disabled=not pick):
            ok, _ = guarded(unsuppress, s, pick, success="Removed")
            if ok: st.rerun()


def _backup():
    app = get_app()
    st.markdown("Backups contain your database and resumes. **OAuth tokens are never included.** Keep backups private: they hold recruiter data.")
    if st.button("Create backup now"):
        ok, p = guarded(backup.create_backup, app.db_path, app.cfg.resumes_dir, app.cfg.backups_dir)
        if ok: st.success(f"Saved {p}")
    files = sorted(app.cfg.backups_dir.glob("backup_*.zip"), reverse=True) if app.cfg.backups_dir.exists() else []
    for f in files[:10]:
        a, b = st.columns([4, 1]); a.write(f"{f.name} ({f.stat().st_size / 1024:.0f} KB)")
        b.download_button("Download", f.read_bytes(), f.name, key=f"bk{f.name}")
    with st.expander("Restore from a backup"):
        st.warning("Restore replaces the current database (kept as .pre-restore). Stop the worker first, then restart the app afterwards.")
        up = st.file_uploader("Backup .zip", type=["zip"])
        if up and st.checkbox("I stopped the worker and understand") and st.button("Restore"):
            import tempfile
            from pathlib import Path
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as t:
                t.write(up.getvalue())
            ok, _ = guarded(backup.restore_backup, Path(t.name), app.db_path, app.cfg.resumes_dir, success="Restored. Restart the app now (Ctrl+C, then run again).")


def _audit():
    with db_scope() as s:
        rows = s.query(AuditLog).order_by(AuditLog.id.desc()).limit(500).all()
        if not rows:
            empty("No audit entries yet"); return
        st.caption("Append-only history of sending-related actions (cannot be edited or deleted).")
        st.dataframe(pd.DataFrame([{"at": r.at, "actor": r.actor, "action": r.action, "entity": r.entity, "id": r.entity_id, "detail": str(r.detail)[:200]} for r in rows]),
                     hide_index=True, width="stretch")


def render():
    header("Application Settings", "Scheduler, suppression, backups and audit history.")
    t = st.tabs(["Scheduler", "Suppression list", "Backup & restore", "Audit log", "About / limits"])
    with t[0]: _scheduler()
    with t[1]: _suppression()
    with t[2]: _backup()
    with t[3]: _audit()
    with t[4]:
        st.markdown("""**Local execution:** the scheduler runs only while the worker process is running on your Mac. When the Mac sleeps or is off, nothing is sent;
queued emails stay in the database and resume afterwards, re-checked against the sending window and daily cap. Nothing is ever burst-sent to catch up.

**AI-assisted drafting:** not used. All emails are deterministic templates, so no paid API or recruiter data leaves your machine except via Gmail.""")
