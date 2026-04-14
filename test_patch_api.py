"""
test_patch_api.py — integration tests for block-level POST /{key} patch operations.

Also tests the auto-reload middleware (mtime-triggered os.execv).

Run: python -m pytest test_patch_api.py -v
"""
import json
import os
import sys
import socket
import tempfile
import threading
import time
import unittest.mock as mock

import pytest
import requests
import uvicorn

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Server on :{port} did not start within {timeout}s")


@pytest.fixture(scope="module")
def server_url():
    from gdata_server import app
    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    wait_for_port(port)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=3.0)


TEST_KEY = "_test/patch/ops"

BASE_DOC = {
    "title": "Test",
    "content": [
        {"heading": {"level": 1, "text": "Title"}},
        {"para": ["First paragraph."]},
        {"para": ["Second paragraph."]},
    ]
}


@pytest.fixture(autouse=True)
def fresh_doc(server_url):
    """Put a fresh BASE_DOC before each test, delete after."""
    r = requests.put(f"{server_url}/{TEST_KEY}", json=BASE_DOC)
    assert r.status_code == 200, f"Setup PUT failed: {r.text}"
    yield
    requests.delete(f"{server_url}/{TEST_KEY}")


def p(server_url, **kwargs) -> requests.Response:
    """POST a patch op to TEST_KEY."""
    return requests.post(f"{server_url}/{TEST_KEY}", json=kwargs)


def doc(server_url) -> dict:
    return requests.get(f"{server_url}/{TEST_KEY}").json()


# ---------------------------------------------------------------------------
# append_block
# ---------------------------------------------------------------------------

class TestAppendBlock:

    def test_appends_at_end(self, server_url):
        r = p(server_url, op="append_block", block={"para": ["Appended."]})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        content = doc(server_url)["content"]
        assert len(content) == 4
        assert content[3] == {"para": ["Appended."]}

    def test_appends_heading(self, server_url):
        r = p(server_url, op="append_block", block={"heading": {"level": 2, "text": "New Section"}})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert content[-1] == {"heading": {"level": 2, "text": "New Section"}}

    def test_missing_block_returns_400(self, server_url):
        r = p(server_url, op="append_block")
        assert r.status_code == 400

    def test_multiple_appends_preserve_order(self, server_url):
        p(server_url, op="append_block", block={"para": ["A"]})
        p(server_url, op="append_block", block={"para": ["B"]})
        p(server_url, op="append_block", block={"para": ["C"]})
        content = doc(server_url)["content"]
        assert len(content) == 6
        assert content[3] == {"para": ["A"]}
        assert content[4] == {"para": ["B"]}
        assert content[5] == {"para": ["C"]}


# ---------------------------------------------------------------------------
# insert_block
# ---------------------------------------------------------------------------

class TestInsertBlock:

    def test_insert_at_zero(self, server_url):
        r = p(server_url, op="insert_block", index=0, block={"para": ["New first."]})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 4
        assert content[0] == {"para": ["New first."]}
        assert content[1] == {"heading": {"level": 1, "text": "Title"}}

    def test_insert_at_middle(self, server_url):
        r = p(server_url, op="insert_block", index=1, block={"para": ["Middle."]})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert content[1] == {"para": ["Middle."]}
        assert content[2] == {"para": ["First paragraph."]}

    def test_insert_at_length_is_append(self, server_url):
        """index == len(content) is valid for insert (appends)."""
        r = p(server_url, op="insert_block", index=3, block={"para": ["Appended via insert."]})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 4
        assert content[3] == {"para": ["Appended via insert."]}

    def test_insert_out_of_range(self, server_url):
        r = p(server_url, op="insert_block", index=99, block={"para": ["OOB."]})
        assert r.status_code == 400

    def test_insert_negative_index(self, server_url):
        r = p(server_url, op="insert_block", index=-1, block={"para": ["x"]})
        assert r.status_code == 400

    def test_missing_index(self, server_url):
        r = p(server_url, op="insert_block", block={"para": ["x"]})
        assert r.status_code == 400

    def test_missing_block(self, server_url):
        r = p(server_url, op="insert_block", index=0)
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# replace_block
# ---------------------------------------------------------------------------

