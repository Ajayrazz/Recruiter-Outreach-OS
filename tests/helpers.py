from recruiter_os.constants import S
from recruiter_os.models import EmailMessage
from recruiter_os.services import campaigns as camp
from recruiter_os.services import messages as msgs
from recruiter_os.models import EmailTemplate
from tests.conftest import NOW


def build_campaign(app, ids, resume_id=None, name="C1", start=True, approve=True):
    with app.db.scope() as s:
        tpl = s.query(EmailTemplate).filter_by(category="A").one()
        c = camp.create_campaign(s, name, template_id=tpl.id, resume_id=resume_id)
        camp.add_contacts(s, c, ids)
        camp.generate_drafts(s, c, "me@gmail.com")
        cid = c.id
        if approve:
            for m in s.query(EmailMessage).filter_by(campaign_id=cid).all():
                msgs.approve(s, m)
        if start:
            camp.start_campaign(s, c, camp.CONFIRM_PHRASE, NOW)
    return cid
