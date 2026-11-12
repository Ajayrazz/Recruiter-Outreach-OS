"""Email validation and normalization (syntax only; no network)."""
from __future__ import annotations

from email_validator import EmailNotValidError, validate_email

_GMAIL = {"gmail.com", "googlemail.com"}


def validate_syntax(raw: str) -> tuple[str | None, str | None]:
    """Return (cleaned_email, error). Syntax validity does not imply the mailbox exists."""
    raw = (raw or "").strip().strip("<>").replace("mailto:", "").strip()
    if not raw:
        return None, "missing email"
    try:
        v = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        return None, f"invalid syntax: {exc}"
    return v.normalized, None


def normalize_email(raw: str) -> str | None:
    """Canonical form used for duplicate comparison and suppression."""
    cleaned, err = validate_syntax(raw)
    if err or not cleaned:
        return None
    local, _, domain = cleaned.lower().partition("@")
    if domain in _GMAIL:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"
