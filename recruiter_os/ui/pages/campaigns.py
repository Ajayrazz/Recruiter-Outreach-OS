import streamlit as st

from recruiter_os.constants import GENERATION_MODES, ROLE_CATEGORIES
from recruiter_os.models import Campaign, CampaignContact, EmailMessage, EmailTemplate, GmailAccount, ResumeFile
from recruiter_os.services import campaigns as svc
from recruiter_os.services import contacts as csvc
from recruiter_os.services.scheduling import effective_cap
from recruiter_os.services.settings import all_settings
from recruiter_os.ui.common import badge, db_scope, empty, fmt_dt, guarded, header

STATE_KIND = {"Draft": "info", "Active": "ok", "Paused": "warn", "Stopped": "bad", "Completed": "ok"}


def _create():
    with st.expander("➕ New campaign", expanded=False):
        with db_scope() as s:
            tpls = {t.name: t.id for t in s.query(EmailTemplate).filter_by(is_followup=False).all()}
            resumes = {f"{r.label} ({r.original_name})": r.id for r in s.query(ResumeFile).filter_by(archived=False).all()}
        with st.form("new_campaign"):
            name = st.text_input("Campaign name", placeholder="SDE Intern Outreach")
            desc = st.text_area("Description")
            c = st.columns(3)
            role = c[0].selectbox("Target role", ROLE_CATEGORIES)
            mode = c[1].selectbox("Generation mode", list(GENERATION_MODES), format_func=lambda k: f"{k} – {GENERATION_MODES[k]}")
            tpl = c[2].selectbox("Template", list(tpls) or ["(none)"])
            res = st.selectbox("Resume to attach (optional: nothing is attached unless chosen)", ["(no attachment)"] + list(resumes))
            if st.form_submit_button("Create campaign", type="primary"):
                def go():
                    with db_scope() as s:
                        svc.create_campaign(s, name, description=desc, role_category=role, mode=mode, template_id=tpls.get(tpl),
                                            resume_id=resumes.get(res))
                ok, _ = guarded(go, success="Campaign created")
                if ok: st.rerun()


def _setup(cid: int):
    with db_scope() as s:
        c = s.get(Campaign, cid)
        tpls = {t.name: t.id for t in s.query(EmailTemplate).filter_by(is_followup=False).all()}
        resumes = {f"{r.label} ({r.original_name})": r.id for r in s.query(ResumeFile).filter_by(archived=False).all()}
        g = all_settings(s)
        locked = c.status in ("Active", "Stopped")
        tname = next((k for k, v in tpls.items() if v == c.template_id), None)
        rname = next((k for k, v in resumes.items() if v == c.resume_id), "(no attachment)")
        with st.form(f"setup_{cid}"):
            desc = st.text_area("Description", c.description)
            a = st.columns(3)
            role = a[0].selectbox("Target role", ROLE_CATEGORIES, index=ROLE_CATEGORIES.index(c.role_category) if c.role_category in ROLE_CATEGORIES else 0)
            mode = a[1].selectbox("Mode", list(GENERATION_MODES), index=list(GENERATION_MODES).index(c.mode), format_func=lambda k: f"{k} – {GENERATION_MODES[k]}")
            tpl = a[2].selectbox("Template", list(tpls) or ["(none)"], index=list(tpls).index(tname) if tname in tpls else 0)
            res = st.selectbox("Resume", ["(no attachment)"] + list(resumes), index=(["(no attachment)"] + list(resumes)).index(rname))
            ov = st.checkbox("Override global schedule for this campaign", value=bool(c.schedule))
            b = st.columns(4)
            cap = b[0].number_input("Daily cap", 1, 150, int((c.schedule or {}).get("daily_cap", g["daily_cap"])))
            bs = b[1].number_input("Batch size", 1, 100, int((c.schedule or {}).get("batch_size", g["batch_size"])))
            bi = b[2].number_input("Batch interval (min)", 1, 1440, int((c.schedule or {}).get("batch_interval_minutes", g["batch_interval_minutes"])))
            st.caption("Global settings (days, window, timezone, pause, retries) are managed in Application Settings.")
            if st.form_submit_button("Save setup", disabled=locked):
                c.description, c.role_category, c.mode, c.template_id = desc, role, mode, tpls.get(tpl)
                c.resume_id = resumes.get(res)
                c.schedule = {"daily_cap": cap, "batch_size": bs, "batch_interval_minutes": bi} if ov else {}
                st.success("Saved")
        if locked:
            st.caption("Setup is locked while a campaign is active or stopped.")


