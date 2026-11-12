import streamlit as st

from recruiter_os.constants import S
from recruiter_os.models import Campaign, EmailMessage
from recruiter_os.services import messages as msgs
from recruiter_os.ui.common import badge, db_scope, empty, fmt_dt, get_app, guarded, header, messages_frame, select_rows, worker_status


def _worker_banner():
    txt, kind = worker_status()
    st.markdown("Background worker: " + badge(txt, kind), unsafe_allow_html=True)
    if kind != "ok":
        st.caption("Scheduled emails only send while the worker runs: `python -m recruiter_os.worker` (see README). "
                   "It cannot run while your Mac is asleep or powered off; missed slots are sent late only within the sending window and daily cap.")


def render_queue():
    header("Sending Queue", "Every email and its persistent status. Failures never disappear silently.")
    _worker_banner()
    with db_scope() as s:
        df = messages_frame(s)
    if df.empty:
        empty("The queue is empty", "Approve emails and start a campaign.")
        return
    counts = df.status.value_counts()
    st.markdown(" ".join(badge(f"{k}: {v}", "info") for k, v in counts.items()), unsafe_allow_html=True)
    st.write("")
    sel = st.multiselect("Filter by status", [x.value for x in S], default=[])
    v = df[df.status.isin(sel)] if sel else df
    ids = select_rows(v.drop(columns=["thread_id", "campaign_id"]), "queue_sel")
    a = st.columns(4)
    if a[0].button("Cancel selected", disabled=not ids):
        with db_scope() as s:
            for i in ids:
                guarded(msgs.cancel, s, s.get(EmailMessage, i))
        st.rerun()
    if a[1].button("Retry selected failures", disabled=not ids, help="Only failures that were not flagged permanent."):
        with db_scope() as s:
            for i in ids:
                m = s.get(EmailMessage, i)
                if m.status == S.FAILED.value and not m.error_permanent:
                    from recruiter_os.models import utcnow
                    msgs.transition(s, m, S.SCHEDULED, scheduled_at=utcnow(), attempts=0, last_error="")
        st.rerun()
    if a[2].button("Re-queue 'Delivery Unknown'", disabled=not ids, help="ONLY after you checked Gmail's Sent folder and the message is NOT there."):
        with db_scope() as s:
            from recruiter_os.models import utcnow
            for i in ids:
                m = s.get(EmailMessage, i)
                if m.status == S.UNKNOWN.value:
                    msgs.transition(s, m, S.SCHEDULED, actor="user", scheduled_at=utcnow(), attempts=0)
        st.rerun()
    if a[3].button("Run dispatcher once now", help="Same as one worker tick. Respects test mode, window, cap and approval."):
        ok, r = guarded(get_app().dispatcher().tick)
        if ok: st.info(str(r))


def render_scheduled():
    header("Scheduled Emails", "Approved emails with an assigned send slot, in send order.")
    _worker_banner()
    with db_scope() as s:
        df = messages_frame(s, [S.SCHEDULED.value])
        pending_cancel = {}
    if df.empty:
        empty("Nothing scheduled", "Approve emails, then Start/Resume a campaign.")
        return
    df = df.iloc[::-1]
    st.markdown(f"**Next:** {df.iloc[0]['scheduled']} → {df.iloc[0]['email']}  ·  **{len(df)}** scheduled")
    ids = select_rows(df[["id", "recruiter", "email", "company", "scheduled", "subject", "attempts", "error"]], "sched_sel")
    if st.button("Cancel selected", disabled=not ids):
        with db_scope() as s:
            for i in ids:
                guarded(msgs.cancel, s, s.get(EmailMessage, i))
        st.rerun()


def render_sent():
    header("Sent Emails", "Submitted means Gmail's API accepted the message. It does not prove inbox or Primary-tab delivery.")
    with db_scope() as s:
        df = messages_frame(s, [S.SUBMITTED.value, S.REPLIED.value, S.BOUNCED.value, S.UNKNOWN.value])
    if df.empty:
        empty("No emails submitted yet")
        return
    f = st.columns(3)
    q = f[0].text_input("Search")
    stat = f[1].multiselect("Status", sorted(df.status.unique()))
    kind = f[2].multiselect("Kind", ["initial", "followup"])
    v = df
    if q: v = v[v.recruiter.str.contains(q, case=False) | v.company.str.contains(q, case=False) | v.email.str.contains(q, case=False)]
    if stat: v = v[v.status.isin(stat)]
    if kind: v = v[v.kind.isin(kind)]
    st.dataframe(v.drop(columns=["campaign_id", "scheduled"]), hide_index=True, width="stretch")
    st.download_button("Export CSV", v.to_csv(index=False).encode(), "sent_emails.csv", "text/csv")
