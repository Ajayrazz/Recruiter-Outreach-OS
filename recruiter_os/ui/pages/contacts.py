import pandas as pd
import streamlit as st

from recruiter_os.constants import RELEVANCE, VERIFICATION
from recruiter_os.models import Contact
from recruiter_os.services import contacts as svc
from recruiter_os.services.suppression import suppress
from recruiter_os.ui.common import db_scope, empty, guarded, header, messages_frame, select_rows

DEFAULT_LOCAL = "data/imports/hr_contacts.csv"


def _import_tab():
    st.markdown("Upload a CSV or Excel file. Imported contacts are **never** emailed automatically: they must be reviewed and marked *Ready for Outreach* first.")
    up = st.file_uploader("Contacts file", type=["csv", "xlsx"], key="contacts_up")
    df, name = None, None
    if up:
        ok, df = guarded(svc.read_table, up, up.name)
        name = up.name
    else:
        with st.expander("Or import a file already on this Mac"):
            path = st.text_input("Path", DEFAULT_LOCAL)
            if st.button("Load file"):
                ok, df = guarded(svc.read_table, path)
                if ok:
                    st.session_state["local_df"], st.session_state["local_name"] = df, path.split("/")[-1]
            df, name = st.session_state.get("local_df"), st.session_state.get("local_name")
    if df is None or df.empty:
        return
    st.markdown(f"**{len(df):,} rows** detected. Columns: {', '.join(df.columns)}")
    auto = svc.auto_map_columns(list(df.columns))
    cols = ["(none)"] + list(df.columns)
    mc = st.columns(4)
    mapping = {}
    for c, f in zip(mc, ["name", "email", "title", "company"]):
        sel = c.selectbox(f.title() + (" *" if f == "email" else ""), cols, index=cols.index(auto[f]) if auto[f] else 0, key=f"map_{f}")
        mapping[f] = None if sel == "(none)" else sel
    st.dataframe(df.head(8), hide_index=True, width="stretch")
    if st.button("Import contacts", type="primary"):
        ok, res = guarded(lambda: _do_import(df, mapping, name))
        if ok:
            st.session_state["last_import"] = res
    res = st.session_state.get("last_import")
    if res:
        st.success(f"Imported {res.imported:,} of {res.total_rows:,} rows.")
        a, b, c, d = st.columns(4)
        a.metric("Invalid email syntax", res.invalid_syntax)
        b.metric("Duplicates in file", res.duplicates_in_file)
        c.metric("Already in database", res.already_in_db)
        d.metric("Rejected total", len(res.rejected))
        st.caption(f"Imported rows missing: name {res.missing_name}, company {res.missing_company}, designation {res.missing_title}.")
        if res.rejected:
            st.dataframe(pd.DataFrame(res.rejected), hide_index=True, width="stretch")
            st.download_button("Download rejected records (CSV)", svc.rejected_to_csv(res.rejected), "rejected_contacts.csv", "text/csv")


def _do_import(df, mapping, name):
    with db_scope() as s:
        return svc.import_contacts(s, df, mapping, name)


def _browse_tab():
    with db_scope() as s:
        df = svc.contacts_frame(s)
    if df.empty:
        empty("No contacts yet", "Use the Import tab to add your recruiter list.")
        return
    f = st.columns([2, 2, 2, 2])
    q = f[0].text_input("Search name / email / company / title", "")
    seg = f[1].multiselect("Segment", sorted(df.segment.unique()))
    ver = f[2].multiselect("Verification", VERIFICATION)
    rel = f[3].multiselect("Relevance", RELEVANCE)
    g = st.columns([2, 2, 2])
    camp = g[0].multiselect("Campaign status", sorted(df.campaign_status.unique()))
    opt = g[1].selectbox("Opt-out", ["Any", "Opted out", "Not opted out"])
    per_page = g[2].selectbox("Rows per page", [50, 100, 250, 500], index=1)
    m = pd.Series(True, index=df.index)
    if q:
        ql = q.lower()
        m &= df[["name", "email", "company", "title"]].apply(lambda c: c.str.lower().str.contains(ql, regex=False)).any(axis=1)
    if seg: m &= df.segment.isin(seg)
    if ver: m &= df.verification.isin(ver)
    if rel: m &= df.relevance.isin(rel)
    if camp: m &= df.campaign_status.isin(camp)
    if opt != "Any": m &= df.opted_out == (opt == "Opted out")
    flt = df[m]
    pages = max(1, -(-len(flt) // per_page))
    page = st.number_input("Page", 1, pages, 1) if pages > 1 else 1
    view = flt.iloc[(page - 1) * per_page: page * per_page]
    st.caption(f"{len(flt):,} contacts match ({len(df):,} total). Select rows to act on them.")
    show = view[["id", "name", "email", "title", "company", "segment", "verification", "relevance", "campaign_status", "reply_status"]]
    ids = select_rows(show, "contact_sel")
    st.markdown("##### Bulk actions " + (f"on {len(ids)} selected" if ids else "(select rows above)"))
    a = st.columns(5)
    rel_new = a[0].selectbox("Set relevance", ["—"] + RELEVANCE[1:], key="relnew")
    if a[0].button("Apply relevance", disabled=not ids or rel_new == "—"):
        with db_scope() as s:
            n = svc.bulk_update(s, ids, relevance=rel_new)
        st.success(f"Updated {n}"); st.rerun()
    if a[1].button("Mark Ready for Outreach", disabled=not ids, help="Confirms you reviewed these contacts. Invalid/suppressed ones are skipped."):
        with db_scope() as s:
            n = svc.bulk_update(s, ids, verification="Ready for Outreach")
        st.success(f"{n} marked ready (suppressed/invalid skipped)"); st.rerun()
    if a[2].button("Check domains (MX/DNS)", disabled=not ids, help="Checks the domain can receive mail. Does NOT prove the mailbox exists."):
        with st.spinner("Checking DNS…"), db_scope() as s:
            st.info(str(svc.run_domain_checks(s, ids)))
    if a[3].button("Suppress", disabled=not ids):
        with db_scope() as s:
            for c in s.query(Contact).filter(Contact.id.in_(ids)):
                suppress(s, c.email, "manual")
        st.success("Suppressed"); st.rerun()
    if a[4].button("Check ALL unreviewed domains"):
        with st.spinner("Checking DNS for all unreviewed contacts… this can take a minute"), db_scope() as s:
            st.info(str(svc.run_domain_checks(s)))
    if len(ids) == 1:
        st.divider()
        with db_scope() as s:
            c = s.get(Contact, ids[0])
            st.subheader(c.name or c.email)
            st.caption(f"{c.title} · {c.company} · {c.email} · source: {c.source}")
            notes = st.text_area("Internal notes", c.notes, key=f"notes_{c.id}")
            if st.button("Save notes"):
                c.notes = notes; st.success("Saved")
            hist = messages_frame(s)
            hist = hist[hist.email == c.email][["id", "kind", "status", "subject", "submitted", "reply"]]
        st.markdown("**Outreach history**")
        st.dataframe(hist, hide_index=True, width="stretch") if not hist.empty else st.caption("No emails for this contact yet.")


def render():
    header("Contacts", "Import, validate, review and segment your recruiter database.")
    t1, t2 = st.tabs(["Browse & review", "Import"])
    with t1: _browse_tab()
    with t2: _import_tab()
