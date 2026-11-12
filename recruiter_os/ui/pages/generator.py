import streamlit as st

from recruiter_os.constants import GENERATION_MODES, ROLE_CATEGORIES, S
from recruiter_os.models import Contact, EmailMessage, EmailTemplate, ResumeFile
from recruiter_os.services import messages as msgs
from recruiter_os.services.settings import get_setting
from recruiter_os.ui.common import badge, db_scope, email_preview, empty, guarded, header, messages_frame, select_rows

REVIEW = [S.AWAITING.value, S.DRAFT.value, S.APPROVED.value]


def _review():
    with db_scope() as s:
        df = messages_frame(s, REVIEW)
    if df.empty:
        empty("Nothing to review", "Generate drafts from a campaign (Campaigns → Emails & launch).")
        return
    f = st.columns(3)
    stat = f[0].multiselect("Status", REVIEW, default=[S.AWAITING.value, S.DRAFT.value])
    camp = f[1].multiselect("Campaign id", sorted(df.campaign_id.dropna().unique()))
    q = f[2].text_input("Search recruiter/company")
    v = df[df.status.isin(stat)]
    if camp: v = v[v.campaign_id.isin(camp)]
    if q: v = v[v.recruiter.str.contains(q, case=False) | v.company.str.contains(q, case=False)]
    left, right = st.columns([2, 3])
    with left:
        ids = select_rows(v[["id", "recruiter", "company", "status"]], "gen_sel")
        a = st.columns(2)
        if a[0].button("Approve selected", disabled=not ids, help="Approves exactly the selected, individually-reviewed emails."):
            with db_scope() as s:
                n = 0
                for i in ids:
                    m = s.get(EmailMessage, i)
                    if m.status in (S.AWAITING.value, S.DRAFT.value):
                        ok, _ = guarded(msgs.approve, s, m)
                        n += ok
            st.success(f"Approved {n}"); st.rerun()
        if a[1].button("Reject selected", disabled=not ids):
            with db_scope() as s:
                for i in ids:
                    m = s.get(EmailMessage, i)
                    if m.status in (S.AWAITING.value, S.DRAFT.value, S.APPROVED.value):
                        msgs.reject(s, m)
            st.rerun()
    with right:
        if len(ids) != 1:
            st.info("Select exactly one email on the left to preview and edit it.")
            return
        _detail(ids[0])


def _detail(mid: int):
    with db_scope() as s:
        m = s.get(EmailMessage, mid)
        res = s.get(ResumeFile, m.resume_id) if m.resume_id else None
        st.markdown(f"**{m.contact.name or '(no name)'}** · {m.contact.title} · {m.contact.company} &nbsp; " + badge(m.status, "ok" if m.status == "Approved" else "warn"),
                    unsafe_allow_html=True)
        st.caption(f"To: {m.to_email}   |   From: {m.sender_email or '(connected Gmail account)'}   |   Attachment: {res.original_name if res else 'none'}   |   Kind: {m.kind}")
        locked = m.status not in (S.AWAITING.value, S.DRAFT.value, S.APPROVED.value, S.SCHEDULED.value)
        subj = st.text_input("Subject", m.subject, key=f"s_{mid}", disabled=locked)
        body = st.text_area("Body (plain text; an HTML version is generated automatically)", m.body_text, height=320, key=f"b_{mid}", disabled=locked)
        warns = msgs.lint_email(subj, body)
        for w in warns:
            st.warning(w)
        if not warns:
            st.caption("✓ No quality warnings")
        c = st.columns(5)
        if c[0].button("Save draft", disabled=locked):
            ok, _ = guarded(msgs.edit_message, s, m, subj, body, success="Saved as draft (approval revoked)")
        if c[1].button("Save & request approval", disabled=locked):
            ok, _ = guarded(msgs.edit_message, s, m, subj, body, save_as_draft=False, success="Saved")
        if c[2].button("Regenerate", disabled=locked or m.status == S.SCHEDULED.value):
            ok, _ = guarded(msgs.regenerate, s, m, success="Regenerated from template")
            if ok: st.rerun()
        if c[3].button("Approve", type="primary", disabled=m.status not in (S.AWAITING.value, S.DRAFT.value)):
            if (subj, body.strip() + "\n") != (m.subject, m.body_text):
                st.error("You have unsaved edits. Save first, then approve what you reviewed.")
            else:
                ok, _ = guarded(msgs.approve, s, m, success="Approved")
                if ok: st.rerun()
        if c[4].button("Reject", disabled=locked):
            ok, _ = guarded(msgs.reject, s, m)
            if ok: st.rerun()
        st.divider()
        tr = get_setting(s, "test_recipient")
        st.caption(f"Send a test copy to your own address ({tr or 'not set in Gmail Settings'}). The recruiter is never contacted.")
        if st.button("Send test copy to myself", disabled=not tr):
            ok, r = guarded(lambda: get_app_dispatcher().send_test_copy(mid, tr))
            if ok: st.success(f"Test sent, Gmail id {r['id']}")
        with st.expander("Rendered preview (as the recruiter sees it)"):
            email_preview(subj, body)


def get_app_dispatcher():
    from recruiter_os.ui.common import get_app
    return get_app().dispatcher()


def _try_mode():
    with db_scope() as s:
        cs = s.query(Contact).order_by(Contact.id).limit(300).all()
        tpls = {f"{t.name}": t.id for t in s.query(EmailTemplate).filter_by(is_followup=False).all()}
        if not cs:
            empty("Import contacts first")
            return
        c = st.selectbox("Recruiter", cs, format_func=lambda x: f"{x.name or x.email} · {x.company}")
        a = st.columns(3)
        mode = a[0].selectbox("Mode", list(GENERATION_MODES), format_func=lambda k: f"{k} – {GENERATION_MODES[k]}")
        role = a[1].selectbox("Role", ROLE_CATEGORIES)
        tn = a[2].selectbox("Template", list(tpls))
        subj, body = msgs.compose(s, c, s.get(EmailTemplate, tpls[tn]), role, mode)
        email_preview(subj, body)
        for w in msgs.lint_email(subj, body):
            st.warning(w)
        st.caption("Preview only; nothing is saved. Create real drafts from a campaign.")


def render():
    header("Email Generator", "Review, edit and approve each email. Unapproved content is never sent.")
    t1, t2 = st.tabs(["Review & approve", "Try a mode / role"])
    with t1: _review()
    with t2: _try_mode()
