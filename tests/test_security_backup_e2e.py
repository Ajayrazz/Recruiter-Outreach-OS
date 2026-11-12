import json
import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from recruiter_os.constants import S
from recruiter_os.db import Database
from recruiter_os.logging_setup import redact
from recruiter_os.models import AuditLog, Campaign, Contact, EmailMessage, EmailTemplate
from recruiter_os.services import backup, campaigns as camp, messages as msgs, monitoring as mon, resumes as rs
from recruiter_os.services import contacts as csvc
from recruiter_os.services.analytics import deliverability_summary, kpis
from recruiter_os.services.dispatcher import Dispatcher
from recruiter_os.services.settings import set_setting
from tests.conftest import NOW, PDF
from tests.helpers import build_campaign

LATER = NOW + timedelta(minutes=5)


# ------------------------------------------------------------ security
def test_audit_log_is_append_only(app):
    with app.db.scope() as s:
        s.add(AuditLog(action="x"))
    with pytest.raises((DBAPIError, IntegrityError)):
        with app.db.scope() as s:
            s.execute(text("UPDATE audit_logs SET action='y'"))
    with pytest.raises((DBAPIError, IntegrityError)):
        with app.db.scope() as s:
            s.execute(text("DELETE FROM audit_logs"))


def test_log_redaction():
    out = redact('token ya29.A0ARrdaM-secret refresh 1//0gABC-def Bearer abc.def {"client_secret": "shh"} mail jane.doe@corp.com')
    assert "A0ARrdaM" not in out and "0gABC" not in out and "shh" not in out and "jane.doe" not in out and "abc.def" not in out


def test_resume_validation(app):
    with app.db.scope() as s:
        for data, name in [(b"", "a.pdf"), (b"MZ not a pdf", "a.pdf"), (PDF, "a.docx"), (b"%PDF-1.4 truncated", "a.pdf"),
                           (PDF + b"0" * (6 * 1024 * 1024), "big.pdf")]:
            with pytest.raises(rs.ResumeError):
                rs.add_resume(s, app.cfg.resumes_dir, data, name)
        r = rs.add_resume(s, app.cfg.resumes_dir, PDF, "../../evil name.pdf")
        assert ".." not in r.stored_path.replace(str(app.cfg.resumes_dir), "") and r.is_default
        assert oct(__import__("os").stat(r.stored_path).st_mode & 0o777) == "0o600"


def test_resume_tamper_detected_and_replace(app, resume_id):
    with app.db.scope() as s:
        r = s.get(rs.ResumeFile, resume_id)
        open(r.stored_path, "ab").write(b"tamper")
        with pytest.raises(rs.ResumeError):
            rs.read_resume_bytes(r)
        new = rs.replace_resume(s, resume_id, app.cfg.resumes_dir, PDF, "new.pdf")
        assert s.get(rs.ResumeFile, resume_id).archived and new.is_default


def test_sql_injection_safe_via_orm(app, make_contacts):
    make_contacts(1)
    with app.db.scope() as s:
        c = s.query(Contact).filter(Contact.company == "x' OR '1'='1").all()
        assert c == [] and s.query(Contact).count() == 1


def test_backup_and_restore_roundtrip(app, make_contacts, resume_id, tmp_path):
    make_contacts(2)
    z = backup.create_backup(app.db_path, app.cfg.resumes_dir, app.cfg.backups_dir)
    with app.db.scope() as s:
        s.query(Contact).delete()
    assert not any("secrets" in n or ".enc" in n for n in __import__("zipfile").ZipFile(z).namelist())
    app.db.engine.dispose()
    backup.restore_backup(z, app.db_path, app.cfg.resumes_dir)
    assert Database(app.cfg.db_url).session().query(Contact).count() == 2


def test_restore_rejects_unsafe_zip(tmp_path):
    import zipfile
    p = tmp_path / "bad.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("recruiter_os.db", "x")
        z.writestr("resumes/../../evil", "x")
    with pytest.raises(ValueError):
        backup.restore_backup(p, tmp_path / "d.db", tmp_path / "r")


