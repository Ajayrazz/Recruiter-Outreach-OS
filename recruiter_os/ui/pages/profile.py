import pandas as pd
import streamlit as st

from recruiter_os.services.settings import get_profile, save_profile
from recruiter_os.ui.common import db_scope, header

TAGS_HELP = "tags: sde, swe, backend, fullstack, ai, dsa (used to pick what is relevant per role)"


def _csv(v): return ", ".join(v)
def _split(t): return [x.strip() for x in t.replace("\n", ",").split(",") if x.strip()]


def render():
    header("Profile Settings", "Everything the generator may say about you. Only what you enter here is ever used. Nothing is invented or inflated.")
    with db_scope() as s:
        p = get_profile(s)
    with st.form("profile"):
        a = st.columns(3)
        name = a[0].text_input("Full name", p["name"]); edu = a[1].text_input("Education", p["education"]); uni = a[2].text_input("University", p["university"])
        b = st.columns(3)
        grad = b[0].text_input("Graduation year", p["graduation_year"]); status = b[1].text_input("Current status", p["status"]); phone = b[2].text_input("Phone (optional)", p["phone"])
        c = st.columns(3)
        li = c[0].text_input("LinkedIn", p["linkedin"]); gh = c[1].text_input("GitHub", p["github"]); pf = c[2].text_input("Portfolio", p["portfolio"])
        d = st.columns(2)
        langs = d[0].text_area("Programming languages (comma separated)", _csv(p["languages"]))
        fe = d[1].text_area("Frontend", _csv(p["frontend"]))
        e = st.columns(3)
        be = e[0].text_area("Backend", _csv(p["backend"])); dbs = e[1].text_area("Databases", _csv(p["databases"])); oth = e[2].text_area("Other skills", _csv(p["other_skills"]))
        st.markdown("**Competitive programming statistics** (shown exactly as entered; never auto-updated)")
        f = st.columns(3)
        dsa = f[0].text_input("DSA problems solved", p["stats"].get("dsa_problems", ""))
        stk = f[1].text_input("LeetCode streak (days)", p["stats"].get("leetcode_streak_days", ""))
        rat = f[2].text_input("LeetCode contest rating", p["stats"].get("leetcode_rating", ""))
        st.markdown("**Achievements** — one phrase per row, written to follow “I …”, e.g. *solved 1,500+ DSA problems*. " + TAGS_HELP)
        ach = st.data_editor(pd.DataFrame([{"text": x["text"], "tags": _csv(x.get("tags", []))} for x in p["achievements"]] or [{"text": "", "tags": ""}]),
                             num_rows="dynamic", width="stretch", key="ach")
        st.markdown("**Experience** — points as separate lines in the cell, phrased after “I …” (e.g. *built a pipeline…*)")
        exp = st.data_editor(pd.DataFrame([{"role": x["role"], "company": x["company"], "points": "\n".join(x["points"]), "tags": _csv(x.get("tags", []))} for x in p["experience"]]
                                           or [{"role": "", "company": "", "points": "", "tags": ""}]), num_rows="dynamic", width="stretch", key="exp")
        st.markdown("**Projects**")
        prj = st.data_editor(pd.DataFrame([{"name": x["name"], "description": x["description"], "tags": _csv(x.get("tags", []))} for x in p["projects"]]
                                           or [{"name": "", "description": "", "tags": ""}]), num_rows="dynamic", width="stretch", key="prj")
        if st.form_submit_button("Save profile", type="primary"):
            clean = lambda df: [r for r in df.fillna("").to_dict("records")]  # noqa: E731
            data = {
                "name": name, "education": edu, "university": uni, "graduation_year": grad, "status": status, "phone": phone,
                "linkedin": li, "github": gh, "portfolio": pf, "languages": _split(langs), "frontend": _split(fe), "backend": _split(be),
                "databases": _split(dbs), "other_skills": _split(oth), "stats": {"dsa_problems": dsa, "leetcode_streak_days": stk, "leetcode_rating": rat},
                "achievements": [{"text": r["text"].strip(), "tags": _split(r["tags"])} for r in clean(ach) if r["text"].strip()],
                "experience": [{"role": r["role"], "company": r["company"], "points": [x.strip() for x in r["points"].split("\n") if x.strip()], "tags": _split(r["tags"])}
                               for r in clean(exp) if r["role"].strip() or r["company"].strip()],
                "projects": [{"name": r["name"], "description": r["description"], "tags": _split(r["tags"])} for r in clean(prj) if r["name"].strip()],
            }
            with db_scope() as s:
                save_profile(s, data)
            st.success("Profile saved. New drafts use it; regenerate existing drafts to apply changes.")
