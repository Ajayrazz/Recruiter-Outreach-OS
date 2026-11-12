import base64
import email
import json
from email import policy
from types import SimpleNamespace

import pytest

from recruiter_os.models import GmailAccount
from recruiter_os.services import gmail as g
from recruiter_os.secure_store import TokenStore, SecretStoreError


def test_mime_has_text_html_and_pdf_attachment():
    m = g.build_mime(sender="me@gmail.com", sender_name="Me", to="hr@x.com", subject="Hi", body_text="Hello\n\nSee https://a.dev",
                     rfc822_id="<id1@x>", attachment=("cv.pdf", b"%PDF-1.4 x"))
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(g.mime_to_raw(m)), policy=policy.default)
    assert parsed["To"] == "hr@x.com" and parsed["Message-ID"] == "<id1@x>"
    assert parsed.get_body(("plain",)).get_content().startswith("Hello")
    assert '<a href="https://a.dev">https://a.dev</a>' in parsed.get_body(("html",)).get_content()
    assert [p.get_filename() for p in parsed.iter_attachments()] == ["cv.pdf"]


def test_html_escapes_content():
    assert "<script>" not in g.text_to_html("<script>alert(1)</script>")


def test_header_injection_blocked():
    with pytest.raises(g.PermanentGmailError):
        g.build_mime(sender="a@b.com", sender_name="", to="x@y.com\nBcc: evil@z.com", subject="s", body_text="b", rfc822_id="<1@x>")


def test_followup_headers():
    m = g.build_mime(sender="a@b.com", sender_name="", to="x@y.com", subject="Re: s", body_text="b", rfc822_id="<2@x>", in_reply_to="<1@x>")
    assert m["In-Reply-To"] == "<1@x>" and m["References"] == "<1@x>"


@pytest.mark.parametrize("status,reason,cls", [
    (401, "", g.AuthGmailError), (403, "forbidden", g.AuthGmailError), (429, "", g.QuotaGmailError),
    (403, "rateLimitExceeded", g.QuotaGmailError), (503, "", g.TransientGmailError), (400, "invalidArgument", g.PermanentGmailError)])
def test_error_classification(status, reason, cls):
    assert type(g.classify_http_error(status, reason)) is cls


def test_token_store_encrypts_and_roundtrips(tmp_path):
    from cryptography.fernet import Fernet
    k = Fernet.generate_key().decode()
    st = TokenStore(tmp_path, k)
    st.save("gmail_x", '{"refresh_token":"1//secret"}')
    raw = (tmp_path / "gmail_x.enc").read_bytes()
    assert b"secret" not in raw
    assert st.load("gmail_x") == '{"refresh_token":"1//secret"}'
    assert oct((tmp_path / "gmail_x.enc").stat().st_mode & 0o777) == "0o600"
    with pytest.raises(SecretStoreError):
        TokenStore(tmp_path, Fernet.generate_key().decode()).load("gmail_x")
    st.delete("gmail_x")
    assert st.load("gmail_x") is None


class FakeFlow:
    def __init__(self, scopes):
        self.scopes = scopes

    def run_local_server(self, **kw):
        assert kw["host"] == "127.0.0.1"
        return SimpleNamespace(scopes=self.scopes, to_json=lambda: json.dumps({"refresh_token": "1//r", "token": "ya29.t"}))


def test_oauth_flow_mocked_requests_minimal_scopes(app, monkeypatch):
    seen = {}

    def factory(scopes):
        seen["scopes"] = scopes
        return FakeFlow(scopes)

    class FakeBuild:
        def users(self): return self
        def getProfile(self, userId): return self
        def execute(self): return {"emailAddress": "new@gmail.com"}
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: FakeBuild())
    with app.db.scope() as s:
        acct = g.connect_account(app.cfg, app.store, s, monitoring=False, open_browser=False, flow_factory=factory)
        assert seen["scopes"] == [g.GMAIL_SEND_SCOPE]  # no readonly unless monitoring enabled
        assert acct.email == "new@gmail.com" and acct.status == "connected"
    assert "ya29" in app.store.load(g.token_name("new@gmail.com"))  # token lives only in encrypted store
    with app.db.scope() as s:
        row = s.query(GmailAccount).filter_by(email="new@gmail.com").one()
        assert "ya29" not in json.dumps([row.token_ref, row.scopes, row.last_error])


def test_oauth_requires_send_scope(app):
    class NoSend(FakeFlow):
        def run_local_server(self, **kw):
            return SimpleNamespace(scopes=[], to_json=lambda: "{}")
    # empty scopes -> falls back to requested scopes, so simulate explicit wrong scope
    class Wrong(FakeFlow):
        def run_local_server(self, **kw):
            return SimpleNamespace(scopes=["other"], to_json=lambda: "{}")
    with app.db.scope() as s, pytest.raises(g.AuthGmailError):
        g.connect_account(app.cfg, app.store, s, False, False, flow_factory=lambda sc: Wrong(sc))


def test_missing_client_secret_file_is_clear_error(app):
    with app.db.scope() as s, pytest.raises(g.AuthGmailError, match="OAuth client file"):
        g.connect_account(app.cfg, app.store, s, False, False)


def test_disconnect_deletes_token(app, monkeypatch):
    app.store.save(g.token_name("me@gmail.com"), json.dumps({"refresh_token": "r"}))
    monkeypatch.setattr("requests.post", lambda *a, **k: SimpleNamespace(status_code=200))
    with app.db.scope() as s:
        acct = s.query(GmailAccount).one()
        note = g.disconnect_account(app.cfg, app.store, acct)
        assert acct.status == "disconnected" and "revocation succeeded" in note
    assert app.store.load(g.token_name("me@gmail.com")) is None
