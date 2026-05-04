"""
gdata_oauth.py — minimal OAuth 2.1 authorization server for gdata MCP access.

Single user, single client.  Supports authorization_code + PKCE (S256 only).
Access tokens live 30 days; auth codes expire in 5 minutes.

Required environment variables (put in ~/.oauth_env, sourced at startup):
  OAUTH_CLIENT_ID      — client identifier, must match Claude.ai connector setting
  OAUTH_CLIENT_SECRET  — client secret, must match Claude.ai connector setting
  OAUTH_PASSWORD       — pbkdf2:<salt_hex>:<hash_hex>  (generate with make_password_hash())
  OAUTH_ISSUER         — base URL, e.g. https://www.critchley.biz
  OAUTH_TOKEN_FILE     — path to JSON token store (default: ~/.oauth_tokens.json)

Generate a password hash:
  python3 -c "import gdata_oauth; print(gdata_oauth.make_password_hash())"
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLIENT_ID     = os.environ.get("OAUTH_CLIENT_ID",    "gdata")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
PASSWORD_HASH = os.environ.get("OAUTH_PASSWORD",      "")
ISSUER        = os.environ.get("OAUTH_ISSUER",        "https://www.critchley.biz")
TOKEN_FILE    = os.environ.get("OAUTH_TOKEN_FILE",
                               str(Path.home() / ".oauth_tokens.json"))

CODE_TTL  = 300           # 5 min
TOKEN_TTL = 30 * 86400    # 30 days


def startup_check():
    missing = [v for v in ("OAUTH_CLIENT_SECRET", "OAUTH_PASSWORD")
               if not os.environ.get(v)]
    if missing:
        sys.exit(f"FATAL: OAuth env vars not set: {', '.join(missing)}\n"
                 f"Source ~/.oauth_env before starting the server.")


# ---------------------------------------------------------------------------
# Password  (pbkdf2:<salt_hex>:<hash_hex>)
# ---------------------------------------------------------------------------

def make_password_hash(password: Optional[str] = None) -> str:
    """Generate a storable password hash.  Prompts if password not given."""
    if password is None:
        import getpass
        password = getpass.getpass("Password: ")
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"pbkdf2:{salt.hex()}:{h.hex()}"


def _check_password(password: str) -> bool:
    if not PASSWORD_HASH or not password:
        return False
    try:
        scheme, salt_hex, stored_hex = PASSWORD_HASH.split(":", 2)
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 260_000
    ).hex()
    return hmac.compare_digest(candidate, stored_hex)


# ---------------------------------------------------------------------------
# PKCE  (S256 only)
# ---------------------------------------------------------------------------

def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


# ---------------------------------------------------------------------------
# Token store  (JSON file — low concurrency, single user)
# ---------------------------------------------------------------------------

class _TokenStore:
    def __init__(self, path: str):
        self._path = Path(path)
        self._data: dict = {"codes": {}, "tokens": {}}
        self._load()

    def _load(self):
        try:
            self._data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {"codes": {}, "tokens": {}}

    def _save(self):
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self._path)

    def _clean(self):
        now = time.time()
        self._data["codes"]  = {k: v for k, v in self._data["codes"].items()
                                 if v["exp"] > now}
        self._data["tokens"] = {k: v for k, v in self._data["tokens"].items()
                                 if v["exp"] > now}

    def save_code(self, code: str, client_id: str, redirect_uri: str,
                  challenge: str, method: str):
        self._clean()
        self._data["codes"][code] = {
            "client_id": client_id, "redirect_uri": redirect_uri,
            "challenge": challenge, "method": method,
            "exp": time.time() + CODE_TTL,
        }
        self._save()

    def consume_code(self, code: str) -> Optional[dict]:
        self._clean()
        entry = self._data["codes"].pop(code, None)
        if entry:
            self._save()
        return entry  # None if not found / expired

    def save_token(self, token: str):
        self._clean()
        self._data["tokens"][token] = {"exp": time.time() + TOKEN_TTL}
        self._save()

    def is_valid(self, token: str) -> bool:
        entry = self._data.get("tokens", {}).get(token)
        return bool(entry and entry["exp"] > time.time())

    def revoke(self, token: str):
        self._data["tokens"].pop(token, None)
        self._save()


_store: Optional[_TokenStore] = None


def _get_store() -> _TokenStore:
    global _store
    if _store is None:
        _store = _TokenStore(TOKEN_FILE)
    return _store


def validate_token(token: Optional[str]) -> bool:
    if not token:
        return False
    return _get_store().is_valid(token)


# ---------------------------------------------------------------------------
# Pure ASGI bearer-token middleware  (BaseHTTPMiddleware buffers and breaks SSE)
# ---------------------------------------------------------------------------

class BearerMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            token = auth[7:] if auth.lower().startswith("bearer ") else None
            if validate_token(token):
                await self.app(scope, receive, send)
                return
            # Reject with 401
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"www-authenticate", b'Bearer realm="gdata"'],
                    [b"content-type", b"text/plain"],
                ],
            })
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        # Pass lifespan / websocket through unchanged
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# OAuth routes (mount on the FastAPI REST app)
# ---------------------------------------------------------------------------

router = APIRouter()

_AUTHORIZE_FORM = """\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Authorise gdata access</title>
  <style>
    body {{ font-family: sans-serif; max-width: 360px; margin: 5em auto;
            padding: 1.5em; border: 1px solid #ddd; border-radius: 8px; }}
    h2   {{ margin-top: 0; }}
    input[type=password] {{
      width: 100%; padding: .5em; margin: .4em 0 1em;
      box-sizing: border-box; border: 1px solid #ccc;
      border-radius: 4px; font-size: 1em;
    }}
    button {{
      width: 100%; padding: .7em; background: #0a84ff; color: #fff;
      border: none; border-radius: 4px; cursor: pointer; font-size: 1em;
    }}
    .err {{ color: #c00; margin-bottom: .8em; }}
  </style>
</head>
<body>
  <h2>Authorise gdata access</h2>
  {error}
  <form method="post">
    <input type="hidden" name="client_id"             value="{client_id}">
    <input type="hidden" name="redirect_uri"          value="{redirect_uri}">
    <input type="hidden" name="state"                 value="{state}">
    <input type="hidden" name="code_challenge"        value="{code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Allow access</button>
  </form>
</body>
</html>"""


def _render_form(client_id, redirect_uri, state,
                 code_challenge, code_challenge_method, error=""):
    return _AUTHORIZE_FORM.format(
        client_id=client_id, redirect_uri=redirect_uri, state=state,
        code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        error=f'<p class="err">{error}</p>' if error else "",
    )


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def protected_resource_metadata(path: str = ""):
    return {
        "resource":              f"{ISSUER}/mcp",
        "authorization_servers": [ISSUER],
    }


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    return {
        "issuer":                                ISSUER,
        "authorization_endpoint":                f"{ISSUER}/oauth/authorize",
        "token_endpoint":                        f"{ISSUER}/oauth/token",
        "revocation_endpoint":                   f"{ISSUER}/oauth/revoke",
        "response_types_supported":              ["code"],
        "grant_types_supported":                 ["authorization_code"],
        "code_challenge_methods_supported":      ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_get(
    client_id: str,
    redirect_uri: str,
    state: str = "",
    response_type: str = "code",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
):
    if client_id != CLIENT_ID:
        raise HTTPException(400, "unknown client_id")
    if code_challenge_method != "S256":
        raise HTTPException(400, "only S256 code_challenge_method supported")
    return _render_form(client_id, redirect_uri, state,
                        code_challenge, code_challenge_method)


@router.post("/oauth/authorize", response_class=HTMLResponse)
async def authorize_post(
    client_id:             str = Form(...),
    redirect_uri:          str = Form(...),
    state:                 str = Form(""),
    code_challenge:        str = Form(""),
    code_challenge_method: str = Form("S256"),
    password:              str = Form(...),
):
    if client_id != CLIENT_ID:
        raise HTTPException(400, "unknown client_id")
    if not _check_password(password):
        return HTMLResponse(
            _render_form(client_id, redirect_uri, state,
                         code_challenge, code_challenge_method,
                         "Incorrect password"),
            status_code=401,
        )
    code = secrets.token_urlsafe(32)
    _get_store().save_code(code, client_id, redirect_uri,
                           code_challenge, code_challenge_method)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}code={code}&state={state}",
        status_code=302,
    )


@router.post("/oauth/token")
async def token_endpoint(
    grant_type:    str = Form(...),
    code:          str = Form(None),
    redirect_uri:  str = Form(None),
    client_id:     str = Form(None),
    client_secret: str = Form(None),
    code_verifier: str = Form(None),
):
    if grant_type != "authorization_code":
        raise HTTPException(400, "unsupported_grant_type")
    if (client_id != CLIENT_ID
            or not hmac.compare_digest(client_secret or "", CLIENT_SECRET)):
        raise HTTPException(401, "invalid_client")
    entry = _get_store().consume_code(code or "")
    if not entry:
        raise HTTPException(400, "invalid_grant")
    if entry["redirect_uri"] != redirect_uri:
        raise HTTPException(400, "redirect_uri_mismatch")
    if entry["challenge"] and not _pkce_ok(code_verifier or "", entry["challenge"]):
        raise HTTPException(400, "invalid_grant")
    token = secrets.token_urlsafe(32)
    _get_store().save_token(token)
    return {
        "access_token": token,
        "token_type":   "Bearer",
        "expires_in":   TOKEN_TTL,
    }


@router.post("/oauth/revoke")
async def revoke(token: str = Form(...)):
    _get_store().revoke(token)
    return {}  # RFC 7009: always 200