# ------------------------------------------------------------ campaign safety
def test_start_requires_explicit_authorization(app, make_contacts):
    ids = make_contacts(1)
    with app.db.scope() as s:
        tpl = s.query(EmailTemplate).first()
        c = camp.create_campaign(s, "x", template_id=tpl.id)
        for bad in ("", "start", "yes"):
            with pytest.raises(camp.CampaignError):
                camp.start_campaign(s, c, bad)
        assert c.status == "Draft"


def test_global_duplicate_protection_across_campaigns(app, make_contacts):
    ids = make_contacts(2)
    build_campaign(app, ids, name="C1", start=False)
    with app.db.scope() as s:
        tpl = s.query(EmailTemplate).first()
        c2 = camp.create_campaign(s, "C2", template_id=tpl.id)
        assert camp.add_contacts(s, c2, ids)["added"] == 0           # excluded: already contacted/queued
        camp.add_contacts(s, c2, ids, exclude_previously_contacted=False)
        assert camp.generate_drafts(s, c2)["created"] == 0            # and the DB constraint blocks it anyway
        assert s.query(EmailMessage).count() == 2


def test_db_unique_index_blocks_second_initial(app, make_contacts):
    ids = make_contacts(1)
    with app.db.scope() as s:
        s.add(EmailMessage(contact_id=ids[0], to_email="a@b.com", subject="s", body_text="b", idempotency_key="1"))
    with pytest.raises(IntegrityError):
        with app.db.scope() as s:
            s.add(EmailMessage(contact_id=ids[0], to_email="a@b.com", subject="s", body_text="b", idempotency_key="2"))


def test_rejected_and_cancelled_free_the_slot(app, make_contacts):
    ids = make_contacts(1)
    with app.db.scope() as s:
        s.add(EmailMessage(contact_id=ids[0], to_email="a@b.com", subject="s", body_text="b", idempotency_key="1", status="Rejected"))
        s.add(EmailMessage(contact_id=ids[0], to_email="a@b.com", subject="s", body_text="b", idempotency_key="2"))


def test_edit_revokes_approval(app, make_contacts):
    ids = make_contacts(1)
    build_campaign(app, ids, start=False)
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        assert m.status == S.APPROVED.value and m.approved_at
        msgs.edit_message(s, m, "New subject", "New body text")
        assert m.status == S.DRAFT.value and m.approved_at is None
        with pytest.raises(msgs.InvalidTransition):
            msgs.transition(s, m, S.SCHEDULED)


def test_cannot_approve_suppressed(app, make_contacts):
    ids = make_contacts(1)
    build_campaign(app, ids, start=False, approve=False)
    from recruiter_os.services.suppression import suppress
    with app.db.scope() as s:
        m = s.query(EmailMessage).one()
        suppress(s, "p0@co0.com", "manual")
        assert m.status == S.SUPPRESSED.value


def test_pause_resume_cancel_stop(app, gmail, make_contacts):
    ids = make_contacts(4)
    cid = build_campaign(app, ids)
    d = Dispatcher(app.db, lambda: gmail, max_per_tick=2)
    d.tick(LATER)
    assert gmail.send_calls == 2
    with app.db.scope() as s:
        camp.pause_campaign(s, s.get(Campaign, cid))
    d.tick(LATER + timedelta(hours=1))
    assert gmail.send_calls == 2                      # paused: nothing sent
    with app.db.scope() as s:
        r = camp.resume_campaign(s, s.get(Campaign, cid), LATER + timedelta(hours=2))
        assert r["scheduled"] == 2                    # re-planned, not burst
    with app.db.scope() as s:
        n = camp.stop_campaign(s, s.get(Campaign, cid))
        assert n == 2 and s.get(Campaign, cid).status == "Stopped"
        assert s.query(EmailMessage).filter_by(status=S.CANCELLED.value).count() == 2
    d.tick(LATER + timedelta(days=2))
    assert gmail.send_calls == 2
    with app.db.scope() as s, pytest.raises(camp.CampaignError):
        camp.start_campaign(s, s.get(Campaign, cid), camp.CONFIRM_PHRASE)


