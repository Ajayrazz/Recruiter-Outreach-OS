from datetime import timedelta

import pytest

from recruiter_os.constants import S
from recruiter_os.models import Campaign, Contact, EmailEvent, EmailMessage, FollowUp, GmailAccount, Suppression
from recruiter_os.services import campaigns as camp
from recruiter_os.services import followups
from recruiter_os.services import messages as msgs
from recruiter_os.services import monitoring as mon
from recruiter_os.services.dispatcher import Dispatcher
from tests.conftest import NOW
from tests.helpers import build_campaign

LATER = NOW + timedelta(minutes=5)

GMAIL_HARD = """Delivery Status Notification (Failure)
Address not found
Your message wasn't delivered to p0@co0.com because the address couldn't be found, or is unable to receive mail.
The response was:
550 5.1.1 The email account that you tried to reach does not exist."""
RFC_DSN = """Subject: Undeliverable
Final-Recipient: rfc822; p1@co1.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown"""
SOFT = """Delivery Status Notification (Delay)
Your message wasn't delivered to p0@co0.com yet.
Status: 4.2.2 mailbox full, will retry"""
POLICY = """Mail delivery failed
Final-Recipient: rfc822; p0@co0.com
Status: 5.7.1
550 5.7.1 message rejected as spam"""


def test_parse_hard_bounce_gmail_format():
    b = mon.parse_bounce(GMAIL_HARD)
    assert b.recipient == "p0@co0.com" and b.kind == "hard" and b.status_code == "5.1.1"


def test_parse_rfc3464():
    b = mon.parse_bounce(RFC_DSN)
    assert (b.recipient, b.kind) == ("p1@co1.com", "hard")


def test_parse_soft_and_policy():
    assert mon.parse_bounce(SOFT).kind == "soft"
    assert mon.parse_bounce(POLICY).kind == "policy"
    assert mon.parse_bounce("Hi, lunch tomorrow?") is None


@pytest.mark.parametrize("body,exp", [
    ("Thanks for reaching out. Please share your resume.", "Request for Resume"),
    ("We are not hiring interns at the moment.", "Not Interested"),
    ("Unfortunately we are not in a position to move forward.", "Rejection"),
    ("I am out of office until Monday.", "Out of Office"),
    ("You can apply here: https://company.com/careers/123", "Opportunity Shared"),
    ("Could you share more details about your projects?", "Request for More Information"),
    ("Thank you, I will forward your profile to the team.", "Positive Response"),
    ("ok", "Unclassified")])
def test_reply_classification(body, exp):
    assert mon.classify_reply("Re: Internship", body) == exp


def test_auto_submitted_header_is_out_of_office():
    assert mon.classify_reply("Re: x", "I'll respond soon", {"Auto-Submitted": "auto-replied"}) == "Out of Office"


def test_quoted_text_ignored():
    body = "Not interested right now.\n\nOn Mon, Nov 16, Me <me@gmail.com> wrote:\n> please share resume"
    assert mon.classify_reply("Re", body) == "Not Interested"


def sent_setup(app, gmail, make_contacts, n=2):
    build_campaign(app, make_contacts(n))
    Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert gmail.send_calls == n
    return gmail


def test_bounce_sync_marks_bounced_suppresses_and_is_idempotent(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts)
    tid, to = gmail.sent[0]["threadId"], gmail.sent[0]["to"]
    gmail.add_incoming(thread_id=tid, sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                       subject="Delivery Status Notification (Failure)", body=GMAIL_HARD.replace("p0@co0.com", to))
    ms = mon.MonitoringService(app.db, gmail)
    assert ms.sync(LATER)["bounces"] == 1
    assert ms.sync(LATER)["bounces"] == 0  # idempotent
    with app.db.scope() as s:
        bounced = s.query(EmailMessage).filter_by(status=S.BOUNCED.value).one()
        assert bounced.bounce_type == "hard"
        assert s.query(Suppression).filter_by(reason="hard_bounce").count() == 1
        assert s.query(EmailEvent).filter_by(event_type="bounce_hard").count() == 1
        assert s.query(GmailAccount).one().last_sync_at is not None


def test_bounce_matched_by_recipient_when_thread_differs(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts)
    gmail.add_incoming(thread_id="other", sender="mailer-daemon@x.com", subject="Undeliverable", body=RFC_DSN)
    assert mon.MonitoringService(app.db, gmail).sync(LATER)["bounces"] == 1
    with app.db.scope() as s:
        assert s.query(EmailMessage).filter_by(to_email="p1@co1.com").one().status == S.BOUNCED.value
        assert s.query(EmailMessage).filter_by(to_email="p0@co0.com").one().status == S.SUBMITTED.value


