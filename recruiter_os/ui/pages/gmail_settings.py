import streamlit as st

from recruiter_os.config import GMAIL_READONLY_SCOPE, HARD_DAILY_CEILING
from recruiter_os.models import Campaign, GmailAccount
from recruiter_os.services import gmail as g
from recruiter_os.services.settings import all_settings, set_setting
from recruiter_os.ui.common import badge, db_scope, fmt_dt, get_app, guarded, header


def render():
    header("Gmail Settings", "Official Gmail API + Google OAuth only. No passwords, no SMTP, no browser automation.")
    app = get_app()
    with db_scope() as s:
        acct = s.query(GmailAccount).order_by(GmailAccount.id.desc()).first()
        cfg = all_settings(s)
        active = s.query(Campaign).filter_by(status="Active").count()
        paused = s.query(Campaign).filter_by(status="Paused").count()
    st.subheader("Connection")
    if acct and acct.status == "connected":
        st.markdown(badge("Connected", "ok") + f" &nbsp; **{acct.email}**", unsafe_allow_html=True)
        st.caption(f"Scopes: {', '.join(x.rsplit('/', 1)[-1] for x in acct.scopes)} · Last sync: {fmt_dt(acct.last_sync_at) or 'never'} · Campaigns: {active} active, {paused} paused")
        if acct.last_error: st.warning(acct.last_error)
        c = st.columns(3)
        if c[0].button("Check Gmail API availability"):
            ok, r = guarded(lambda: app.transport().profile_email())
            if ok: st.success(f"Gmail API reachable. Authenticated as {r}")
            else:
                with db_scope() as s:
                    a = s.get(GmailAccount, acct.id)
                    a.last_error = "Check failed; reconnect if credentials were revoked."
        if c[1].button("Disconnect & revoke"):
            with db_scope() as s:
                note = g.disconnect_account(app.cfg, app.store, s.get(GmailAccount, acct.id))
            st.success(note); st.rerun()
    else:
        st.markdown(badge("Needs re-authorization" if acct and acct.status == "needs_reauth" else "Not connected", "warn"), unsafe_allow_html=True)
        if acct and acct.last_error: st.warning(acct.last_error)
    with st.expander("Connect / reconnect", expanded=not (acct and acct.status == "connected")):
        if not app.cfg.client_secret_file.exists():
            st.info("Upload the OAuth client JSON from Google Cloud Console (type: Desktop app). See the README for the 5-minute setup.")
            up = st.file_uploader("client_secret.json", type=["json"])
            if up and st.button("Save OAuth client file"):
                import json
                try:
                    data = json.loads(up.getvalue())
                    assert "installed" in data, "Expected a 'Desktop app' OAuth client (key 'installed')."
                    app.cfg.client_secret_file.parent.mkdir(parents=True, exist_ok=True)
                    app.cfg.client_secret_file.write_bytes(up.getvalue()); app.cfg.client_secret_file.chmod(0o600)
                    st.success("Saved"); st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Invalid file: {exc}")
        else:
            st.caption("OAuth client file found. Clicking connect opens Google's consent screen in your browser (local redirect to 127.0.0.1).")
            mon = st.checkbox("Also allow read-only Gmail access (needed for reply & bounce detection)", value=cfg["gmail_monitoring_enabled"])
            if st.button("Connect Gmail", type="primary"):
                with st.spinner("Waiting for Google consent in your browser…"):
                    def go():
                        with db_scope() as s:
                            a = g.connect_account(app.cfg, app.store, s, monitoring=mon)
                            set_setting(s, "gmail_monitoring_enabled", mon)
                            if not cfg["test_recipient"]:
                                set_setting(s, "test_recipient", a.email)
                            return a
                    ok, a = guarded(go)
                if ok:
                    st.success(f"Connected {a.email}"); st.rerun()
    st.divider()
    st.subheader("Sending safety")
    with db_scope() as s:
        cfg = all_settings(s)
        test = st.toggle("Test mode (nothing is sent to recruiters)", value=cfg["test_mode"], help="While on, the worker holds all campaign emails. Test copies to yourself still work.")
        if test != cfg["test_mode"]:
            if not test:
                st.warning("You are about to allow real emails to recruiters. Only approved emails in active campaigns will ever be sent.")
                if st.checkbox("I understand and want to turn test mode OFF"):
                    set_setting(s, "test_mode", False); st.rerun()
            else:
                set_setting(s, "test_mode", True); st.rerun()
        tr = st.text_input("Test recipient (your own address)", cfg["test_recipient"])
        cap = st.number_input(f"Daily sending cap (global; app ceiling {HARD_DAILY_CEILING})", 1, HARD_DAILY_CEILING, int(cfg["daily_cap"]),
                              help="Conservative by default. Gmail's own account limits are separate and may be lower; raise only after a clean pilot.")
        if cap > 50: st.warning("High volume for a personal account. Increase gradually and watch bounces.")
        mon2 = st.toggle("Reply & bounce monitoring", cfg["gmail_monitoring_enabled"], help="Requires the read-only scope: reconnect after enabling.")
        if st.button("Save"):
            set_setting(s, "test_recipient", tr); set_setting(s, "daily_cap", int(cap)); set_setting(s, "gmail_monitoring_enabled", mon2)
            if mon2 and acct and GMAIL_READONLY_SCOPE not in [x for x in acct.scopes]:
                st.warning("Saved. Reconnect Gmail (above) with read-only access to activate monitoring.")
            else:
                st.success("Saved")
    with st.expander("Limits you should know"):
        st.markdown("""- Gmail API quota is **not** your sending limit. Personal accounts have their own daily sending limits set by Google (they change; check Google's current docs) and may be lowered further if Google sees abuse.
- OAuth apps in *Testing* mode issue refresh tokens that **expire after 7 days**; reconnect when prompted, or publish the app (personal use, unverified is allowed for ≤100 users).
- Gmail accepting a message is not delivery. Primary placement is up to the recipient's provider.""")
