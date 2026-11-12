"""Encrypted-at-rest storage for OAuth tokens.

Tokens are never put in ordinary DB columns. They are encrypted with Fernet; the key comes from
RECRUITER_OS_SECRET_KEY, else the macOS Keychain (via keyring), else a 0600 key file.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("recruiter_os.secure_store")
_SERVICE = "recruiter-outreach-os"


class SecretStoreError(RuntimeError):
    pass


def _load_key(secrets_dir: Path, env_key: str | None) -> bytes:
    if env_key:
        return env_key.encode()
    try:
        import keyring
        key = keyring.get_password(_SERVICE, "fernet-key")
        if not key:
            key = Fernet.generate_key().decode()
            keyring.set_password(_SERVICE, "fernet-key", key)
        return key.encode()
    except Exception as exc:  # no keychain backend available
        log.warning("keyring unavailable (%s); falling back to key file", type(exc).__name__)
    secrets_dir.mkdir(parents=True, exist_ok=True)
    kf = secrets_dir / ".fernet.key"
    if not kf.exists():
        fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(Fernet.generate_key())
    return kf.read_bytes().strip()


class TokenStore:
    def __init__(self, secrets_dir: Path, env_key: str | None = None):
        self.dir = secrets_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(_load_key(secrets_dir, env_key))

    def _path(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        return self.dir / f"{safe}.enc"

    def save(self, name: str, plaintext: str) -> str:
        p = self._path(name)
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self._fernet.encrypt(plaintext.encode()))
        os.replace(tmp, p)
        return str(p)

    def load(self, name: str) -> str | None:
        p = self._path(name)
        if not p.exists():
            return None
        try:
            return self._fernet.decrypt(p.read_bytes()).decode()
        except InvalidToken as exc:
            raise SecretStoreError("Stored token cannot be decrypted (key changed?). Reconnect Gmail.") from exc

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