def _recruiters(cid: int):
    with db_scope() as s:
        c = s.get(Campaign, cid)
        members = s.query(CampaignContact).filter_by(campaign_id=cid).count()
        st.markdown(f"**{members}** recruiters in this campaign.")
        excl = st.checkbox("Exclude recruiters already contacted/queued in any campaign (recommended)", True)
        elig = csvc.eligible_contacts(s, excl)
        in_c = {r[0] for r in s.query(CampaignContact.contact_id).filter_by(campaign_id=cid)}
        elig = [e for e in elig if e.id not in in_c]
        st.caption(f"{len(elig):,} eligible contacts (Ready for Outreach, not suppressed, not 'Not Relevant').")
        if not elig:
            empty("No eligible contacts", "Review contacts and mark them 'Ready for Outreach' first.")
            return
        f = st.columns(3)
        seg = f[0].multiselect("Segment", sorted({csvc.segment_of(e.title) for e in elig}))
        q = f[1].text_input("Search company/title", "")
        lim = f[2].number_input("Add at most", 1, 2000, 25, help="Start with a small pilot batch.")
        pool = [e for e in elig if (not seg or csvc.segment_of(e.title) in seg) and (not q or q.lower() in f"{e.company} {e.title}".lower())]
        st.caption(f"{len(pool):,} match your filter.")
        if pool:
            import pandas as pd
            st.dataframe(pd.DataFrame([{"name": e.name, "email": e.email, "title": e.title, "company": e.company} for e in pool[:200]]),
                         hide_index=True, width="stretch")
        if st.button(f"Add first {min(lim, len(pool))} to campaign", disabled=not pool or c.status in ("Stopped",)):
            st.success(str(svc.add_contacts(s, c, [e.id for e in pool[: int(lim)]], excl)))
            st.rerun()


def _launch(cid: int):
    with db_scope() as s:
        c = s.get(Campaign, cid)
        stats = svc.campaign_stats(s, cid)
        g = {**all_settings(s), **(c.schedule or {})}
        acct = s.query(GmailAccount).filter_by(status="connected").first()
        st.markdown(" ".join(badge(f"{k}: {v}", "info") for k, v in stats.items() if v) or "No emails yet.", unsafe_allow_html=True)
        st.write("")
        a = st.columns(3)
        if a[0].button("1 · Generate personalized drafts", disabled=c.status in ("Stopped",)):
            ok, r = guarded(svc.generate_drafts, s, c, acct.email if acct else "")
            if ok: st.success(f"{r}. Review them in **Email Generator**; nothing is sent until you approve.")
        st.info(f"Schedule preview: {effective_cap(g)} emails/day max · batches of {g['batch_size']} every {g['batch_interval_minutes']} min · "
                f"{g['window_start']}–{g['window_end']} {g['timezone']} on weekdays {g['sending_days']}.")
        if g["test_mode"]:
            st.warning("TEST MODE is ON: starting the campaign schedules emails, but the worker will hold them. Switch in Gmail Settings when you are ready.")
        if not acct:
            st.warning("Gmail is not connected yet (Gmail Settings).")
        if c.status in ("Draft", "Paused"):
            if c.status == "Draft":
                if stats["Approved"] == 0:
                    st.caption("Approve some drafts first; start with a small pilot (5–10).")
                phrase = st.text_input('Type **START CAMPAIGN** to authorize', key=f"phrase_{cid}")
                if st.button("2 · Start campaign", type="primary"):
                    ok, r = guarded(svc.start_campaign, s, c, phrase)
                    if ok: st.success(f"Campaign active. {r}"); st.rerun()
            else:
                st.warning(f"Paused: {c.pause_reason}")
                if st.button("Resume campaign", type="primary"):
                    ok, r = guarded(svc.resume_campaign, s, c)
                    if ok: st.success(str(r)); st.rerun()
        if c.status == "Active":
            b = st.columns(4)
            if b[0].button("Schedule newly approved"):
                ok, r = guarded(svc.schedule_approved, s, c)
                if ok: st.success(str(r))
            if b[1].button("Pause"):
                ok, _ = guarded(svc.pause_campaign, s, c)
                if ok: st.rerun()
        d = st.columns(4)
        if d[0].button("Cancel pending emails"):
            st.success(f"Cancelled {svc.cancel_pending(s, c)}"); st.rerun()
        if d[1].button("Retry eligible failures"):
            st.success(f"Re-queued {svc.retry_failures(s, c)} (transient failures only)")
        if d[2].button("Stop campaign", disabled=c.status == "Stopped"):
            st.success(f"Stopped; cancelled {svc.stop_campaign(s, c)} pending"); st.rerun()
        nxt = (s.query(EmailMessage).filter_by(campaign_id=cid, status="Scheduled").order_by(EmailMessage.scheduled_at).first())
        st.markdown("**Next scheduled email:** " + (f"{fmt_dt(nxt.scheduled_at)} → {nxt.to_email}" if nxt else "none"))


def render():
    header("Campaigns", "Group recruiters, a template and a schedule. Starting a campaign always needs your explicit authorization.")
    _create()
    with db_scope() as s:
        camps = [(c.id, c.name, c.status) for c in s.query(Campaign).order_by(Campaign.id.desc()).all()]
    if not camps:
        empty("No campaigns yet", "Create one above (e.g. “SDE Intern Outreach”).")
        return
    sel = st.selectbox("Campaign", camps, format_func=lambda c: f"{c[1]}  ·  {c[2]}")
    cid = sel[0]
    t1, t2, t3 = st.tabs(["Setup", "Recruiters", "Emails & launch"])
    with t1: _setup(cid)
    with t2: _recruiters(cid)
    with t3: _launch(cid)
