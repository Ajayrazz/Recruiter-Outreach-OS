import streamlit as st

from recruiter_os.models import Contact, EmailTemplate
from recruiter_os.services import messages as msgs
from recruiter_os.services import templates as svc
from recruiter_os.constants import ROLE_CATEGORIES
from recruiter_os.ui.common import db_scope, email_preview, empty, guarded, header

HELP = ("Variables: `{{ recruiter_name }}` `{{ company }}` `{{ recruiter_title }}` `{{ role }}` `{{ my_name }}` `{{ education_line }}` "
        "`{{ graduation_year }}` `{{ skills }}` `{{ achievements }}` `{{ experience.role }}` `{{ experience.company }}` `{{ experience.points }}` "
        "`{{ projects }}` (list of name/description) `{{ linkedin }}` `{{ github }}` `{{ portfolio }}` `{{ role_subject }}` `{{ original_subject }}` (follow-ups). "
        "Use `{% if recruiter_name %}…{% endif %}` for optional parts.")


def render():
    header("Email Templates", "Jinja2 templates. Facts come from your Profile; nothing is invented.")
    with db_scope() as s:
        rows = [(t.id, t.name, t.category, t.is_default) for t in s.query(EmailTemplate).order_by(EmailTemplate.name).all()]
    left, right = st.columns([1, 2])
    with left:
        if st.button("➕ New template", width="stretch"):
            st.session_state["tpl_sel"] = 0
        for tid, name, cat, d in rows:
            if st.button(f"{'★ ' if d else ''}{name}", key=f"t{tid}", width="stretch"):
                st.session_state["tpl_sel"] = tid
    tid = st.session_state.get("tpl_sel", rows[0][0] if rows else 0)
    with right:
        with db_scope() as s:
            t = s.get(EmailTemplate, tid) if tid else None
            name = st.text_input("Name", t.name if t else "", key=f"n{tid}")
            cats = ["A", "B", "C", "followup", "General"] + ROLE_CATEGORIES
            cat = st.selectbox("Category (mode A/B/C, follow-up or role)", cats, index=cats.index(t.category) if t and t.category in cats else 4, key=f"c{tid}")
            subject = st.text_input("Subject", t.subject if t else "Exploring Software Engineering Internship Opportunities", key=f"s{tid}")
            body = st.text_area("Body", t.body if t else "{% if recruiter_name %}Hi {{ recruiter_name }},{% else %}Hello,{% endif %}\n\n", height=340, key=f"b{tid}")
            fu = st.checkbox("This is a follow-up template", t.is_followup if t else False, key=f"f{tid}")
            with st.expander("Variable reference"):
                st.markdown(HELP)
            a = st.columns(4)
            if a[0].button("Save", type="primary"):
                ok, nt = guarded(svc.save_template, s, name, cat, subject, body, t.id if t else None, fu, success="Saved")
                if ok: st.session_state["tpl_sel"] = nt.id; st.rerun()
            if a[1].button("Duplicate", disabled=not t):
                ok, nt = guarded(svc.duplicate_template, s, t.id)
                if ok: st.session_state["tpl_sel"] = nt.id; st.rerun()
            if a[2].button("Set default for category", disabled=not t):
                svc.set_default(s, t.id); st.rerun()
            if a[3].button("Delete", disabled=not t):
                ok, _ = guarded(svc.delete_template, s, t.id, success="Deleted")
                if ok: st.session_state["tpl_sel"] = 0; st.rerun()
            st.markdown("##### Preview")
            cs = s.query(Contact).order_by(Contact.id).limit(200).all()
            if cs:
                c = st.selectbox("Preview with", cs, format_func=lambda x: f"{x.name or x.email} · {x.company}")
                role = st.selectbox("Role", ROLE_CATEGORIES, key="prole")
                try:
                    probe = EmailTemplate(name="p", subject=subject, body=body)
                    subj, bd = msgs.compose(s, c, probe, role, "A", extra_ctx={"original_subject": "…"})
                    email_preview(subj, bd)
                    for w in msgs.lint_email(subj, bd):
                        st.warning(w)
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))
            else:
                st.caption("Import contacts to preview with real data.")
