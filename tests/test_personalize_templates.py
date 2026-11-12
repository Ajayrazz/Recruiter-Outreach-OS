import pytest

from recruiter_os.models import Campaign, Contact, EmailTemplate
from recruiter_os.services import messages as msgs
from recruiter_os.services import personalize as p
from recruiter_os.services import templates as t
from recruiter_os.services.campaigns import create_campaign
from recruiter_os.services.settings import get_profile


def test_greeting_name():
    assert p.greeting_name("Akanksha Puri") == "Akanksha"
    assert p.greeting_name("Dr. Rahul Sharma") == "Rahul"
    assert p.greeting_name("") == ""
    assert p.greeting_name("x") == ""
    assert p.greeting_name("hr@company.com") == ""


@pytest.fixture()
def tpl(app):
    with app.db.scope() as s:
        return s.query(EmailTemplate).filter_by(category="A").one().id


def render_for(app, tid, name="Akanksha Puri", company="SourceFuse Technologies", mode="A", role="SDE Intern"):
    with app.db.scope() as s:
        c = Contact(name=name, email="a@b.com", email_normalized="a@b.com", title="Associate Director HR", company=company)
        return msgs.compose(s, c, s.get(EmailTemplate, tid), role, mode)


def test_personalized_email_uses_recruiter_company_and_profile(app, tpl):
    subj, body = render_for(app, tpl)
    assert body.startswith("Hi Akanksha,") and "SourceFuse Technologies" in body
    assert "built an OCR pipeline" in body and "Test University" in body
    assert "https://github.com/test" in body and "Akanksha Puri" not in body.split("\n")[0]
    assert "{{" not in body


def test_missing_name_gets_neutral_greeting_and_missing_company(app, tpl):
    _, body = render_for(app, tpl, name="", company="")
    assert body.startswith("Hello,") and "your organisation" in body


def test_no_invented_facts_with_empty_profile(app, tpl):
    with app.db.scope() as s:
        c = Contact(name="Sam", email="a@b.com", email_normalized="a@b.com", title="HR", company="X")
        ctx = p.build_context({"name": "Only Name"}, {"name": "Sam", "title": "HR", "company": "X"}, "SDE Intern", "A")
        body = p.render(s.get(EmailTemplate, tpl).body, ctx)
    for forbidden in ("1,500", "LeetCode", "Exanor", "RouteX"):
        assert forbidden not in body


def test_role_specific_selection_differs(app):
    with app.db.scope() as s:
        prof = get_profile(s)
        prof["projects"].append({"name": "DreamDrift", "description": "an AI coaching app", "tags": ["ai"]})
        ai = p.build_context(prof, {"name": "A B", "title": "", "company": "X"}, "AI Engineer Intern", "C")
        be = p.build_context(prof, {"name": "A B", "title": "", "company": "X"}, "Backend Intern", "C")
    assert ai["projects"][0]["name"] == "DreamDrift" and be["projects"][0]["name"] == "RouteX"


def test_mode_b_and_c_render(app):
    with app.db.scope() as s:
        for cat, mode in (("B", "B"), ("C", "C")):
            tp = s.query(EmailTemplate).filter_by(category=cat).one()
            c = Contact(name="Lee Wong", email="a@b.com", email_normalized="a@b.com", title="HR", company="Co")
            subj, body = msgs.compose(s, c, tp, "Backend Intern", mode)
            assert "Co" in body and subj and "{" not in subj


def test_subject_for_role_template(app):
    with app.db.scope() as s:
        tp = s.query(EmailTemplate).filter_by(category="C").one()
        c = Contact(name="Lee", email="a@b.com", email_normalized="a@b.com", title="", company="Co")
        subj, _ = msgs.compose(s, c, tp, "Backend Intern", "C")
    assert subj == "Exploring Backend Engineering Internship Opportunities"


def test_template_sandbox_and_errors(app):
    with app.db.scope() as s:
        with pytest.raises(p.TemplateError):
            t.save_template(s, "bad", "A", "s", "{{ undefined_var }}")
        with pytest.raises(p.TemplateError):
            t.save_template(s, "evil", "A", "s", "{{ ''.__class__.__mro__ }}")
        with pytest.raises(p.TemplateError):
            t.save_template(s, "syntax", "A", "s", "{% if %}")


def test_template_crud(app):
    with app.db.scope() as s:
        tp = t.save_template(s, "mine", "A", "Hello {{ company }}", "Hi {{ recruiter_name }}")
        d = t.duplicate_template(s, tp.id)
        assert d.name == "mine (copy)"
        t.set_default(s, d.id)
        assert s.get(EmailTemplate, d.id).is_default and not s.get(EmailTemplate, tp.id).is_default
        t.delete_template(s, d.id)
        assert s.get(EmailTemplate, d.id) is None


def test_template_in_use_cannot_be_deleted(app, tpl):
    with app.db.scope() as s:
        create_campaign(s, "c", template_id=tpl)
        with pytest.raises(ValueError):
            t.delete_template(s, tpl)


def test_lint_flags_problems():
    w = msgs.lint_email("ACT NOW!!!", "click here https://bit.ly/x " * 3, False)
    joined = " ".join(w)
    assert "ALL CAPS" in joined and "shortener" in joined and "Spam-like" in joined
    good = "Hello, " + "word " * 130
    assert msgs.lint_email("Internship inquiry", good) == []
