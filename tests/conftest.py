from __future__ import annotations

from datetime import datetime

import pytest

from recruiter_os.bootstrap import App
from recruiter_os.config import Config
from recruiter_os.db import Database
from recruiter_os.models import Contact, GmailAccount, ResumeFile
from recruiter_os.secure_store import TokenStore
from recruiter_os.services import contacts as csvc
from recruiter_os.services import resumes as rsvc
from recruiter_os.services.settings import save_profile, set_setting
from recruiter_os.services.templates import seed_default_templates
from tests.fakes import FakeGmail

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
NOW = datetime(2026, 11, 16, 5, 0)  # Monday 10:30 IST


@pytest.fixture()
def app(tmp_path) -> App:
    cfg = Config(data_dir=tmp_path, db_url=f"sqlite:///{tmp_path/'t.db'}", client_secret_file=tmp_path / "cs.json", oauth_port=8765,
                 secret_key=None, test_mode=True, ai_provider="none", ai_api_key=None, log_level="WARNING")
    db = Database(cfg.db_url)
    from cryptography.fernet import Fernet
    store = TokenStore(tmp_path / "secrets", Fernet.generate_key().decode())
    a = App(cfg, db, store)
    with db.scope() as s:
        seed_default_templates(s)
        save_profile(s, {"name": "Test User", "education": "B.Tech CSE", "university": "Test University", "graduation_year": "2027",
                         "status": "Final-year CSE student", "linkedin": "https://linkedin.com/in/test", "github": "https://github.com/test",
                         "portfolio": "https://test.dev", "languages": ["Java", "Python", "C++"], "backend": ["Node.js", "REST APIs"],
                         "frontend": ["React.js"], "experience": [{"company": "Acme", "role": "SDE Intern", "tags": ["backend", "sde"],
                                                                   "points": ["built an OCR pipeline", "added RBAC"]}],
                         "projects": [{"name": "RouteX", "description": "a mini API gateway", "tags": ["backend", "sde"]}],
                         "achievements": [{"text": "solved 1,500+ DSA problems", "tags": ["dsa", "sde"]}]})
        set_setting(s, "test_mode", False)
        # wide-open window so tests are deterministic
        for k, v in dict(window_start="00:00", window_end="23:59", sending_days=[0, 1, 2, 3, 4, 5, 6], pause_between_emails_seconds=0,
                         daily_cap=50, batch_size=50, batch_interval_minutes=1, test_recipient="me@gmail.com").items():
            set_setting(s, k, v)
        s.add(GmailAccount(email="me@gmail.com", status="connected", scopes=["send"]))
    return a


@pytest.fixture()
def gmail() -> FakeGmail:
    return FakeGmail()


@pytest.fixture()
def make_contacts(app):
    def _make(n=3, ready=True):
        with app.db.scope() as s:
            ids = []
            for i in range(n):
                c = Contact(name=f"Person{i} Surname", email=f"p{i}@co{i}.com", email_normalized=f"p{i}@co{i}.com", title="Head of HR",
                            company=f"Company {i}", verification_status="Ready for Outreach" if ready else "Syntax Valid")
                s.add(c)
                s.flush()
                ids.append(c.id)
            return ids
    return _make


@pytest.fixture()
def resume_id(app):
    with app.db.scope() as s:
        return rsvc.add_resume(s, app.cfg.resumes_dir, PDF, "resume.pdf").id
