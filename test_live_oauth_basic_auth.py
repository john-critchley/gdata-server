"""Live end-to-end test for Apache Basic Auth -> OAuth -> MCP.

This test deliberately mutates the deployed WebDAV htpasswd file.  It is
disabled by default and must run as root on gravlax:

    RUN_LIVE_OAUTH_TEST=1 python3 -m pytest test_live_oauth_basic_auth.py -v
"""

import base64
import hashlib
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

HTPASSWD = Path("/etc/apache2/.htpasswd-webdav")
BASE_URL = "https://www.critchley.biz"
TIMEOUT = 15.0

ISSUERS = (
    ("notes", "", Path("/etc/gdata_oauth.env")),
    ("private", "/private", Path("/etc/gdata_oauth_private.env")),
    ("misc", "/misc", Path("/etc/gdata_oauth_misc.env")),
)


def _read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    return values


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_OAUTH_TEST") == "1",
    "live OAuth test is disabled (set RUN_LIVE_OAUTH_TEST=1 on gravlax)",
)
class LiveOAuthBasicAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise AssertionError("live OAuth test must run as root")
        if not HTPASSWD.is_file():
            raise AssertionError(f"missing credential file: {HTPASSWD}")

        cls._tempdir = tempfile.TemporaryDirectory(prefix="oauth-htpasswd-")
        cls.backup = Path(cls._tempdir.name) / "htpasswd.backup"
        cls.original_stat = HTPASSWD.stat()
        shutil.copy2(HTPASSWD, cls.backup)
        cls.username = f"mcp-oauth-test-{secrets.token_hex(6)}"
        cls.password = secrets.token_urlsafe(24)
        try:
            subprocess.run(
                ["htpasswd", "-b", str(HTPASSWD), cls.username, cls.password],
                check=True,
                capture_output=True,
                text=True,
            )
            assert HTPASSWD.read_bytes().splitlines()[-1].startswith(
                f"{cls.username}:".encode()
            ), "test user was not appended to the htpasswd file"
        except BaseException:
            cls._restore_htpasswd()
            raise

    @classmethod
    def tearDownClass(cls):
        # Restore content and metadata even when authorization/token assertions fail.
        cls._restore_htpasswd()
        cls._tempdir.cleanup()

    @classmethod
    def _restore_htpasswd(cls):
        shutil.copyfile(cls.backup, HTPASSWD)
        os.chown(HTPASSWD, cls.original_stat.st_uid, cls.original_stat.st_gid)
        os.chmod(HTPASSWD, stat.S_IMODE(cls.original_stat.st_mode))
        if HTPASSWD.read_bytes() != cls.backup.read_bytes():
            raise AssertionError("failed to restore the WebDAV htpasswd file")

    def test_basic_auth_oauth_token_and_mcp(self):
        for label, prefix, env_path in ISSUERS:
            with self.subTest(issuer=label):
                self._exercise_issuer(label, prefix, env_path)

    def _exercise_issuer(self, label, prefix, env_path):
        oauth_env = _read_env(env_path)
        client_id = oauth_env["OAUTH_CLIENT_ID"]
        client_secret = oauth_env["OAUTH_CLIENT_SECRET"]
        redirect_uri = "https://example.invalid/mcp-oauth-test"
        state = secrets.token_urlsafe(18)
        verifier, challenge = _pkce()
        authorize_url = f"{BASE_URL}{prefix}/oauth/authorize"

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            unauthenticated = client.get(authorize_url, params=params)
            assert unauthenticated.status_code == 401
            assert "Basic" in unauthenticated.headers.get("www-authenticate", "")

            authorized = client.get(
                authorize_url,
                params=params,
                auth=httpx.BasicAuth(self.username, self.password),
            )
            assert authorized.status_code == 302, authorized.text
            callback = urlparse(authorized.headers["location"])
            callback_params = parse_qs(callback.query)
            assert callback_params["state"] == [state]
            code = callback_params["code"][0]

            token_response = client.post(
                f"{BASE_URL}{prefix}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code_verifier": verifier,
                },
            )
            assert token_response.status_code == 200, token_response.text
            token_data = token_response.json()
            token = token_data["access_token"]
            assert token_data["token_type"] == "Bearer"
            assert token_data["expires_in"] == 30 * 86400

            try:
                mcp_response = client.post(
                    f"{BASE_URL}{prefix}/mcp",
                    follow_redirects=True,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "oauth-live-test",
                                "version": "1",
                            },
                        },
                    },
                )
                assert mcp_response.status_code == 200, (
                    f"MCP initialize returned {mcp_response.status_code}: "
                    f"{mcp_response.text}"
                )
            finally:
                revoke_response = client.post(
                    f"{BASE_URL}{prefix}/oauth/revoke", data={"token": token}
                )
                assert revoke_response.status_code == 200

            rejected = client.post(
                f"{BASE_URL}{prefix}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            )
            assert rejected.status_code == 401, f"{label} accepted a revoked token"


if __name__ == "__main__":
    unittest.main(verbosity=2)
