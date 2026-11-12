"""Resume (PDF) management with content validation."""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from recruiter_os.config import MAX_RESUME_BYTES
from recruiter_os.models import ResumeFile
from recruiter_os.services.audit import audit


class ResumeError(ValueError):
    pass


def validate_pdf(data: bytes, original_name: str) -> None:
    if not original_name.lower().endswith(".pdf"):
        raise ResumeError("Only PDF files are accepted.")
    if not data:
        raise ResumeError("File is empty.")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeError(f"File exceeds {MAX_RESUME_BYTES // (1024 * 1024)} MB limit.")
    if not data.startswith(b"%PDF-"):
        raise ResumeError("File content is not a valid PDF (missing %PDF header).")
    if b"%%EOF" not in data[-2048:]:
        raise ResumeError("PDF appears truncated (no %%EOF marker).")


def add_resume(s: Session, resumes_dir: Path, data: bytes, original_name: str, label: str = "",
               role_category: str = "", make_default: bool = False) -> ResumeFile:
    validate_pdf(data, original_name)
    resumes_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(original_name).name)[:80]
    path = resumes_dir / f"{uuid.uuid4().hex[:10]}_{safe}"
    path.write_bytes(data)
    path.chmod(0o600)
    first = s.query(ResumeFile).filter_by(archived=False).count() == 0
    r = ResumeFile(label=label or Path(original_name).stem, original_name=Path(original_name).name, stored_path=str(path),
                   sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), role_category=role_category)
    s.add(r)
    s.flush()
    if make_default or first:
        set_default_resume(s, r.id)
    audit(s, "add_resume", "resume", r.id, name=r.original_name)
    return r


def set_default_resume(s: Session, rid: int) -> None:
    for r in s.query(ResumeFile).all():
        r.is_default = r.id == rid
    s.flush()


def replace_resume(s: Session, rid: int, resumes_dir: Path, data: bytes, original_name: str) -> ResumeFile:
    old = s.get(ResumeFile, rid)
    new = add_resume(s, resumes_dir, data, original_name, label=old.label, role_category=old.role_category,
                     make_default=old.is_default)
    old.archived, old.is_default = True, False
    return new


def resume_for(s: Session, rid: int | None, role_category: str = "") -> ResumeFile | None:
    """Resume is only attached if explicitly chosen (campaign/message). Never an implicit default."""
    return s.get(ResumeFile, rid) if rid else None


def read_resume_bytes(r: ResumeFile) -> bytes:
    p = Path(r.stored_path)
    if not p.exists():
        raise FileNotFoundError(f"Resume file missing on disk: {r.original_name}")
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != r.sha256:
        raise ResumeError("Resume file changed on disk since upload (checksum mismatch).")
    return data