class TestReplaceBlock:

    def test_replace_first(self, server_url):
        r = p(server_url, op="replace_block", index=0, block={"para": ["Replaced first."]})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 3
        assert content[0] == {"para": ["Replaced first."]}

    def test_replace_middle(self, server_url):
        r = p(server_url, op="replace_block", index=1, block={"heading": {"level": 2, "text": "Replaced"}})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert content[1] == {"heading": {"level": 2, "text": "Replaced"}}
        assert len(content) == 3

    def test_replace_last(self, server_url):
        r = p(server_url, op="replace_block", index=2, block={"para": ["New last."]})
        assert r.status_code == 200
        assert doc(server_url)["content"][2] == {"para": ["New last."]}

    def test_replace_out_of_range(self, server_url):
        r = p(server_url, op="replace_block", index=99, block={"para": ["x"]})
        assert r.status_code == 400

    def test_replace_missing_block(self, server_url):
        r = p(server_url, op="replace_block", index=0)
        assert r.status_code == 400

    def test_replace_missing_index(self, server_url):
        r = p(server_url, op="replace_block", block={"para": ["x"]})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# delete_block
# ---------------------------------------------------------------------------

class TestDeleteBlock:

    def test_delete_first(self, server_url):
        r = p(server_url, op="delete_block", index=0)
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 2
        assert content[0] == {"para": ["First paragraph."]}

    def test_delete_middle(self, server_url):
        r = p(server_url, op="delete_block", index=1)
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 2
        assert content[0] == {"heading": {"level": 1, "text": "Title"}}
        assert content[1] == {"para": ["Second paragraph."]}

    def test_delete_last(self, server_url):
        r = p(server_url, op="delete_block", index=2)
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 2
        assert content[-1] == {"para": ["First paragraph."]}

    def test_delete_out_of_range(self, server_url):
        r = p(server_url, op="delete_block", index=99)
        assert r.status_code == 400

    def test_delete_negative_index(self, server_url):
        r = p(server_url, op="delete_block", index=-1)
        assert r.status_code == 400

    def test_delete_missing_index(self, server_url):
        r = p(server_url, op="delete_block")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# delete_blocks
# ---------------------------------------------------------------------------

class TestDeleteBlocks:

    def test_delete_two(self, server_url):
        r = p(server_url, op="delete_blocks", indices=[0, 2])
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 1
        assert content[0] == {"para": ["First paragraph."]}

    def test_delete_out_of_order(self, server_url):
        """Indices in any order should produce the same result as sorted."""
        r = p(server_url, op="delete_blocks", indices=[2, 0])
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 1
        assert content[0] == {"para": ["First paragraph."]}

    def test_delete_with_duplicates(self, server_url):
        """Duplicate indices are silently deduplicated."""
        r = p(server_url, op="delete_blocks", indices=[1, 1])
        assert r.status_code == 200
        assert len(doc(server_url)["content"]) == 2

    def test_delete_all(self, server_url):
        r = p(server_url, op="delete_blocks", indices=[0, 1, 2])
        assert r.status_code == 200
        assert doc(server_url)["content"] == []

    def test_delete_out_of_range(self, server_url):
        r = p(server_url, op="delete_blocks", indices=[0, 99])
        assert r.status_code == 400

    def test_empty_indices(self, server_url):
        r = p(server_url, op="delete_blocks", indices=[])
        assert r.status_code == 400

    def test_missing_indices(self, server_url):
        r = p(server_url, op="delete_blocks")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# patch_meta
# ---------------------------------------------------------------------------

