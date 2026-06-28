"""
test_reorder.py — integration tests for the reorder op on the REST HTTP surface.

Tests both POST /{key} {op: reorder} and PATCH /{key} {op: reorder} against
an in-process instance of gdata_mcp_server's REST app (make_rest_app).

Run: python -m pytest test_reorder.py -v
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

# OAuth token file required by gdata_mcp_server at import time.
_tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_tf.close()
os.environ.setdefault("OAUTH_TOKEN_FILE", _tf.name)

from gdata_mcp_server import make_rest_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


KEY = "_test/reorder"

BASE_DOC = {
    "title": "Reorder Test",
    "content": [
        {"heading": {"level": 1, "text": "First"}},
        {"para": ["Second."]},
        {"para": ["Third."]},
        {"para": ["Fourth."]},
    ],
}


def _put(url, key, doc) -> dict:
    r = requests.put(f"{url}/{key}", json=doc)
    assert r.status_code == 200, f"PUT failed: {r.text}"
    return r.json()


def _get(url, key) -> dict:
    return requests.get(f"{url}/{key}").json()


def _get_ids(url, key) -> dict:
    r = requests.post(f"{url}/{key}", json={"op": "get_with_block_ids"})
    assert r.status_code == 200
    return r.json()


@pytest.fixture(autouse=True)
def fresh_doc(server_url):
    result = _put(server_url, KEY, BASE_DOC)
    yield result  # yields {"status": "ok", "rev": ..., "block_ids": [...]}
    requests.delete(f"{server_url}/{KEY}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post_reorder(server_url, order, **extra) -> requests.Response:
    return requests.post(f"{server_url}/{KEY}", json={"op": "reorder", "order": order, **extra})


def patch_reorder(server_url, order, **extra) -> requests.Response:
    return requests.patch(f"{server_url}/{KEY}", json={"op": "reorder", "order": order, **extra})


# ---------------------------------------------------------------------------
# TestReorderPost — via POST /{key}
# ---------------------------------------------------------------------------

class TestReorderPost:

    def test_reverse_order(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = post_reorder(server_url, list(reversed(ids)))
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        c = _get(server_url, KEY)["content"]
        assert c[0] == {"para": ["Fourth."]}
        assert c[3] == {"heading": {"level": 1, "text": "First"}}

    def test_move_first_to_last(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        new_order = ids[1:] + [ids[0]]
        r = post_reorder(server_url, new_order)
        assert r.status_code == 200
        c = _get(server_url, KEY)["content"]
        assert c[-1] == {"heading": {"level": 1, "text": "First"}}
        assert c[0] == {"para": ["Second."]}

    def test_identity_reorder_ok(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = post_reorder(server_url, ids)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_rev_increments(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        old_rev = fresh_doc["rev"]
        post_reorder(server_url, list(reversed(ids)))
        info = _get_ids(server_url, KEY)
        assert int(info["rev"][1:]) > int(old_rev[1:])

    def test_block_ids_unchanged(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        post_reorder(server_url, list(reversed(ids)))
        info = _get_ids(server_url, KEY)
        assert set(info["block_ids"]) == set(ids)

    def test_content_not_modified(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        reversed_ids = list(reversed(ids))
        post_reorder(server_url, reversed_ids)
        c = _get(server_url, KEY)["content"]
        # Just the heading — originally index 0, now at index 3 after reversal
        assert c[3] == {"heading": {"level": 1, "text": "First"}}
        assert c[0] == {"para": ["Fourth."]}

    def test_if_rev_correct_succeeds(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        rev = fresh_doc["rev"]
        r = post_reorder(server_url, ids, if_rev=rev)
        assert r.status_code == 200

    def test_if_rev_stale_returns_409(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = post_reorder(server_url, ids, if_rev="r9999")
        assert r.status_code == 409

    def test_missing_id_returns_422(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = post_reorder(server_url, ids[:3])  # omit last
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "missing" in detail

    def test_unknown_id_returns_422(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = post_reorder(server_url, ids[:3] + ["BOGUSID"])
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "unknown" in detail

    def test_duplicate_id_returns_422(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = post_reorder(server_url, [ids[0]] + ids)  # first ID duplicated
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "duplicates" in detail

    def test_non_list_order_returns_400(self, server_url, fresh_doc):
        r = requests.post(f"{server_url}/{KEY}", json={"op": "reorder", "order": "notalist"})
        assert r.status_code == 400

    def test_reorder_usable_in_batch(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = requests.post(f"{server_url}/{KEY}", json={
            "op": "batch",
            "ops": [{"op": "reorder", "order": list(reversed(ids))}],
        })
        assert r.status_code == 200
        c = _get(server_url, KEY)["content"]
        assert c[0] == {"para": ["Fourth."]}


# ---------------------------------------------------------------------------
# TestReorderPatch — same ops via PATCH /{key}
# ---------------------------------------------------------------------------

class TestReorderPatch:

    def test_patch_method_reverse_order(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = patch_reorder(server_url, list(reversed(ids)))
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        c = _get(server_url, KEY)["content"]
        assert c[0] == {"para": ["Fourth."]}

    def test_patch_method_if_rev_stale_returns_409(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = patch_reorder(server_url, ids, if_rev="r9999")
        assert r.status_code == 409

    def test_patch_method_missing_id_returns_422(self, server_url, fresh_doc):
        ids = fresh_doc["block_ids"]
        r = patch_reorder(server_url, ids[:2])
        assert r.status_code == 422

    def test_patch_method_other_ops_work(self, server_url, fresh_doc):
        """PATCH /{key} accepts all ops, not just reorder."""
        r = requests.patch(f"{server_url}/{KEY}", json={
            "op": "append_block", "block": {"para": ["Via PATCH."]}
        })
        assert r.status_code == 200
        c = _get(server_url, KEY)["content"]
        assert c[-1] == {"para": ["Via PATCH."]}

    def test_patch_method_get_with_block_ids(self, server_url, fresh_doc):
        r = requests.patch(f"{server_url}/{KEY}", json={"op": "get_with_block_ids"})
        assert r.status_code == 200
        body = r.json()
        assert "block_ids" in body
        assert "rev" in body
