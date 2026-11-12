"""Gmail integration: OAuth, MIME construction, sending, search. Uses only the official Gmail API."""
from __future__ import annotations

import base64
import html
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid
from typing import Any, Protocol

from recruiter_os.config import GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE, Config
from recruiter_os.models import GmailAccount, utcnow
from recruiter_os.secure_store import TokenStore

log = logging.getLogger("recruiter_os.gmail")


# ------------------------------------------------------------------ errors
class GmailError(Exception):
    permanent = False


class TransientGmailError(GmailError):
    """Network blip, 5xx, rate limit: safe to retry with backoff."""


class PermanentGmailError(GmailError):
    """Bad recipient / malformed message: never auto-retried."""
    permanent = True


class AuthGmailError(GmailError):
    """Expired/revoked credentials: requires user to reconnect. Not a per-message failure."""


class QuotaGmailError(TransientGmailError):
    """Gmail told us to slow down: campaigns are paused rather than retried blindly."""


def classify_http_error(status: int, reason: str, message: str = "") -> GmailError:
    reason = (reason or "").lower()
    if status == 401 or reason in ("autherror", "invalidcredentials"):
        return AuthGmailError(message or "Gmail authentication failed")
    if reason in ("ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded", "quotaexceeded") or status == 429:
        return QuotaGmailError(message or "Gmail rate/quota limit hit")
    if status == 403:
        return AuthGmailError(message or "Gmail permission denied (scope missing or access revoked)")
    if status in (500, 502, 503, 504):
        return TransientGmailError(message or f"Gmail server error {status}")
    return PermanentGmailError(message or f"Gmail rejected the request ({status} {reason})")


# ------------------------------------------------------------------ MIME
_URL = re.compile(r"(https?://[^\s<>\"']+[^\s<>\"'.,;:)])")


def text_to_html(text: str) -> str:
    """Minimal, safe HTML: escaped text, line breaks, links whose visible text equals the href."""
    esc = html.escape(text)
    esc = _URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', esc)
    paras = [p.replace("\n", "<br>\n") for p in esc.strip().split("\n\n")]
    body = "".join(f"<p>{p}</p>" for p in paras)
    return f'<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">{body}</body></html>'


def new_rfc822_id(domain: str = "mail.gmail.com") -> str:
    return make_msgid(idstring=uuid.uuid4().hex[:16], domain=domain)


def build_mime(*, sender: str, sender_name: str, to: str, subject: str, body_text: str, rfc822_id: str,
               attachment: tuple[str, bytes] | None = None, in_reply_to: str | None = None,
               references: str | None = None) -> MimeMessage:
    for hdr_val in (to, subject, sender):  # header-injection guard
        if "\n" in hdr_val or "\r" in hdr_val:
            raise PermanentGmailError("Header injection attempt blocked")
    m = MimeMessage()
    m["From"] = formataddr((sender_name, sender)) if sender_name else sender
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = rfc822_id
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = references or in_reply_to
    m.set_content(body_text)
    m.add_alternative(text_to_html(body_text), subtype="html")
    if attachment:
        m.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    return m


def mime_to_raw(m: MimeMessage) -> str:
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


# ------------------------------------------------------------------ transport interface
class GmailTransport(Protocol):
    def profile_email(self) -> str: ...
    def send_raw(self, raw: str, thread_id: str | None = None) -> dict[str, str]: ...
    def find_by_rfc822_id(self, rfc822_id: str) -> dict[str, str] | None: ...
    def list_ids(self, query: str, max_results: int = 200) -> list[dict[str, str]]: ...
    def get_message(self, msg_id: str, fmt: str = "full") -> dict[str, Any]: ...