class TestPatchMeta:

    def test_update_title(self, server_url):
        r = p(server_url, op="patch_meta", fields={"title": "Updated Title"})
        assert r.status_code == 200
        d = doc(server_url)
        assert d["title"] == "Updated Title"
        assert len(d["content"]) == 3  # content untouched

    def test_update_version_and_date(self, server_url):
        r = p(server_url, op="patch_meta", fields={"version": 7, "updated": "2026-04-14"})
        assert r.status_code == 200
        d = doc(server_url)
        assert d["version"] == 7
        assert d["updated"] == "2026-04-14"

    def test_add_new_top_level_field(self, server_url):
        r = p(server_url, op="patch_meta", fields={"tags": ["notes", "test"]})
        assert r.status_code == 200
        assert doc(server_url)["tags"] == ["notes", "test"]

    def test_content_forbidden(self, server_url):
        r = p(server_url, op="patch_meta", fields={"content": []})
        assert r.status_code == 400

    def test_missing_fields(self, server_url):
        r = p(server_url, op="patch_meta")
        assert r.status_code == 400

    def test_fields_not_object(self, server_url):
        r = p(server_url, op="patch_meta", fields=["not", "an", "object"])
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestPatchErrors:

    def test_unknown_op(self, server_url):
        r = p(server_url, op="explode")
        assert r.status_code == 400

    def test_missing_key_404(self, server_url):
        r = requests.post(
            f"{server_url}/_test/no-such-key-xyz",
            json={"op": "append_block", "block": {"para": ["x"]}}
        )
        assert r.status_code == 404

    def test_string_content_not_patchable(self, server_url):
        """Shorthand string content cannot be block-patched — caller must PUT first."""
        requests.put(f"{server_url}/{TEST_KEY}", json={"title": "T", "content": "Just a string."})
        r = p(server_url, op="append_block", block={"para": ["x"]})
        assert r.status_code == 400

    def test_no_content_key_returns_409(self, server_url):
        requests.put(f"{server_url}/{TEST_KEY}", json={"title": "No content here"})
        r = p(server_url, op="append_block", block={"para": ["x"]})
        assert r.status_code == 409

    def test_invalid_json_body(self, server_url):
        r = requests.post(
            f"{server_url}/{TEST_KEY}",
            data="{not json}",
            headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# v2: sidecar / block IDs / rev / ETag
# ---------------------------------------------------------------------------

class TestSidecarAndBlockIds:

    def test_get_with_block_ids_returns_structure(self, server_url):
        r = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"})
        assert r.status_code == 200
        body = r.json()
        assert body["document"]["title"] == "Test"
        assert body["rev"].startswith("r")
        assert len(body["block_ids"]) == 3

    def test_block_ids_stable_across_reads(self, server_url):
        r1 = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"}).json()
        r2 = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"}).json()
        assert r1["block_ids"] == r2["block_ids"]
        assert r1["rev"] == r2["rev"]

    def test_rev_increments_after_patch(self, server_url):
        r1 = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"}).json()
        p(server_url, op="append_block", block={"para": ["x"]})
        r2 = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"}).json()
        assert r2["rev"] != r1["rev"]
        # rev is monotonically incrementing
        assert int(r2["rev"][1:]) > int(r1["rev"][1:])

    def test_put_regenerates_block_ids(self, server_url):
        r1 = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"}).json()
        requests.put(f"{server_url}/{TEST_KEY}", json=BASE_DOC)
        r2 = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"}).json()
        # IDs are regenerated; rev is incremented
        assert r2["block_ids"] != r1["block_ids"]
        assert int(r2["rev"][1:]) > int(r1["rev"][1:])

    def test_etag_header_on_get(self, server_url):
        # Ensure sidecar exists
        requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"})
        r = requests.get(f"{server_url}/{TEST_KEY}")
        assert "ETag" in r.headers
        etag = r.headers["ETag"]
        assert etag.startswith('"r') and etag.endswith('"')

    def test_patch_response_includes_rev(self, server_url):
        r = p(server_url, op="append_block", block={"para": ["z"]})
        assert r.status_code == 200
        body = r.json()
        assert "rev" in body
        assert body["rev"].startswith("r")

    def test_append_block_returns_inserted_block_id(self, server_url):
        r = p(server_url, op="append_block", block={"para": ["z"]})
        body = r.json()
        assert "inserted_block_id" in body
        assert len(body["inserted_block_id"]) == 6

    def test_inserted_id_appears_in_block_ids(self, server_url):
        r = p(server_url, op="append_block", block={"para": ["z"]})
        new_id = r.json()["inserted_block_id"]
        ids = requests.post(f"{server_url}/{TEST_KEY}",
                            json={"op": "get_with_block_ids"}).json()["block_ids"]
        assert new_id in ids


# ---------------------------------------------------------------------------
# v2: ID-based single ops
# ---------------------------------------------------------------------------

class TestIdBasedOps:

    def _ids(self, server_url) -> dict:
        r = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "get_with_block_ids"}).json()
        return {"rev": r["rev"], "block_ids": r["block_ids"]}

    def test_replace_block_by_id(self, server_url):
        info = self._ids(server_url)
        bid = info["block_ids"][1]
        r = p(server_url, op="replace_block", block_id=bid, block={"para": ["Replaced."]})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert content[1] == {"para": ["Replaced."]}
        # block_id at position 1 is unchanged (same slot)
        ids_after = self._ids(server_url)["block_ids"]
        assert ids_after[1] == bid

    def test_delete_block_by_id(self, server_url):
        info = self._ids(server_url)
        bid = info["block_ids"][1]
        r = p(server_url, op="delete_block", block_id=bid)
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert len(content) == 2
        # bid no longer present
        ids_after = self._ids(server_url)["block_ids"]
        assert bid not in ids_after

    def test_insert_before(self, server_url):
        info = self._ids(server_url)
        bid = info["block_ids"][1]
        r = p(server_url, op="insert_before", block_id=bid, block={"para": ["Before."]})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert content[1] == {"para": ["Before."]}
        assert content[2] == {"para": ["First paragraph."]}
        new_id = r.json()["inserted_block_id"]
        ids_after = self._ids(server_url)["block_ids"]
        assert ids_after[1] == new_id
        assert ids_after[2] == bid

    def test_insert_after(self, server_url):
        info = self._ids(server_url)
        bid = info["block_ids"][1]
        r = p(server_url, op="insert_after", block_id=bid, block={"para": ["After."]})
        assert r.status_code == 200
        content = doc(server_url)["content"]
        assert content[2] == {"para": ["After."]}
        assert content[1] == {"para": ["First paragraph."]}
        new_id = r.json()["inserted_block_id"]
        ids_after = self._ids(server_url)["block_ids"]
        assert ids_after[2] == new_id
        assert ids_after[1] == bid

    def test_replace_by_bad_id_returns_404(self, server_url):
        r = p(server_url, op="replace_block", block_id="xxxxxx", block={"para": ["x"]})
        assert r.status_code == 404

    def test_delete_by_bad_id_returns_404(self, server_url):
        r = p(server_url, op="delete_block", block_id="xxxxxx")
        assert r.status_code == 404

    def test_insert_before_bad_id_returns_404(self, server_url):
        r = p(server_url, op="insert_before", block_id="xxxxxx", block={"para": ["x"]})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# v2: batch op
