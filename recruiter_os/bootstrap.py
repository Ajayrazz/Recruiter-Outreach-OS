"""Application container: wires config, DB, token store, Gmail transport and seed data."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from recruiter_os.config import PROJECT_ROOT, Config, load_config
from recruiter_os.db import Database
from recruiter_os.logging_setup import setup_logging
from recruiter_os.models import GmailAccount, UserProfile
from recruiter_os.secure_store import TokenStore
from recruiter_os.services.dispatcher import Dispatcher
from recruiter_os.services.gmail import AuthGmailError, GmailService
from recruiter_os.services.monitoring import MonitoringService
from recruiter_os.services.settings import save_profile, set_setting
from recruiter_os.services.templates import seed_default_templates


@dataclass
class App:
    cfg: Config
    db: Database
    store: TokenStore

    def transport(self) -> GmailService:
        with self.db.scope() as s:
            acct = s.query(GmailAccount).filter_by(status="connected").first()
            if not acct:
                raise AuthGmailError("Gmail is not connected.")
            email = acct.email
        return GmailService(self.cfg, self.store, email)

    def dispatcher(self) -> Dispatcher:
        return Dispatcher(self.db, self.transport)

    def monitor(self) -> MonitoringService:
        return MonitoringService(self.db, self.transport())

    @property
    def db_path(self) -> Path:
        return Path(self.db.url.replace("sqlite:///", ""))


def create_app(cfg: Config | None = None, db: Database | None = None, store: TokenStore | None = None) -> App:
    cfg = cfg or load_config()
    setup_logging(cfg.logs_dir, cfg.log_level)
    db = db or Database(cfg.db_url)
    store = store or TokenStore(cfg.secrets_dir, cfg.secret_key)
    with db.scope() as s:
        seed_default_templates(s)
        seed = PROJECT_ROOT / "seed" / "profile.json"
        if not s.query(UserProfile).first() and seed.exists():
            save_profile(s, json.loads(seed.read_text()))
        from recruiter_os.models import AppSetting
        if not s.get(AppSetting, "test_mode"):
            set_setting(s, "test_mode", cfg.test_mode)
    return App(cfg, db, store)
