"""Structured (JSON-lines) logging with sensitive-data redaction."""
from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    (re.compile(r"ya29\.[\w\-.]+"), "[REDACTED_ACCESS_TOKEN]"),
    (re.compile(r"1//[\w\-.]+"), "[REDACTED_REFRESH_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[\w\-.]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\"?(?:refresh_token|access_token|client_secret|api_key|password)\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1[REDACTED]"),
    (re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"), r"\1***\2"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                   "logger": record.name, "msg": redact(record.getMessage())}
        extra = getattr(record, "ctx", None)
        if extra:
            payload["ctx"] = json.loads(redact(json.dumps(extra, default=str)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload)


_configured = False


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("recruiter_os")
    root.setLevel(level)
    fh = RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(RedactingJsonFormatter())
    sh = logging.StreamHandler()
    sh.setFormatter(RedactingJsonFormatter())
    root.addHandler(fh)
    root.addHandler(sh)
    root.propagate = False
    _configured = True