def test_soft_bounce_does_not_suppress(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts)
    to = gmail.sent[0]["to"]
    gmail.add_incoming(thread_id=gmail.sent[0]["threadId"], sender="mailer-daemon@x.com", subject="Delay", body=SOFT.replace("p0@co0.com", to))
    mon.MonitoringService(app.db, gmail).sync(LATER)
    with app.db.scope() as s:
        assert s.query(Suppression).count() == 0
        assert s.query(EmailMessage).filter_by(bounce_type="soft").count() == 1
        assert s.query(EmailMessage).filter_by(status=S.BOUNCED.value).count() == 0


def test_reply_matching_updates_contact_and_status(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts)
    gmail.add_incoming(thread_id=gmail.sent[1]["threadId"], sender="Person1 <p1@co1.com>", subject="Re: Internship",
                       body="Please share your resume.")
    gmail.add_incoming(thread_id="unrelated", sender="spam@x.com", subject="Hello", body="buy")
    assert mon.MonitoringService(app.db, gmail).sync(LATER)["replies"] == 1
    with app.db.scope() as s:
        m = s.query(EmailMessage).filter_by(to_email="p1@co1.com").one()
        assert m.status == S.REPLIED.value and m.reply_category == "Request for Resume"
        assert m.contact.reply_status == "Request for Resume"
        assert s.query(EmailMessage).filter_by(status=S.SUBMITTED.value).count() == 1


def test_out_of_office_is_not_a_reply(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts, 1)
    gmail.add_incoming(thread_id=gmail.sent[0]["threadId"], sender="p0@co0.com", subject="Automatic reply", body="I am out of office.")
    mon.MonitoringService(app.db, gmail).sync(LATER)
    with app.db.scope() as s:
        assert s.query(EmailMessage).one().status == S.SUBMITTED.value


def test_opt_out_reply_suppresses(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts, 1)
    gmail.add_incoming(thread_id=gmail.sent[0]["threadId"], sender="p0@co0.com", subject="Re", body="Please remove me from your list.")
    mon.MonitoringService(app.db, gmail).sync(LATER)
    with app.db.scope() as s:
        assert s.query(Contact).one().opted_out and s.query(Suppression).filter_by(reason="opt_out").count() == 1


def test_sync_failure_is_recorded_not_swallowed(app, gmail, make_contacts):
    def boom(*a, **k):
        raise RuntimeError("network down")
    gmail.list_ids = boom
    r = mon.MonitoringService(app.db, gmail).sync(LATER)
    assert "network down" in r["error"]
    with app.db.scope() as s:
        assert "network down" in s.query(GmailAccount).one().last_error


# ----------------------------- follow-ups
def test_followup_waiting_period_and_idempotent(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts, 2)
    with app.db.scope() as s:
        assert followups.refresh_followups(s, LATER + timedelta(days=3)) == 0
        assert followups.refresh_followups(s, LATER + timedelta(days=9)) == 2
        assert followups.refresh_followups(s, LATER + timedelta(days=9)) == 0


def test_no_followup_after_reply_bounce_or_optout(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts, 3)
    with app.db.scope() as s:
        ms = s.query(EmailMessage).order_by(EmailMessage.id).all()
        ms[0].contact.reply_status = "Positive Response"
        ms[1].contact.opted_out = True
        ms[2].status = S.BOUNCED.value
        assert followups.refresh_followups(s, LATER + timedelta(days=30)) == 0


def test_followup_limit_and_manual_approval(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts, 1)
    later = LATER + timedelta(days=9)
    with app.db.scope() as s:
        followups.refresh_followups(s, later)
        fu = s.query(FollowUp).one()
        m = followups.draft_followup(s, fu.id)
        assert m.status == S.AWAITING.value and m.thread_id == gmail.sent[0]["threadId"]
        assert "no problem" in m.body_text.lower()
    Dispatcher(app.db, lambda: gmail).tick(later + timedelta(minutes=5))
    assert gmail.send_calls == 1  # unapproved follow-up is never sent
    with app.db.scope() as s:
        msgs.approve(s, s.query(EmailMessage).filter_by(kind="followup").one())
        camp.schedule_approved(s, s.query(Campaign).one(), later)
    Dispatcher(app.db, lambda: gmail).tick(later + timedelta(minutes=5))
    assert gmail.send_calls == 2
    with app.db.scope() as s:
        assert followups.refresh_followups(s, later + timedelta(days=40)) == 0  # max_followups=1 reached


def test_cleanup_dismisses_stale_reminders(app, gmail, make_contacts):
    sent_setup(app, gmail, make_contacts, 1)
    with app.db.scope() as s:
        followups.refresh_followups(s, LATER + timedelta(days=9))
        s.query(Contact).one().reply_status = "Rejection"
        assert followups.cleanup_stale(s) == 1
        with pytest.raises(ValueError):
            followups.draft_followup(s, s.query(FollowUp).one().id)
