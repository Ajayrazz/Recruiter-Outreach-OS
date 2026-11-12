import streamlit as st

from recruiter_os.models import Contact, EmailMessage, EmailTemplate, FollowUp
from recruiter_os.services import followups as svc
from recruiter_os.services.settings import all_settings
from recruiter_os.ui.common import db_scope, empty, fmt_dt, guarded, header


def render():
    header("Follow-ups", "Reminders only. Every follow-up is drafted, reviewed and approved by you. Limited and never sent after a reply, bounce or opt-out.")
    with db_scope() as s:
        cfg = all_settings(s)
    st.caption(f"Rules: wait {cfg['followup_wait_days']} days · max {cfg['max_followups']} follow-up(s) per recruiter · change in Application Settings.")
    if st.button("Refresh due follow-ups"):
        with db_scope() as s:
            d, n = svc.cleanup_stale(s), svc.refresh_followups(s)
        st.success(f"{n} new due, {d} dismissed as no longer valid")
    with db_scope() as s:
        due = (s.query(FollowUp, Contact, EmailMessage).join(Contact, Contact.id == FollowUp.contact_id)
               .join(EmailMessage, EmailMessage.id == FollowUp.original_message_id).filter(FollowUp.status == "Due")
               .order_by(FollowUp.due_at).all())
        tpls = {t.name: t.id for t in s.query(EmailTemplate).filter_by(is_followup=True).all()}
        if not due:
            empty("No follow-ups due", "They appear once the waiting period has passed with no reply.")
            return
        tn = st.selectbox("Follow-up template", list(tpls)) if tpls else None
        for f, c, m in due:
            with st.container(border=True):
                a, b, d = st.columns([4, 1.4, 1.2])
                a.markdown(f"**{c.name or c.email}** · {c.company}  \nSent {fmt_dt(m.submitted_at)} · due {fmt_dt(f.due_at)} · follow-up #{f.number}  \n_{m.subject}_")
                if b.button("Draft follow-up", key=f"fd{f.id}"):
                    ok, nm = guarded(svc.draft_followup, s, f.id, tpls.get(tn))
                    if ok:
                        st.success("Drafted. Review & approve it in Email Generator."); st.rerun()
                if d.button("Dismiss", key=f"fx{f.id}"):
                    svc.dismiss(s, f.id); st.rerun()
