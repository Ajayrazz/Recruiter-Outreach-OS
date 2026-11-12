"""In-memory Gmail transport for tests. No network, no real email."""
from __future__ import annotations

import base64
import email
import itertools
from email import policy
from typing import Any

from recruiter_os.services.gmail import GmailError


class FakeGmail:
    def __init__(self, address: str = "me@gmail.com"):
        self.address = address
        self.sent: list[dict[str, Any]] = []
        self.inbox: list[dict[str, Any]] = []   # messages we pretend arrived
        self._ids = itertools.count(1)
        self.fail_with: list[Exception] = []     # popped one per send attempt
        self.send_calls = 0

    def profile_email(self) -> str:
        return self.address

    def send_raw(self, raw: str, thread_id: str | None = None) -> dict[str, str]:
        self.send_calls += 1
        if self.fail_with:
            raise self.fail_with.pop(0)
        mime = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        n = next(self._ids)
        rec = {"id": f"m{n}", "threadId": thread_id or f"t{n}", "mime": mime, "to": mime["To"], "subject": mime["Subject"],
               "message_id": mime["Message-ID"], "attachments": [p.get_filename() for p in mime.iter_attachments()]}
        self.sent.append(rec)
        return {"id": rec["id"], "threadId": rec["threadId"]}

    def find_by_rfc822_id(self, rfc822_id: str) -> dict[str, str] | None:
        for r in self.sent:
            if r["message_id"] == rfc822_id:
                return {"id": r["id"], "threadId": r["threadId"]}
        return None

    def add_incoming(self, *, thread_id: str, sender: str, subject: str, body: str, headers: dict[str, str] | None = None) -> str:
        mid = f"in{next(self._ids)}"
        hdrs = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}] + \
               [{"name": k, "value": v} for k, v in (headers or {}).items()]
        self.inbox.append({"id": mid, "threadId": thread_id, "sender": sender.lower(), "payload": {
            "headers": hdrs, "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}})
        return mid

    def list_ids(self, query: str, max_results: int = 200) -> list[dict[str, str]]:
        bounce = "mailer-daemon" in query and "-from:mailer-daemon" not in query
        out = []
        for m in self.inbox:
            is_b = "mailer-daemon" in m["sender"] or "postmaster" in m["sender"]
            if is_b == bounce:
                out.append({"id": m["id"], "threadId": m["threadId"]})
        return out[:max_results]

    def get_message(self, msg_id: str, fmt: str = "full") -> dict[str, Any]:
        for m in self.inbox:
            if m["id"] == msg_id:
                return m
        raise GmailError("not found")
