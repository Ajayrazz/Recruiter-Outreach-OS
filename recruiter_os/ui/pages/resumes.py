from pathlib import Path

import streamlit as st

from recruiter_os.config import MAX_RESUME_BYTES
from recruiter_os.constants import ROLE_CATEGORIES
from recruiter_os.models import ResumeFile
from recruiter_os.services import resumes as svc
from recruiter_os.ui.common import badge, db_scope, empty, fmt_dt, get_app, guarded, header


def render():
    header("Resume Manager", f"PDF only, up to {MAX_RESUME_BYTES // (1024 * 1024)} MB. A resume is attached only when a campaign explicitly selects it.")
    app = get_app()
    with st.expander("Upload a resume", expanded=True):
        up = st.file_uploader("PDF resume", type=["pdf"], key="resume_up")
        c = st.columns(2)
        label = c[0].text_input("Label", placeholder="Backend resume 2026")
        role = c[1].selectbox("Intended for", [""] + ROLE_CATEGORIES, format_func=lambda x: x or "(any)")
        if st.button("Save resume", disabled=not up, type="primary"):
            def go():
                with db_scope() as s:
                    svc.add_resume(s, app.cfg.resumes_dir, up.getvalue(), up.name, label, role)
            ok, _ = guarded(go, success="Resume saved")
            if ok: st.rerun()
    with db_scope() as s:
        rows = s.query(ResumeFile).order_by(ResumeFile.archived, ResumeFile.id.desc()).all()
        if not rows:
            empty("No resumes uploaded")
            return
        for r in rows:
            with st.container(border=True):
                a, b = st.columns([4, 3])
                tag = (badge("default", "ok") if r.is_default else "") + (badge("archived", "warn") if r.archived else "")
                a.markdown(f"**{r.label}** {tag}  \n{r.original_name} · {r.size_bytes / 1024:.0f} KB · {r.role_category or 'any role'} · uploaded {fmt_dt(r.created_at)}  \n`sha256 {r.sha256[:16]}…`",
                           unsafe_allow_html=True)
                with b:
                    k = st.columns(3)
                    if not r.archived:
                        if k[0].button("Make default", key=f"d{r.id}", disabled=r.is_default):
                            svc.set_default_resume(s, r.id); st.rerun()
                        if k[1].button("Archive", key=f"a{r.id}"):
                            r.archived, r.is_default = True, False; st.rerun()
                    try:
                        k[2].download_button("Download", svc.read_resume_bytes(r), r.original_name, "application/pdf", key=f"dl{r.id}")
                    except Exception as exc:  # noqa: BLE001
                        k[2].error(str(exc))
                if not r.archived:
                    new = st.file_uploader(f"Replace “{r.label}” with a newer PDF", type=["pdf"], key=f"rp{r.id}")
                    if new and st.button("Replace", key=f"rpb{r.id}"):
                        ok, _ = guarded(svc.replace_resume, s, r.id, app.cfg.resumes_dir, new.getvalue(), new.name, success="Replaced (old version archived)")
                        if ok: st.rerun()
