"""
test_gdata_mcp_rest.py — integration tests for gdata_mcp_server's REST HTTP
surface (make_rest_app), covering two bug fixes (2026-07-08):

1. PUT/write must reject a JSON array shaped like an ops/patch payload
   (e.g. [{"op": "upsert", ...}]) instead of silently storing it -- that
   previously left a key permanently unpatchable with no clear cause.
   Storing other non-dict JSON (plain lists, strings, numbers) must still
   work; only the specific ops-shaped corruption pattern is blocked.

2. The REST batch op's own 'ops' string-parsing (separate code path from
   the MCP batch tool, which has its own copy) used plain json.loads with
   no tolerance for a client escaping apostrophes as \\' -- fixed to use
   _parse_json_robust like everywhere else.

Run: python -m pytest test_gdata_mcp_rest.py -v
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

import pytest
import requests
import uvicorn

sys.path.insert(0, os.path.dirname(__file__))

_tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_tf.close()
os.environ.setdefault("OAUTH_TOKEN_FILE", _tf.name)

from gdata_mcp_server import make_rest_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Port {port} did not open within {timeout}s")


@pytest.fixture(scope="module")
def server_url():
    with tempfile.NamedTemporaryFile(suffix=".gdbm", delete=False) as f:
        db_path = f.name
    app = make_rest_app(db_path)
    port = _free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    _wait_port(port)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=3.0)
    for p in (db_path, db_path + ".db"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


KEY = "_test/mcp-rest/put"
BATCH_KEY = "_test/mcp-rest/batch"

BASE_DOC = {
    "title": "Base",
    "content": [
        {"para": ["First."]},
        {"para": ["Second."]},
    ],
}


@pytest.fixture(autouse=True)
def cleanup(server_url):
    yield
    requests.delete(f"{server_url}/{KEY}")
    requests.delete(f"{server_url}/{BATCH_KEY}")


# ---------------------------------------------------------------------------
# PUT: ops-shaped payload rejection
# ---------------------------------------------------------------------------

class TestPutOpsShapeRejection:

    def test_ops_shaped_list_rejected(self, server_url):
        payload = [{"op": "upsert", "name": "x", "content": []}]
        r = requests.put(f"{server_url}/{KEY}", json=payload)
        assert r.status_code == 400
        assert "op" in r.json()["detail"]

    def test_ops_shaped_list_not_stored(self, server_url):
        """The rejected PUT must leave no trace -- key should not exist."""
        payload = [{"op": "upsert", "name": "x", "content": []}]
        requests.put(f"{server_url}/{KEY}", json=payload)
        r = requests.get(f"{server_url}/{KEY}")
        assert r.status_code == 404

    def test_multi_op_batch_shaped_list_rejected(self, server_url):
        """Realistic batch payload (multiple ops), not just a single-op list."""
        payload = [
            {"op": "append_block", "block": {"para": ["a"]}},
            {"op": "patch_meta", "fields": {"title": "x"}},
        ]
        r = requests.put(f"{server_url}/{KEY}", json=payload)
        assert r.status_code == 400

    def test_rejection_does_not_clobber_existing_value(self, server_url):
        """A corrupt PUT attempt must not destroy a previously-good document."""
        requests.put(f"{server_url}/{KEY}", json=BASE_DOC)
        bad_payload = [{"op": "upsert", "name": "x", "content": []}]
        r = requests.put(f"{server_url}/{KEY}", json=bad_payload)
        assert r.status_code == 400
        assert requests.get(f"{server_url}/{KEY}").json() == BASE_DOC

    # --- confirm the fix is narrowly scoped: general KV storage still works ---

    def test_plain_list_of_numbers_still_allowed(self, server_url):
        r = requests.put(f"{server_url}/{KEY}", json=[1, 2, 3])
        assert r.status_code == 200
        assert requests.get(f"{server_url}/{KEY}").json() == [1, 2, 3]

    def test_empty_list_still_allowed(self, server_url):
        r = requests.put(f"{server_url}/{KEY}", json=[])
        assert r.status_code == 200
        assert requests.get(f"{server_url}/{KEY}").json() == []

    def test_list_of_dicts_without_op_key_still_allowed(self, server_url):
        """A list of plain dicts (no 'op' key) is not ops-shaped -- must be allowed."""
        r = requests.put(f"{server_url}/{KEY}", json=[{"foo": 1}, {"bar": 2}])
        assert r.status_code == 200
        assert requests.get(f"{server_url}/{KEY}").json() == [{"foo": 1}, {"bar": 2}]

    def test_plain_string_value_still_allowed(self, server_url):
        r = requests.put(f"{server_url}/{KEY}", json="just a string")
        assert r.status_code == 200
        assert requests.get(f"{server_url}/{KEY}").json() == "just a string"

    def test_normal_document_still_allowed(self, server_url):
        r = requests.put(f"{server_url}/{KEY}", json=BASE_DOC)
        assert r.status_code == 200
        assert requests.get(f"{server_url}/{KEY}").json() == BASE_DOC

    def test_invalid_json_body_rejected(self, server_url):
        r = requests.put(
            f"{server_url}/{KEY}",
            data="{not json}",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# REST batch: apostrophe-escape tolerance (separate code path from the MCP
# batch tool -- gdata_mcp_server.py's REST /{key} POST batch handler has its
# own 'ops' string-parsing, previously plain json.loads with no repair)
# ---------------------------------------------------------------------------

class TestRestBatchApostrophe:

    @pytest.fixture(autouse=True)
    def fresh(self, server_url):
        requests.put(f"{server_url}/{BATCH_KEY}", json=BASE_DOC)
        yield
        requests.delete(f"{server_url}/{BATCH_KEY}")

    def test_batch_ops_string_with_escaped_apostrophe_is_repaired(self, server_url):
        ops = [{"op": "append_block", "block": {"para": ["I'll do this"]}}]
        # Simulate a client that (wrongly) escapes apostrophes as \' when
        # building the ops JSON string -- this is the reported bug.
        ops_json_text = json.dumps(ops).replace("I'll", "I\\'ll")
        r = requests.post(f"{server_url}/{BATCH_KEY}",
                           json={"op": "batch", "ops": ops_json_text})
        assert r.status_code == 200, r.text
        content = requests.get(f"{server_url}/{BATCH_KEY}").json()["content"]
        assert content[-1] == {"para": ["I'll do this"]}

    def test_batch_ops_string_valid_json_unaffected(self, server_url):
        """Sanity check: a properly-encoded ops string (no escaping mistake)
        still works after the fix."""
        ops = [{"op": "patch_meta", "fields": {"title": "Updated"}}]
        r = requests.post(f"{server_url}/{BATCH_KEY}",
                           json={"op": "batch", "ops": json.dumps(ops)})
        assert r.status_code == 200, r.text
        assert requests.get(f"{server_url}/{BATCH_KEY}").json()["title"] == "Updated"

    def test_batch_ops_string_genuinely_invalid_json_still_errors(self, server_url):
        r = requests.post(f"{server_url}/{BATCH_KEY}",
                           json={"op": "batch", "ops": "{not valid json"})
        assert r.status_code == 400
        assert "ops" in r.json()["detail"]