def header(msg: dict[str, Any], name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def message_text(msg: dict[str, Any]) -> str:
    """Extract the text/plain body (falls back to stripped HTML)."""
    def walk(part: dict[str, Any]) -> list[tuple[str, str]]:
        out = []
        data = part.get("body", {}).get("data")
        if data and part.get("mimeType", "").startswith("text/"):
            out.append((part["mimeType"], base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")))
        for p in part.get("parts", []) or []:
            out += walk(p)
        return out
    parts = walk(msg.get("payload", {}))
    for mt, txt in parts:
        if mt == "text/plain":
            return txt
    for mt, txt in parts:
        return re.sub(r"<[^>]+>", " ", txt)
    return msg.get("snippet", "")


# ------------------------------------------------------------------ real service
class GmailService:
    """Real Gmail API client. Credentials live encrypted in TokenStore, refreshed automatically."""

    def __init__(self, cfg: Config, store: TokenStore, account_email: str):
        self.cfg, self.store, self.account_email = cfg, store, account_email
        self._svc = None

    def _credentials(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        blob = self.store.load(token_name(self.account_email))
        if not blob:
            raise AuthGmailError("Gmail is not connected.")
        info = json.loads(blob)
        creds = Credentials.from_authorized_user_info(info)
        if not creds.valid:
            if creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as exc:
                    raise AuthGmailError("Gmail authorization expired or was revoked. Reconnect Gmail.") from exc
                except Exception as exc:  # network
                    raise TransientGmailError(f"Could not refresh Gmail token: {type(exc).__name__}") from exc
                self.store.save(token_name(self.account_email), creds.to_json())
            else:
                raise AuthGmailError("No refresh token stored. Reconnect Gmail.")
        return creds

    def _service(self):
        if self._svc is None:
            from googleapiclient.discovery import build
            self._svc = build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)
        return self._svc

    def _call(self, request_factory):
        from googleapiclient.errors import HttpError
        try:
            return request_factory(self._service()).execute(num_retries=2)
        except HttpError as exc:
            reason = ""
            try:
                reason = json.loads(exc.content.decode())["error"]["errors"][0]["reason"]
            except Exception:  # noqa: BLE001
                pass
            if exc.resp.status in (401,):
                self._svc = None
            raise classify_http_error(exc.resp.status, reason, f"Gmail API {exc.resp.status} {reason}") from exc
        except (OSError, TimeoutError) as exc:
            raise TransientGmailError(f"Network error talking to Gmail: {type(exc).__name__}") from exc

    def profile_email(self) -> str:
        return self._call(lambda s: s.users().getProfile(userId="me"))["emailAddress"]

    def send_raw(self, raw: str, thread_id: str | None = None) -> dict[str, str]:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        r = self._call(lambda s: s.users().messages().send(userId="me", body=body))
        return {"id": r["id"], "threadId": r.get("threadId", "")}

    def find_by_rfc822_id(self, rfc822_id: str) -> dict[str, str] | None:
        q = f"rfc822msgid:{rfc822_id.strip('<>')}"
        r = self._call(lambda s: s.users().messages().list(userId="me", q=q, maxResults=1))
        msgs = r.get("messages", [])
        return {"id": msgs[0]["id"], "threadId": msgs[0].get("threadId", "")} if msgs else None

    def list_ids(self, query: str, max_results: int = 200) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        token = None
        while len(out) < max_results:
            r = self._call(lambda s: s.users().messages().list(userId="me", q=query, pageToken=token, maxResults=min(100, max_results)))
            out += r.get("messages", [])
            token = r.get("nextPageToken")
            if not token:
                break
        return out[:max_results]

    def get_message(self, msg_id: str, fmt: str = "full") -> dict[str, Any]:
        return self._call(lambda s: s.users().messages().get(userId="me", id=msg_id, format=fmt))


def token_name(email: str) -> str:
    return "gmail_" + re.sub(r"[^a-z0-9]", "_", email.lower())


# ------------------------------------------------------------------ OAuth flow (local, interactive)
def required_scopes(monitoring: bool) -> list[str]:
    return [GMAIL_SEND_SCOPE] + ([GMAIL_READONLY_SCOPE] if monitoring else [])


def connect_account(cfg: Config, store: TokenStore, db_session, monitoring: bool, open_browser: bool = True,
                    flow_factory=None) -> GmailAccount:
    """Runs the Google OAuth installed-app flow on 127.0.0.1 and stores the encrypted token."""
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not cfg.client_secret_file.exists():
            raise AuthGmailError(f"OAuth client file not found at {cfg.client_secret_file}. See README (Google Cloud setup).")
        flow_factory = lambda scopes: InstalledAppFlow.from_client_secrets_file(str(cfg.client_secret_file), scopes)  # noqa: E731
    scopes = required_scopes(monitoring)
    flow = flow_factory(scopes)
    creds = flow.run_local_server(host="127.0.0.1", port=cfg.oauth_port, open_browser=open_browser,
                                  access_type="offline", prompt="consent", authorization_prompt_message="",
                                  success_message="Gmail connected. You can close this tab.")
    granted = list(creds.scopes or scopes)
    if GMAIL_SEND_SCOPE not in granted:
        raise AuthGmailError("The gmail.send permission was not granted.")
    tmp_name = "gmail_pending"
    store.save(tmp_name, creds.to_json())
    svc = GmailService(cfg, store, "pending")
    # profile lookup using the freshly issued credentials
    from googleapiclient.discovery import build
    email = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()["emailAddress"]
    ref = store.save(token_name(email), creds.to_json())
    store.delete(tmp_name)
    acct = db_session.query(GmailAccount).filter_by(email=email).first() or GmailAccount(email=email)
    acct.status, acct.scopes, acct.token_ref, acct.last_error = "connected", granted, ref, ""
    db_session.add(acct)
    db_session.flush()
    return acct


def disconnect_account(cfg: Config, store: TokenStore, acct: GmailAccount, revoke: bool = True) -> str:
    """Revoke at Google (best effort), delete the local token, mark disconnected."""
    note = "Local token deleted."
    blob = store.load(token_name(acct.email))
    if revoke and blob:
        try:
            import requests
            tok = json.loads(blob).get("refresh_token") or json.loads(blob).get("token")
            r = requests.post("https://oauth2.googleapis.com/revoke", params={"token": tok}, timeout=10)
            note += " Google revocation " + ("succeeded." if r.status_code == 200 else f"returned {r.status_code}.")
        except Exception as exc:  # noqa: BLE001
            note += f" Google revocation failed ({type(exc).__name__}); revoke manually at myaccount.google.com/permissions."
    store.delete(token_name(acct.email))
    acct.status, acct.token_ref = "disconnected", ""
    return note


def mark_needs_reauth(acct: GmailAccount, error: str) -> None:
    acct.status, acct.last_error = "needs_reauth", error[:300]
