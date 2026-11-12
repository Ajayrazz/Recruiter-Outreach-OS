"""Recruiter Outreach OS: Streamlit entrypoint.  Run:  streamlit run app.py"""
import streamlit as st

st.set_page_config(page_title="Recruiter Outreach OS", page_icon="📬", layout="wide")

from recruiter_os.ui import common  # noqa: E402
from recruiter_os.ui.pages import (app_settings, campaigns, contacts, dashboard, deliverability, followups, generator,  # noqa: E402
                                   gmail_settings, profile, queue, replies, resumes, templates)

common.inject_css()
st.logo(str(common.ASSETS / "logo.svg"), size="large")
common.get_app()

pages = {
    "Overview": [st.Page(dashboard.render, title="Dashboard", icon=":material/dashboard:", default=True)],
    "Outreach": [
        st.Page(contacts.render, title="Contacts", icon=":material/contacts:", url_path="contacts"),
        st.Page(campaigns.render, title="Campaigns", icon=":material/campaign:", url_path="campaigns"),
        st.Page(generator.render, title="Email Generator", icon=":material/edit_note:", url_path="generator"),
        st.Page(templates.render, title="Email Templates", icon=":material/description:", url_path="templates"),
    ],
    "Sending": [
        st.Page(queue.render_queue, title="Sending Queue", icon=":material/queue:", url_path="queue"),
        st.Page(queue.render_scheduled, title="Scheduled Emails", icon=":material/schedule:", url_path="scheduled"),
        st.Page(queue.render_sent, title="Sent Emails", icon=":material/outbox:", url_path="sent"),
    ],
    "Monitoring": [
        st.Page(replies.render, title="Replies", icon=":material/mark_email_read:", url_path="replies"),
        st.Page(followups.render, title="Follow-ups", icon=":material/reply:", url_path="followups"),
        st.Page(deliverability.render, title="Deliverability", icon=":material/verified:", url_path="deliverability"),
    ],
    "Settings": [
        st.Page(resumes.render, title="Resume Manager", icon=":material/picture_as_pdf:", url_path="resumes"),
        st.Page(gmail_settings.render, title="Gmail Settings", icon=":material/mail_lock:", url_path="gmail"),
        st.Page(profile.render, title="Profile Settings", icon=":material/person:", url_path="profile"),
        st.Page(app_settings.render, title="Application Settings", icon=":material/settings:", url_path="settings"),
    ],
}
st.navigation(pages).run()
