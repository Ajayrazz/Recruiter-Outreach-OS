"""Backup/restore of the database and resume files. OAuth tokens are never included."""
from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


def create_backup(db_path: Path, resumes_dir: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    out = backups_dir / f"backup_{datetime.now():%Y%m%d_%H%M%S}.zip"
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "recruiter_os.db"
        src, dst = sqlite3.connect(db_path), sqlite3.connect(snap)
        with dst:
            src.backup(dst)  # consistent online snapshot, safe while the app is running
        src.close()
        dst.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "recruiter_os.db")
            if resumes_dir.exists():
                for f in resumes_dir.iterdir():
                    if f.is_file():
                        z.write(f, f"resumes/{f.name}")
    out.chmod(0o600)
    return out


def restore_backup(zip_path: Path, db_path: Path, resumes_dir: Path) -> None:
    """Restore. Stop the worker and the app first. The current DB is kept as .pre-restore."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "recruiter_os.db" not in names:
            raise ValueError("Not a valid Recruiter Outreach OS backup")
        for n in names:
            if n != "recruiter_os.db" and (n.startswith("/") or ".." in Path(n).parts or not n.startswith("resumes/")):
                raise ValueError(f"Unsafe path in backup: {n}")
        if db_path.exists():
            db_path.replace(db_path.with_suffix(".db.pre-restore"))
        for ext in ("-wal", "-shm"):
            Path(str(db_path) + ext).unlink(missing_ok=True)
        with z.open("recruiter_os.db") as src, open(db_path, "wb") as dst:
            dst.write(src.read())
        resumes_dir.mkdir(parents=True, exist_ok=True)
        for n in names:
            if n.startswith("resumes/") and not n.endswith("/"):
                (resumes_dir / Path(n).name).write_bytes(z.read(n))
