"""Environment-driven configuration. Secrets only ever come from env / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
HARD_DAILY_CEILING = 150  # absolute application ceiling, far below Gmail's account limits
MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB


def _resolve(p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_url: str
    client_secret_file: Path
    oauth_port: int
    secret_key: str | None
    test_mode: bool
    ai_provider: str
    ai_api_key: str | None
    log_level: str

    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir.parent / "logs" if self.data_dir.name == "data" else self.data_dir / "logs"


def load_config() -> Config:
    data_dir = _resolve(os.getenv("RECRUITER_OS_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        data_dir=data_dir,
        db_url=os.getenv("RECRUITER_OS_DB_URL", f"sqlite:///{data_dir / 'recruiter_os.db'}"),
        client_secret_file=_resolve(os.getenv("GOOGLE_CLIENT_SECRET_FILE", str(data_dir / "secrets" / "client_secret.json"))),
        oauth_port=int(os.getenv("OAUTH_LOCAL_PORT", "8765")),
        secret_key=os.getenv("RECRUITER_OS_SECRET_KEY") or None,
        test_mode=os.getenv("RECRUITER_OS_TEST_MODE", "true").lower() in ("1", "true", "yes"),
        ai_provider=os.getenv("AI_PROVIDER", "none").lower(),
        ai_api_key=os.getenv("AI_API_KEY") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
