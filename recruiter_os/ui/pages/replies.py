import streamlit as st

from recruiter_os.constants import REPLY_CATEGORIES
from recruiter_os.models import Contact, EmailEvent, EmailMessage, GmailAccount
from recruiter_os.services.settings import get_setting
from recruiter_os.ui.common import db_scope, empty, fmt_dt, get_app, guarded, header


def render():
    header("Replies", "Detected from your Gmail threads. Classification is keyword-based; correct it if needed. Nothing is auto-replied.")
    with db_scope() as s:
        mon = get_setting(s, "gmail_monitoring_enabled")
        acct = s.query(GmailAccount).filter_by(status="connected").first()
    if not mon:
        st.warning("Reply & bounce monitoring is off. Enable it in Gmail Settings (it adds the read-only Gmail permission and needs a reconnect).")
    c = st.columns([1, 3])
    if c[0].button("Sync now", disabled=not (mon and acct)):
        with st.spinner("Syncing Gmail…"):
            ok, r = guarded(lambda: get_app().monitor().sync())
        if ok:
            (st.error if r.get("error") else st.success)(str(r))
    if acct:
        c[1].caption(f"Last sync: {fmt_dt(acct.last_sync_at) or 'never'}")
    with db_scope() as s:
        evs = (s.query(EmailEvent, EmailMessage, Contact).join(EmailMessage, EmailMessage.id == EmailEvent.message_id)
               .join(Contact, Contact.id == EmailMessage.contact_id).filter(EmailEvent.event_type == "reply")
               .order_by(EmailEvent.occurred_at.desc()).limit(500).all())
        rows = [{"id": e.id, "when": fmt_dt(e.occurred_at), "recruiter": c.name, "company": c.company, "category": e.detail.get("category"),
                 "snippet": e.detail.get("snippet", "")[:200], "thread": m.thread_id, "message_id": m.id, "contact_id": c.id,
                 "email": c.email} for e, m, c in evs]
    if not rows:
        empty("No replies detected yet", "Replies appear after a sync, once recruiters respond to sent emails.")
        return
    import pandas as pd
    df = pd.DataFrame(rows)
    st.bar_chart(df.category.value_counts())
    st.dataframe(df.drop(columns=["thread", "message_id", "contact_id"]), hide_index=True, width="stretch")
    st.markdown("##### Manage a reply")
    pick = st.selectbox("Reply", rows, format_func=lambda r: f"{r['when']} · {r['recruiter'] or r['email']} · {r['category']}")
    new_cat = st.selectbox("Correct category", REPLY_CATEGORIES, index=REPLY_CATEGORIES.index(pick["category"]) if pick["category"] in REPLY_CATEGORIES else 7)
    if st.button("Update category"):
        with db_scope() as s:
            m = s.get(EmailMessage, pick["message_id"]); m.reply_category = new_cat
            m.contact.reply_status = new_cat
        st.success("Updated"); st.rerun()
    st.markdown("**Draft a response manually** (not sent from here; copy it into Gmail)")
    st.text_area("Draft", f"Hi {pick['recruiter'].split()[0] if pick['recruiter'] else ''},\n\nThank you for getting back to me. \n\nBest regards,\n", height=180, key=f"rd_{pick['id']}")
    if pick["thread"]:
        st.link_button("Open thread in Gmail", f"https://mail.google.com/mail/u/0/#all/{pick['thread']}")
