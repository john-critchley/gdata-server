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
PASSWORD_HASH  = os.environ.get("OAUTH_PASSWORD",  "")
PASSWORD_HASH2 = os.environ.get("OAUTH_PASSWORD2", "")
ISSUER        = os.environ.get("OAUTH_ISSUER",        "https://www.critchley.biz")
AUTHORIZATION_ENDPOINT = os.environ.get(
    "OAUTH_AUTHORIZATION_ENDPOINT", f"{ISSUER}/oauth/authorize"
)
_issuer_path  = ISSUER.replace("https://", "").replace("http://", "").split("/", 1)
_STORE_LABEL  = _issuer_path[1].strip("/") if len(_issuer_path) > 1 and _issuer_path[1].strip("/") else "notes"
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


def _verify_hash(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, stored_hex = stored.split(":", 2)
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 260_000
    ).hex()
    return hmac.compare_digest(candidate, stored_hex)

def _check_password(password: str) -> bool:
    if not password:
        return False
    if PASSWORD_HASH and _verify_hash(password, PASSWORD_HASH):
        return True
    if PASSWORD_HASH2 and _verify_hash(password, PASSWORD_HASH2):
        return True
    return False


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
            self._data = {"codes": {}, "tokens": {}, "clients": {}}
        self._data.setdefault("codes", {})
        self._data.setdefault("tokens", {})
        self._data.setdefault("clients", {})

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

    def save_client(self, client_id: str, client_secret: str, auth_method: str):
        self._data.setdefault("clients", {})[client_id] = {
            "client_secret": client_secret,
            "token_endpoint_auth_method": auth_method,
            "issued_at": int(time.time()),
        }
        self._save()

    def get_client(self, client_id: str) -> Optional[dict]:
        return self._data.get("clients", {}).get(client_id)

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
            resource_metadata = (
                f'Bearer realm="gdata", '
                f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'
            ).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"www-authenticate", resource_metadata],
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
  <title>Authorise {store_label} access</title>
  <style>
    body {{ font-family: sans-serif; max-width: 360px; margin: 5em auto;
            padding: 1.5em; border: 1px solid #ddd; border-radius: 8px; }}
    h2   {{ margin-top: 0; }}
    input[type=text], input[type=password] {{
      width: 100%; padding: .5em; margin: .4em 0 1em;
      box-sizing: border-box; border: 1px solid #ccc;
      border-radius: 4px; font-size: 1em;
    }}
    input[type=text][readonly] {{ background: #f5f5f5; color: #666; }}
    button {{
      width: 100%; padding: .7em; background: #0a84ff; color: #fff;
      border: none; border-radius: 4px; cursor: pointer; font-size: 1em;
    }}
    .err {{ color: #c00; margin-bottom: .8em; }}
  </style>
</head>
<body>
  <h2>Authorise {store_label} access</h2>
  {error}
  <form method="post">
    <input type="hidden" name="client_id"             value="{client_id}">
    <input type="hidden" name="redirect_uri"          value="{redirect_uri}">
    <input type="hidden" name="state"                 value="{state}">
    <input type="hidden" name="code_challenge"        value="{code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
    <input type="text"     name="username" value="{store_label}" readonly>
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
        store_label=_STORE_LABEL,
        error=f'<p class="err">{error}</p>' if error else "",
    )


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def protected_resource_metadata(path: str = ""):
    return {
        "resource":              f"{ISSUER}/mcp/",
        "authorization_servers": [ISSUER],
    }


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    return {
        "issuer":                                ISSUER,
        "authorization_endpoint":                AUTHORIZATION_ENDPOINT,
        "token_endpoint":                        f"{ISSUER}/oauth/token",
        "revocation_endpoint":                   f"{ISSUER}/oauth/revoke",
        "response_types_supported":              ["code"],
        "grant_types_supported":                 ["authorization_code"],
        "code_challenge_methods_supported":      ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "registration_endpoint":                 f"{ISSUER}/oauth/register",
    }


def _client_known(client_id: str) -> bool:
    # Accept: hardcoded CLIENT_ID, registered clients, or any codex-* (ChatGPT)
    return (client_id == CLIENT_ID or 
            _get_store().get_client(client_id) is not None or
            client_id.startswith("codex-"))


def _client_auth_ok(client_id: str, client_secret: Optional[str]) -> bool:
    if client_id == CLIENT_ID:
        return hmac.compare_digest(client_secret or "", CLIENT_SECRET)
    # For codex-* clients (ChatGPT), check store or allow "none" auth
    client = _get_store().get_client(client_id)
    if not client:
        # If not registered, assume codex-* clients use "none" (no secret required)
        return client_id.startswith("codex-")
    method = client.get("token_endpoint_auth_method", "client_secret_post")
    if method == "none":
        return True
    return hmac.compare_digest(client_secret or "", client.get("client_secret", ""))


@router.post("/oauth/register")
async def register_client(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not all(isinstance(uri, str) for uri in redirect_uris):
        raise HTTPException(400, "redirect_uris must be a list of strings")
    auth_method = body.get("token_endpoint_auth_method", "client_secret_post")
    if auth_method not in ("client_secret_post", "none"):
        auth_method = "client_secret_post"
    client_id = "codex-" + secrets.token_urlsafe(18)
    client_secret = "" if auth_method == "none" else secrets.token_urlsafe(32)
    _get_store().save_client(client_id, client_secret, auth_method)
    response = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    if client_secret:
        response["client_secret"] = client_secret
        response["client_secret_expires_at"] = 0
    return response


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_get(
    client_id: str,
    redirect_uri: str,
    state: str = "",
    response_type: str = "code",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
):
    if not _client_known(client_id):
        raise HTTPException(400, "unknown client_id")
    if response_type != "code":
        raise HTTPException(400, "only response_type=code supported")
    if code_challenge_method != "S256":
        raise HTTPException(400, "only S256 code_challenge_method supported")
    if not code_challenge:
        raise HTTPException(400, "code_challenge required")

    # Apache protects this endpoint with the WebDAV Basic Auth credentials.
    # Once that succeeds, issue the normal short-lived, one-use PKCE code
    # without asking for a second application password.
    code = secrets.token_urlsafe(32)
    _get_store().save_code(code, client_id, redirect_uri,
                           code_challenge, code_challenge_method)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}code={code}&state={state}",
        status_code=302,
    )


@router.post("/oauth/authorize", response_class=HTMLResponse)
async def authorize_post(
    client_id:             str = Form(...),
    redirect_uri:          str = Form(...),
    state:                 str = Form(""),
    code_challenge:        str = Form(""),
    code_challenge_method: str = Form("S256"),
    password:              str = Form(...),
):
    if not _client_known(client_id):
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
    if not _client_auth_ok(client_id or "", client_secret):
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
