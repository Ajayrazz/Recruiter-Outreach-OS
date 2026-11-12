"""Renders every page headlessly (Streamlit AppTest) against a throwaway data dir; fails on any exception.
Run twice per page: with an empty database and with imported + reviewed contacts."""
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = [("dashboard", "render"), ("contacts", "render"), ("campaigns", "render"), ("generator", "render"), ("templates", "render"),
         ("queue", "render_queue"), ("queue", "render_scheduled"), ("queue", "render_sent"), ("replies", "render"), ("followups", "render"),
         ("deliverability", "render"), ("resumes", "render"), ("gmail_settings", "render"), ("profile", "render"), ("app_settings", "render")]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECRUITER_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECRUITER_OS_SECRET_KEY", Fernet.generate_key().decode())
    from recruiter_os.ui import common
    common.get_app.clear()
    yield
    common.get_app.clear()


def render(mod, fn):
    script = f"from recruiter_os.ui import common\ncommon.inject_css()\nfrom recruiter_os.ui.pages import {mod}\n{mod}.{fn}()\n"
    at = AppTest.from_string(script, default_timeout=60)
    at.run()
    return at


def seed():
    from recruiter_os.services import campaigns as camp, contacts as c, messages as msgs
    from recruiter_os.models import EmailTemplate
    from recruiter_os.ui.common import get_app
    app = get_app()
    df = c.read_table(ROOT / "data" / "imports" / "hr_contacts.csv")
    with app.db.scope() as s:
        c.import_contacts(s, df.head(40), c.auto_map_columns(list(df.columns)), "hr.csv")
        ids = [x.id for x in s.query(c.Contact).limit(10)]
        c.bulk_update(s, ids, verification="Ready for Outreach", relevance="Relevant")
        camp_ = camp.create_campaign(s, "Smoke", template_id=s.query(EmailTemplate).filter_by(category="A").one().id)
        camp.add_contacts(s, camp_, ids[:5])
        camp.generate_drafts(s, camp_, "me@gmail.com")


@pytest.mark.parametrize("mod,fn", PAGES)
def test_page_renders_empty(env, mod, fn):
    at = render(mod, fn)
    assert not at.exception, [e.value for e in at.exception]


@pytest.mark.skipif(not (ROOT / "data" / "imports" / "hr_contacts.csv").exists(), reason="sample CSV absent")
@pytest.mark.parametrize("mod,fn", PAGES)
def test_page_renders_with_data(env, mod, fn):
    seed()
    at = render(mod, fn)
    assert not at.exception, [e.value for e in at.exception]


def test_main_entrypoint_boots(env):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
