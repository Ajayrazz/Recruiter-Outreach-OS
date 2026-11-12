import altair as alt
import pandas as pd
import streamlit as st

from recruiter_os.models import GmailAccount
from recruiter_os.services import analytics
from recruiter_os.services.settings import get_setting
from recruiter_os.ui.common import badge, db_scope, empty, hero, kpi, worker_status


def _chart(df: pd.DataFrame, x: str, y: str, color: str | None = None, kind: str = "bar", title: str = ""):
    if df.empty:
        empty("No data yet", "This chart fills in from real stored events.")
        return
    enc = {"x": alt.X(x, title=None), "y": alt.Y(y, title=None)}
    if color:
        enc["color"] = alt.Color(color, legend=alt.Legend(orient="bottom", title=None), scale=alt.Scale(range=["#8b7cf6", "#f472b6", "#38bdf8", "#34d399"]))
    base = alt.Chart(df, title=title)
    st.altair_chart((base.mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=None if color else "#8b7cf6") if kind == "bar"
                     else base.mark_line(point=True, color=None if color else "#a78bfa")).encode(**enc)
                    .properties(height=240), width="stretch")


def render():
    hero("Land your first internship", "one thoughtful email at a time", "Personalized, approved, scheduled and tracked. Real data only.")
    with db_scope() as s:
        test_mode = get_setting(s, "test_mode")
        acct = s.query(GmailAccount).first()
        k = analytics.kpis(s)
        outreach, replies, bounces = analytics.outreach_over_time(s), analytics.replies_over_time(s), analytics.bounce_trend(s)
        statuses, companies, comp = analytics.contact_status_counts(s), analytics.company_outreach(s), analytics.campaign_comparison(s)
    wtxt, wkind = worker_status()
    st.markdown(
        badge("TEST MODE: nothing is sent to recruiters" if test_mode else "LIVE: approved emails will be sent", "warn" if test_mode else "bad") + " &nbsp; " +
        badge(f"Gmail: {acct.status if acct else 'not connected'}", "ok" if acct and acct.status == "connected" else "warn") + " &nbsp; " +
        badge(f"Worker: {wtxt}", wkind), unsafe_allow_html=True)
    st.write("")
    items = list(k.items())
    for row in (items[:4], items[4:8], items[8:]):
        cols = st.columns(4)
        for c, (label, val) in zip(cols, row):
            c.markdown(kpi(label, f"{val:,}"), unsafe_allow_html=True)
        st.write("")
    if k["Total contacts"] == 0:
        empty("No contacts yet", "Go to Contacts → Import to load your recruiter CSV/XLSX.")
    st.caption("“Submitted to Gmail” means Gmail's API accepted the message. It does not mean it reached an inbox or the Primary tab.")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Outreach over time")
        _chart(outreach, "date:T", "count:Q")
    with c2:
        st.subheader("Replies over time")
        _chart(replies, "date:T", "count:Q")
    c3, c4 = st.columns(2)
    with c3:
        st.subheader("Bounce trend")
        _chart(bounces, "date:T", "count:Q", color="event_type:N")
    with c4:
        st.subheader("Contact statuses")
        _chart(statuses, "status:N", "count:Q")
    c5, c6 = st.columns(2)
    with c5:
        st.subheader("Company-wise outreach")
        if companies.empty:
            empty("No outreach yet")
        else:
            st.dataframe(companies, hide_index=True, width="stretch")
    with c6:
        st.subheader("Campaign comparison")
        if comp.empty:
            empty("No campaigns yet")
        else:
            st.dataframe(comp, hide_index=True, width="stretch")
    with st.expander("Metrics that are intentionally unavailable"):
        for name, why in analytics.UNAVAILABLE.items():
            st.markdown(f"**{name}**: {why}")