# ---------------------------------------------------------------------------

class TestBatchOp:

    def _get(self, server_url) -> dict:
        return requests.post(f"{server_url}/{TEST_KEY}",
                             json={"op": "get_with_block_ids"}).json()

    def test_batch_atomic_success(self, server_url):
        info = self._get(server_url)
        bid1 = info["block_ids"][1]
        bid2 = info["block_ids"][2]
        r = requests.post(f"{server_url}/{TEST_KEY}", json={
            "op": "batch",
            "if_rev": info["rev"],
            "ops": [
                {"op": "replace_block", "block_id": bid1, "block": {"para": ["New p1."]}},
                {"op": "delete_block", "block_id": bid2},
                {"op": "patch_meta", "fields": {"version": 9}},
            ]
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "rev" in body
        assert body["inserted_block_ids"] == []
        d = doc(server_url)
        assert d["content"][1] == {"para": ["New p1."]}
        assert len(d["content"]) == 2
        assert d["version"] == 9

    def test_batch_returns_inserted_block_ids(self, server_url):
        r = requests.post(f"{server_url}/{TEST_KEY}", json={
            "op": "batch",
            "ops": [
                {"op": "append_block", "block": {"para": ["A"]}},
                {"op": "append_block", "block": {"para": ["B"]}},
            ]
        })
        assert r.status_code == 200
        body = r.json()
        assert len(body["inserted_block_ids"]) == 2

    def test_batch_rollback_on_bad_op(self, server_url):
        """If any op fails, no changes are committed."""
        info = self._get(server_url)
        r = requests.post(f"{server_url}/{TEST_KEY}", json={
            "op": "batch",
            "ops": [
                {"op": "append_block", "block": {"para": ["ok"]}},
                {"op": "replace_block", "block_id": "xxxxxx", "block": {"para": ["bad"]}},
            ]
        })
        assert r.status_code == 404
        # Content unchanged
        assert len(doc(server_url)["content"]) == 3

    def test_batch_if_rev_match(self, server_url):
        info = self._get(server_url)
        r = requests.post(f"{server_url}/{TEST_KEY}", json={
            "op": "batch",
            "if_rev": info["rev"],
            "ops": [{"op": "patch_meta", "fields": {"title": "New"}}],
        })
        assert r.status_code == 200

    def test_batch_if_rev_mismatch_returns_409(self, server_url):
        r = requests.post(f"{server_url}/{TEST_KEY}", json={
            "op": "batch",
            "if_rev": "r999",
            "ops": [{"op": "patch_meta", "fields": {"title": "x"}}],
        })
        assert r.status_code == 409

    def test_batch_empty_ops_returns_400(self, server_url):
        r = requests.post(f"{server_url}/{TEST_KEY}", json={"op": "batch", "ops": []})
        assert r.status_code == 400

    def test_single_patch_if_rev_mismatch_returns_409(self, server_url):
        r = p(server_url, op="append_block", block={"para": ["x"]},
              if_rev="r999")
        assert r.status_code == 409

    def test_put_if_match_mismatch_returns_412(self, server_url):
        r = requests.put(
            f"{server_url}/{TEST_KEY}",
            json=BASE_DOC,
            headers={"If-Match": '"r999"'},
        )
        assert r.status_code == 412

    def test_put_if_match_correct(self, server_url):
        info = self._get(server_url)
        r = requests.put(
            f"{server_url}/{TEST_KEY}",
            json=BASE_DOC,
            headers={"If-Match": f'"{info["rev"]}"'},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Auto-reload middleware (unit test — mocks os.execv)
# ---------------------------------------------------------------------------

class TestAutoReload:

    def test_execv_called_when_mtime_changes(self, server_url):
        """Middleware must call os.execv when source file mtime changes."""
        import gdata_server as gs

        original_mtime = gs._SOURCE_MTIME

        with mock.patch("gdata_server.os.path.getmtime", return_value=original_mtime + 1), \
             mock.patch("gdata_server.os.execv") as mock_execv:
            # Make any request to trigger the middleware
            requests.get(f"{server_url}/_test/trigger-reload")
            mock_execv.assert_called_once_with(sys.executable, [sys.executable] + sys.argv)

    def test_no_execv_when_mtime_unchanged(self, server_url):
        """Middleware must not call os.execv when source file is unchanged."""
        import gdata_server as gs

        with mock.patch("gdata_server.os.path.getmtime", return_value=gs._SOURCE_MTIME), \
             mock.patch("gdata_server.os.execv") as mock_execv:
            requests.get(f"{server_url}/_test/no-reload")
            mock_execv.assert_not_called()
