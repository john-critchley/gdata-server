"""Tests for the private/authenticated notes_web mode.

Covers the configurable mount PREFIX and the opt-in auth gate:
  - no token -> browsers 302 to {PREFIX}/login, API clients get 401
  - {PREFIX}/login mints a short-lived token and sets an HttpOnly session cookie
  - open-redirect protection on the login ?next= param
  - a valid Bearer header or session cookie grants access; logout revokes
  - emitted links use PREFIX (a private page never links into the public store)
  - default (public) config keeps the gate off and stays at /notes

The module reads PREFIX/AUTH from env at import; here we monkeypatch the module
globals (read at make_router call / request time) and use an isolated token
store, so no reload or network is needed.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gdata_oauth
import notes_web


# --- fakes ---------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if url.endswith('/__missing__'):
            return _FakeResp(404, None)
        return _FakeResp(200, {
            "title": "T",
            "content": [{"para": ["hi ", {"link": {"href": "other", "text": "o"}}]}],
        })


class _FakeHttpx:
    def AsyncClient(self, *a, **k):
        return _FakeClient()


def _make_client(monkeypatch, tmp_path, *, prefix="/private", auth=True):
    monkeypatch.setattr(notes_web, "PREFIX", prefix)
    monkeypatch.setattr(notes_web, "AUTH_REQUIRED", auth)
    monkeypatch.setattr(notes_web, "SESSION_COOKIE", "notes_session")
    monkeypatch.setattr(notes_web, "SESSION_TTL", 3600)
    monkeypatch.setattr(notes_web, "SESSION_HOME", "CONTENTS")
    monkeypatch.setattr(notes_web, "httpx", _FakeHttpx())
    monkeypatch.setattr(gdata_oauth, "_store",
                        gdata_oauth._TokenStore(str(tmp_path / "tok.json")))
    app = FastAPI(redirect_slashes=False)
    app.include_router(notes_web.make_router("http://127.0.0.1:9"))
    return TestClient(app)


@pytest.fixture
def private(monkeypatch, tmp_path):
    return _make_client(monkeypatch, tmp_path)


# --- gate ----------------------------------------------------------------

def test_gate_redirects_browser_to_login(private):
    r = private.get("/private/CONTENTS", headers={"accept": "text/html"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/private/login?next=")
    # the original path is preserved (url-encoded) in next=
    assert "%2Fprivate%2FCONTENTS" in r.headers["location"]


def test_gate_401_for_api_client(private):
    r = private.get("/private/CONTENTS", headers={"accept": "application/json"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")


def test_invalid_bearer_rejected(private):
    r = private.get("/private/CONTENTS",
                    headers={"accept": "application/json",
                             "authorization": "Bearer not-a-real-token"},
                    follow_redirects=False)
    assert r.status_code == 401


# --- login / cookie ------------------------------------------------------

def test_login_mints_cookie_and_redirects_home(private):
    r = private.get("/private/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/private/CONTENTS"
    sc = r.headers.get("set-cookie", "")
    assert "notes_session=" in sc
    assert "HttpOnly" in sc
    assert "Secure" in sc
    assert "Path=/private" in sc
    assert "SameSite=lax" in sc.lower() or "samesite=lax" in sc.lower()


def test_login_next_local_is_honoured(private):
    r = private.get("/private/login", params={"next": "/private/jira/tickets"},
                    follow_redirects=False)
    assert r.headers["location"] == "/private/jira/tickets"


def test_login_next_open_redirect_blocked(private):
    for evil in ("https://evil.example/x", "//evil.example", "/etc/passwd",
                 "/notes/secret"):
        r = private.get("/private/login", params={"next": evil},
                        follow_redirects=False)
        assert r.headers["location"] == "/private/CONTENTS", evil


# --- access with a valid token ------------------------------------------

def test_bearer_grants_access_and_links_use_prefix(private):
    tok = gdata_oauth.issue_token(ttl=3600)
    r = private.get("/private/CONTENTS",
                    headers={"accept": "text/html", "authorization": f"Bearer {tok}"},
                    follow_redirects=False)
    assert r.status_code == 200
    # link target rendered under the private prefix, not /notes
    assert 'href="/private/other"' in r.text
    assert 'href="/notes/' not in r.text


def test_cookie_grants_access(private):
    tok = gdata_oauth.issue_token(ttl=3600)
    private.cookies.set("notes_session", tok)
    r = private.get("/private/CONTENTS", headers={"accept": "text/html"},
                    follow_redirects=False)
    assert r.status_code == 200


def test_logout_revokes_token(private):
    tok = gdata_oauth.issue_token(ttl=3600)
    assert gdata_oauth.validate_token(tok)
    private.cookies.set("notes_session", tok)
    r = private.get("/private/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/private/login"
    assert not gdata_oauth.validate_token(tok)


# --- public config unaffected -------------------------------------------

def test_public_config_has_no_gate(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path, prefix="/notes", auth=False)
    # served without any token
    r = client.get("/notes/CONTENTS", headers={"accept": "text/html"},
                   follow_redirects=False)
    assert r.status_code == 200
    assert 'href="/notes/other"' in r.text
