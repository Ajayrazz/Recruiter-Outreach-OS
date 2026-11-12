"""Template CRUD and seeding."""
from __future__ import annotations

from sqlalchemy.orm import Session

from recruiter_os.models import EmailTemplate
from recruiter_os.services.audit import audit
from recruiter_os.services.personalize import DEFAULT_TEMPLATES, TemplateError, render


def seed_default_templates(s: Session) -> int:
    n = 0
    for t in DEFAULT_TEMPLATES:
        if not s.query(EmailTemplate).filter_by(name=t["name"]).first():
            s.add(EmailTemplate(**t))
            n += 1
    s.flush()
    return n


def validate_template(subject: str, body: str) -> None:
    dummy = {"recruiter_name": "Alex", "company": "Acme", "role": "SDE Intern", "role_subject": "x", "original_subject": "x",
             "my_name": "Me", "me": {}, "education_line": "", "graduation_year": "", "skills": "", "experience": None,
             "projects": [], "achievements": "", "linkedin": "", "github": "", "portfolio": "", "recruiter_title": "", "role_title": ""}
    render(subject, dummy)
    render(body, dummy)


def save_template(s: Session, name: str, category: str, subject: str, body: str, tid: int | None = None,
                  is_followup: bool = False) -> EmailTemplate:
    if not name.strip() or not subject.strip() or not body.strip():
        raise TemplateError("Name, subject and body are required.")
    validate_template(subject, body)
    t = s.get(EmailTemplate, tid) if tid else EmailTemplate(name=name)
    t.name, t.category, t.subject, t.body, t.is_followup = name.strip(), category, subject, body, is_followup
    s.add(t)
    s.flush()
    audit(s, "save_template", "template", t.id)
    return t


def duplicate_template(s: Session, tid: int) -> EmailTemplate:
    src = s.get(EmailTemplate, tid)
    base, n = f"{src.name} (copy)", 1
    name = base
    while s.query(EmailTemplate).filter_by(name=name).first():
        n += 1
        name = f"{base} {n}"
    t = EmailTemplate(name=name, category=src.category, subject=src.subject, body=src.body, is_followup=src.is_followup)
    s.add(t)
    s.flush()
    return t


def set_default(s: Session, tid: int) -> None:
    t = s.get(EmailTemplate, tid)
    for o in s.query(EmailTemplate).filter_by(category=t.category):
        o.is_default = o.id == tid
    s.flush()


def delete_template(s: Session, tid: int) -> None:
    from recruiter_os.models import Campaign, EmailMessage
    if s.query(Campaign.id).filter_by(template_id=tid).first() or s.query(EmailMessage.id).filter_by(template_id=tid).first():
        raise ValueError("Template is used by campaigns/emails and cannot be deleted. Duplicate and edit instead.")
    s.delete(s.get(EmailTemplate, tid))
    s.flush()
