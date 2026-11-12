import streamlit as st

from recruiter_os.constants import S
from recruiter_os.models import EmailMessage, Suppression
from recruiter_os.services import analytics, messages as msgs
from recruiter_os.services.settings import all_settings
from recruiter_os.ui.common import badge, db_scope, empty, header


def render():
    header("Deliverability", "What is observed, what is inferred, and what Gmail simply does not tell us.")
    with db_scope() as s:
        d = analytics.deliverability_summary(s)
        bt = analytics.bounce_trend(s)
        cfg = all_settings(s)
        drafts = s.query(EmailMessage).filter(EmailMessage.status.in_([S.AWAITING.value, S.DRAFT.value, S.APPROVED.value])).limit(400).all()
        issues = [(m.id, m.to_email, w) for m in drafts for w in msgs.lint_email(m.subject, m.body_text)]
        supp = s.query(Suppression).count()
    st.markdown("#### Observed")
    c = st.columns(5)
    c[0].metric("Submitted to Gmail", d["submitted"])
    c[1].metric("Hard bounces", d["hard_bounces"], delta=f"{d['hard_bounce_rate']:.1%}" if d["hard_bounce_rate"] is not None else "n/a", delta_color="inverse")
    c[2].metric("Policy rejections (5.7.x)", d["policy_rejections"])
    c[3].metric("API failures", d["api_failures"])
    c[4].metric("Delivery unknown", d["delivery_unknown"])
    if d["hard_bounce_rate"] and d["hard_bounce_rate"] > 0.05:
        st.error("Hard-bounce rate above 5%: pause, clean the list and verify addresses before sending more.")
    elif d["policy_rejections"]:
        st.warning("Provider policy rejections observed: pause and review content and volume before continuing.")
    st.caption("“No bounce observed” ≠ delivered to inbox. Bounces are only detected when monitoring is enabled and Gmail receives the notice.")
    st.markdown("#### Bounce trend")
    if bt.empty:
        empty("No bounces observed")
    else:
        st.bar_chart(bt.pivot_table(index="date", columns="event_type", values="count", fill_value=0))
    st.markdown("#### Content quality of pending emails")
    if issues:
        import pandas as pd
        st.dataframe(pd.DataFrame(issues, columns=["email id", "to", "warning"]), hide_index=True, width="stretch")
    else:
        st.success("No lint warnings on pending drafts.")
    st.markdown("#### Current sending posture")
    st.markdown(f"Daily cap **{cfg['daily_cap']}** · batch **{cfg['batch_size']}** every **{cfg['batch_interval_minutes']} min** · window {cfg['window_start']}–{cfg['window_end']} · suppression list **{supp}**")
    if cfg["batch_size"] >= 50:
        st.warning("A batch of 50 in one go is aggressive for a personal Gmail account. A long interval does not make it safe. Prefer small pilots.")
    st.markdown("#### What you should know")
    st.markdown("""
- **Primary vs Promotions vs Spam is decided by the recipient's provider and their behavior.** No application can force Primary placement, and this one does not try.
- Sending uses Google's own authentication for your gmail.com address (SPF/DKIM/DMARC are Google's). There is nothing to configure without a custom domain.
- What helps legitimately: accurate subjects, individually personalized text, a real relevance to the recipient, a small pilot, a conservative daily cap, removing bounced/opted-out addresses, and stopping when problems appear.
- Not used, by design: tracking pixels, rotating senders, randomized delays to look "human", fake engagement, or any filter evasion.
- Replies you receive are the strongest positive signal; they are tracked on the Replies page.
- Not available from Gmail: open rates, tab placement, spam-folder placement, complaint data. Those are never estimated here.
""")
    with st.expander("Optional: custom-domain setup (SPF, DKIM, DMARC)"):
        st.markdown("""If you later own a domain (e.g. via Google Workspace): 1) add Google's SPF include (`v=spf1 include:_spf.google.com ~all`);
2) generate and publish the DKIM key in the Workspace Admin console (Apps → Gmail → Authenticate email); 3) publish a DMARC record such as
`v=DMARC1; p=none; rua=mailto:you@yourdomain.com` and tighten gradually; 4) use Google Postmaster Tools to monitor reputation. This app cannot and does not change DNS.""")