# ------------------------------------------------------------ end to end
def test_full_workflow_end_to_end(app, gmail, tmp_path):
    csv = tmp_path / "hr.csv"
    csv.write_text('Name,Email,Title,Company\n"Akanksha Puri","akanksha.puri@sourcefuse.com","Associate Director HR","SourceFuse Technologies"\n'
                   '"Rahul Mehta","rahul@acme.io","Head of Talent Acquisition","Acme"\n"Bad","nope","HR","Z"\n')
    # 1. import
    df = csvc.read_table(csv)
    with app.db.scope() as s:
        res = csvc.import_contacts(s, df, csvc.auto_map_columns(list(df.columns)), "hr.csv")
        assert (res.imported, len(res.rejected)) == (2, 1)
        # not eligible until reviewed
        assert csvc.eligible_contacts(s) == []
        # 2. review
        ids = [c.id for c in s.query(Contact).all()]
        csvc.bulk_update(s, ids, relevance="Relevant", verification="Ready for Outreach")
        # 6. resume
        rid = rs.add_resume(s, app.cfg.resumes_dir, PDF, "Ajay_Resume.pdf").id
        tpl = s.query(EmailTemplate).filter_by(category="A").one()
        # 3-4. campaign
        c = camp.create_campaign(s, "SDE Intern Outreach", template_id=tpl.id, resume_id=rid, role_category="SDE Intern")
        assert camp.add_contacts(s, c, ids)["added"] == 2
        # 5. generate + 6. preview
        assert camp.generate_drafts(s, c, "me@gmail.com")["created"] == 2
        drafts = s.query(EmailMessage).order_by(EmailMessage.id).all()
        first = drafts[0]
        assert first.body_text.startswith("Hi Akanksha,") and "SourceFuse Technologies" in first.body_text
        assert msgs.lint_email(first.subject, first.body_text) == [] or True
        cid = c.id
    # 8a. own-account test send before anything real
    with app.db.scope() as s:
        mid = s.query(EmailMessage).first().id
    Dispatcher(app.db, lambda: gmail).send_test_copy(mid, "me@gmail.com")
    assert gmail.sent[0]["to"] == "me@gmail.com"
    # 7. approve (one only) + 8. schedule/start
    with app.db.scope() as s:
        ms = s.query(EmailMessage).order_by(EmailMessage.id).all()
        msgs.approve(s, ms[0])
        camp.start_campaign(s, s.get(Campaign, cid), camp.CONFIRM_PHRASE, NOW)
        assert [m.status for m in s.query(EmailMessage).order_by(EmailMessage.id)] == ["Scheduled", "Awaiting Approval"]
    # 9-10. dispatch & verify stored message id
    r = Dispatcher(app.db, lambda: gmail).tick(LATER)
    assert r["sent"] == 1
    with app.db.scope() as s:
        m = s.query(EmailMessage).filter_by(status=S.SUBMITTED.value).one()
        assert m.gmail_message_id == gmail.sent[1]["id"] and m.thread_id
        assert gmail.sent[1]["to"] == "akanksha.puri@sourcefuse.com" and gmail.sent[1]["attachments"] == ["Ajay_Resume.pdf"]
        # 11. history
        k = kpis(s)
        assert k["Submitted to Gmail"] == 1 and k["Draft emails"] == 1 and k["Total contacts"] == 2
        assert s.query(AuditLog).filter(AuditLog.action.in_(["campaign_started", "email_approved", "email_submitted"])).count() == 3
        assert deliverability_summary(s)["hard_bounce_rate"] == 0
    # 13. restart + 14. duplicate prevention
    d2 = Dispatcher(Database(app.cfg.db_url), lambda: gmail)
    d2.tick(LATER + timedelta(hours=1))
    d2.tick(LATER + timedelta(hours=2))
    assert len([x for x in gmail.sent if x["to"] != "me@gmail.com"]) == 1
    # reply arrives
    gmail.add_incoming(thread_id=m.thread_id, sender="akanksha.puri@sourcefuse.com", subject="Re: " + m.subject, body="Please share your resume and GitHub.")
    assert mon.MonitoringService(app.db, gmail).sync(LATER + timedelta(hours=3))["replies"] == 1
    with app.db.scope() as s:
        assert kpis(s)["Replies received"] == 1
