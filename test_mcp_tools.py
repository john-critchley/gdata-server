"""
test_mcp_tools.py — comprehensive MCP tool coverage.

Exercises every tool (get, put, delete, keys, patch, batch, table_op) and
every operation variant against a local in-process test server.

All notes created/modified/deleted use keys under test/mcp-tools/.

Run:
    python -m pytest test_mcp_tools.py -v
    python -m pytest test_mcp_tools.py -v -k TestTableOp

MAINTENANCE: update this file whenever the MCP interface changes — new tools,
new ops, new parameters, or changed error behaviour. The tool descriptions in
gdata_mcp_server.py list_tools() are the canonical interface spec.

Known gaps in the server interface (as of 2026-06):
  - table.deduplicate: implemented and working but was absent from the table_op
    tool description in list_tools() until 2026-06-26 (now fixed).
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time

# Set env vars before importing gdata modules that read them at module load.
_token_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_token_file.close()
os.environ.setdefault("OAUTH_TOKEN_FILE", _token_file.name)
os.environ.setdefault("MCP_SSE", "false")        # transport tests are in test_mcp_transports.py
os.environ.setdefault("MCP_STREAMABLE", "true")

sys.path.insert(0, os.path.dirname(__file__))

import pytest
import uvicorn

import gdata_oauth
from gdata_mcp_server import make_mcp_app
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


BEARER = "test-bearer-mcp-tools"
TIMEOUT = 10
BASE = "test/mcp-tools"  # all test keys are children of this prefix


# ---------------------------------------------------------------------------
# Server infrastructure
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


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


def _parse(content) -> dict:
    return json.loads(content[0].text)


@pytest.fixture(scope="module")
def db_file():
    with tempfile.NamedTemporaryFile(suffix=".gdbm", delete=False) as f:
        path = f.name
    yield path
    for p in (path, path + ".db"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


@pytest.fixture(scope="module")
def server_url(db_file):
    gdata_oauth._store = None
    gdata_oauth._get_store().save_token(BEARER)

    app = make_mcp_app(db_file)
    port = _free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    _wait_port(port)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Client helper mixin — all test classes inherit this
# ---------------------------------------------------------------------------

class _MCP:
    """Minimal synchronous MCP client wrappers over streamable HTTP."""

    def _client(self, server_url):
        return streamablehttp_client(
            f"{server_url}/mcp",
            headers={"Authorization": f"Bearer {BEARER}"},
        )

    def _call(self, server_url, tool: str, args: dict) -> dict:
        async def go():
            async with self._client(server_url) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return _parse((await s.call_tool(tool, args)).content)
        return _run(go())

    # --- shorthand wrappers ---

    def _get(self, server_url, key: str, **kw) -> dict:
        return self._call(server_url, "get", {"key": key, **kw})

    def _put(self, server_url, key: str, value, **kw) -> dict:
        return self._call(server_url, "put", {"key": key, "value": value, **kw})

    def _delete(self, server_url, key: str) -> dict:
        return self._call(server_url, "delete", {"key": key})

    def _keys(self, server_url) -> list:
        return self._call(server_url, "keys", {})["keys"]

    def _patch(self, server_url, key: str, **kw) -> dict:
        return self._call(server_url, "patch", {"key": key, **kw})

    def _batch(self, server_url, key: str, ops, **kw) -> dict:
        return self._call(server_url, "batch", {"key": key, "ops": ops, **kw})

    def _table_op(self, server_url, key: str, op: str, block=0, **kw) -> dict:
        return self._call(server_url, "table_op",
                          {"key": key, "op": op, "block": block, **kw})


# ---------------------------------------------------------------------------
# TestGet
# ---------------------------------------------------------------------------

class TestGet(_MCP):
    KEY = f"{BASE}/get"

    def test_get_returns_stored_value(self, server_url):
        self._put(server_url, self.KEY, {"a": 1, "b": [2, 3]})
        r = self._get(server_url, self.KEY)
        assert r == {"a": 1, "b": [2, 3]}
        self._delete(server_url, self.KEY)

    def test_get_missing_key_returns_error(self, server_url):
        r = self._get(server_url, f"{BASE}/no-such-key")
        assert "error" in r

    def test_get_with_block_ids_returns_document_rev_and_ids(self, server_url):
        doc = {"title": "T", "content": [{"para": ["x"]}, {"para": ["y"]}]}
        self._put(server_url, self.KEY, doc)
        r = self._get(server_url, self.KEY, include_block_ids=True)
        assert "document" in r
        assert r["document"] == doc
        assert r["rev"].startswith("r")
        assert len(r["block_ids"]) == 2
        self._delete(server_url, self.KEY)

    def test_get_with_block_ids_missing_key_returns_error(self, server_url):
        r = self._get(server_url, f"{BASE}/no-such-key", include_block_ids=True)
        assert "error" in r

    def test_get_non_jsonhtl_value(self, server_url):
        self._put(server_url, self.KEY, [1, "two", None])
        r = self._get(server_url, self.KEY)
        assert r == [1, "two", None]
        self._delete(server_url, self.KEY)


# ---------------------------------------------------------------------------
# TestPut
# ---------------------------------------------------------------------------

class TestPut(_MCP):
    KEY = f"{BASE}/put"

    def test_put_returns_status_rev_and_block_ids(self, server_url):
        doc = {"title": "T", "content": [{"para": ["x"]}]}
        r = self._put(server_url, self.KEY, doc)
        assert r["status"] == "ok"
        assert r["rev"].startswith("r")
        assert len(r["block_ids"]) == 1
        self._delete(server_url, self.KEY)

    def test_put_block_ids_count_matches_content_length(self, server_url):
        doc = {"title": "T", "content": [{"para": ["a"]}, {"para": ["b"]}, {"para": ["c"]}]}
        r = self._put(server_url, self.KEY, doc)
        assert len(r["block_ids"]) == 3
        self._delete(server_url, self.KEY)

    def test_put_increments_rev_on_overwrite(self, server_url):
        doc = {"title": "T", "content": [{"para": ["x"]}]}
        r1 = self._put(server_url, self.KEY, doc)
        r2 = self._put(server_url, self.KEY, doc)
        assert int(r2["rev"][1:]) > int(r1["rev"][1:])
        self._delete(server_url, self.KEY)

    def test_put_if_rev_correct_succeeds(self, server_url):
        doc = {"title": "T", "content": []}
        r1 = self._put(server_url, self.KEY, doc)
        r2 = self._put(server_url, self.KEY, doc, if_rev=r1["rev"])
        assert r2["status"] == "ok"
        self._delete(server_url, self.KEY)

    def test_put_if_rev_mismatch_returns_412(self, server_url):
        doc = {"title": "T", "content": []}
        self._put(server_url, self.KEY, doc)
        r = self._put(server_url, self.KEY, doc, if_rev="r999")
        assert "error" in r
        assert r.get("status_code") == 412
        self._delete(server_url, self.KEY)

    def test_put_non_dict_value(self, server_url):
        r = self._put(server_url, self.KEY, [1, 2, 3])
        assert r["status"] == "ok"
        assert self._get(server_url, self.KEY) == [1, 2, 3]
        self._delete(server_url, self.KEY)

    def test_put_value_as_json_string_is_unwrapped(self, server_url):
        """put() unwraps a value passed as a JSON-encoded string."""
        obj = {"x": 42}
        r = self._call(server_url, "put", {"key": self.KEY, "value": json.dumps(obj)})
        assert r["status"] == "ok"
        assert self._get(server_url, self.KEY) == obj
        self._delete(server_url, self.KEY)


# ---------------------------------------------------------------------------
# TestDelete
# ---------------------------------------------------------------------------

class TestDelete(_MCP):
    KEY = f"{BASE}/delete"

    def test_delete_existing_returns_deleted_status(self, server_url):
        self._put(server_url, self.KEY, {"x": 1})
        r = self._delete(server_url, self.KEY)
        assert r == {"status": "deleted"}

    def test_delete_makes_key_absent_from_get(self, server_url):
        self._put(server_url, self.KEY, {"x": 1})
        self._delete(server_url, self.KEY)
        r = self._get(server_url, self.KEY)
        assert "error" in r

    def test_delete_removes_key_from_keys_list(self, server_url):
        self._put(server_url, self.KEY, {"x": 1})
        self._delete(server_url, self.KEY)
        assert self.KEY not in self._keys(server_url)

    def test_delete_nonexistent_key_returns_error(self, server_url):
        r = self._delete(server_url, f"{BASE}/no-such-key-to-delete")
        assert "error" in r


# ---------------------------------------------------------------------------
# TestKeys
# ---------------------------------------------------------------------------

class TestKeys(_MCP):
    KEY = f"{BASE}/keys-probe"

    def test_keys_returns_a_list(self, server_url):
        r = self._call(server_url, "keys", {})
        assert "keys" in r
        assert isinstance(r["keys"], list)

    def test_keys_includes_newly_created_key(self, server_url):
        self._put(server_url, self.KEY, {"x": 1})
        assert self.KEY in self._keys(server_url)
        self._delete(server_url, self.KEY)

    def test_keys_excludes_deleted_key(self, server_url):
        self._put(server_url, self.KEY, {"x": 1})
        self._delete(server_url, self.KEY)
        assert self.KEY not in self._keys(server_url)

    def test_keys_sorted_alphabetically(self, server_url):
        k = self._keys(server_url)
        assert k == sorted(k)


# ---------------------------------------------------------------------------
# TestPatch
# ---------------------------------------------------------------------------

_PATCH_DOC = {
    "title": "Patch Test",
    "content": [
        {"heading": {"level": 1, "text": "H1"}},
        {"para": ["Para 1."]},
        {"para": ["Para 2."]},
    ],
}


class TestPatch(_MCP):
    KEY = f"{BASE}/patch"

    @pytest.fixture(autouse=True)
    def fresh(self, server_url):
        self._put(server_url, self.KEY, _PATCH_DOC)
        yield
        self._delete(server_url, self.KEY)

    def _content(self, server_url) -> list:
        return self._get(server_url, self.KEY)["content"]

    # --- append_block ---

    def test_append_block(self, server_url):
        r = self._patch(server_url, self.KEY, op="append_block",
                        block={"para": ["Appended."]})
        assert r["status"] == "ok"
        assert "rev" in r
        c = self._content(server_url)
        assert len(c) == 4
        assert c[-1] == {"para": ["Appended."]}

    def test_append_block_returns_inserted_block_id(self, server_url):
        r = self._patch(server_url, self.KEY, op="append_block",
                        block={"para": ["x"]})
        assert "inserted_block_id" in r

    # --- insert_block ---

    def test_insert_block_at_index(self, server_url):
        r = self._patch(server_url, self.KEY, op="insert_block", index=1,
                        block={"para": ["Inserted."]})
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert len(c) == 4
        assert c[1] == {"para": ["Inserted."]}
        assert c[2] == {"para": ["Para 1."]}

    def test_insert_block_at_zero(self, server_url):
        self._patch(server_url, self.KEY, op="insert_block", index=0,
                    block={"para": ["New first."]})
        assert self._content(server_url)[0] == {"para": ["New first."]}

    # --- replace_block ---

    def test_replace_block_by_index(self, server_url):
        r = self._patch(server_url, self.KEY, op="replace_block", index=1,
                        block={"para": ["Replaced."]})
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert len(c) == 3
        assert c[1] == {"para": ["Replaced."]}

    def test_replace_block_by_block_id(self, server_url):
        info = self._get(server_url, self.KEY, include_block_ids=True)
        bid = info["block_ids"][1]
        r = self._patch(server_url, self.KEY, op="replace_block",
                        block_id=bid, block={"para": ["By ID."]})
        assert r["status"] == "ok"
        assert self._content(server_url)[1] == {"para": ["By ID."]}

    # --- delete_block ---

    def test_delete_block_by_index(self, server_url):
        r = self._patch(server_url, self.KEY, op="delete_block", index=1)
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert len(c) == 2
        assert c[1] == {"para": ["Para 2."]}

    def test_delete_block_by_block_id(self, server_url):
        info = self._get(server_url, self.KEY, include_block_ids=True)
        bid = info["block_ids"][1]
        r = self._patch(server_url, self.KEY, op="delete_block", block_id=bid)
        assert r["status"] == "ok"
        assert len(self._content(server_url)) == 2

    # --- delete_blocks ---

    def test_delete_blocks(self, server_url):
        r = self._patch(server_url, self.KEY, op="delete_blocks", indices=[0, 2])
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert len(c) == 1
        assert c[0] == {"para": ["Para 1."]}

    # --- patch_meta ---

    def test_patch_meta(self, server_url):
        r = self._patch(server_url, self.KEY, op="patch_meta",
                        fields={"title": "Updated", "version": 2})
        assert r["status"] == "ok"
        d = self._get(server_url, self.KEY)
        assert d["title"] == "Updated"
        assert d["version"] == 2
        assert len(d["content"]) == 3  # content untouched

    def test_patch_meta_content_key_forbidden(self, server_url):
        r = self._patch(server_url, self.KEY, op="patch_meta",
                        fields={"content": []})
        assert "error" in r

    def test_patch_meta_fields_as_json_string(self, server_url):
        """fields passed as JSON string is unwrapped."""
        r = self._patch(server_url, self.KEY, op="patch_meta",
                        fields=json.dumps({"title": "String-fields"}))
        assert r["status"] == "ok"
        assert self._get(server_url, self.KEY)["title"] == "String-fields"

    # --- insert_before / insert_after (block-ID based) ---

    def test_insert_before_by_block_id(self, server_url):
        info = self._get(server_url, self.KEY, include_block_ids=True)
        bid = info["block_ids"][0]
        r = self._patch(server_url, self.KEY, op="insert_before",
                        block_id=bid, block={"para": ["Before first."]})
        assert "inserted_block_id" in r
        assert self._content(server_url)[0] == {"para": ["Before first."]}

    def test_insert_after_by_block_id(self, server_url):
        info = self._get(server_url, self.KEY, include_block_ids=True)
        bid = info["block_ids"][0]
        r = self._patch(server_url, self.KEY, op="insert_after",
                        block_id=bid, block={"para": ["After first."]})
        assert "inserted_block_id" in r
        assert self._content(server_url)[1] == {"para": ["After first."]}

    # --- block string unwrapping ---

    def test_block_as_json_string_is_unwrapped(self, server_url):
        r = self._patch(server_url, self.KEY, op="append_block",
                        block=json.dumps({"para": ["String block."]}))
        assert r["status"] == "ok"
        assert self._content(server_url)[-1] == {"para": ["String block."]}

    # --- if_rev concurrency ---

    def test_if_rev_correct_succeeds(self, server_url):
        info = self._get(server_url, self.KEY, include_block_ids=True)
        r = self._patch(server_url, self.KEY, op="append_block",
                        block={"para": ["x"]}, if_rev=info["rev"])
        assert r["status"] == "ok"

    def test_if_rev_stale_returns_409(self, server_url):
        r = self._patch(server_url, self.KEY, op="append_block",
                        block={"para": ["x"]}, if_rev="r999")
        assert "error" in r
        assert r.get("status_code") == 409

    def test_rev_increments_after_each_patch(self, server_url):
        r1 = self._get(server_url, self.KEY, include_block_ids=True)
        self._patch(server_url, self.KEY, op="append_block", block={"para": ["x"]})
        r2 = self._get(server_url, self.KEY, include_block_ids=True)
        assert int(r2["rev"][1:]) > int(r1["rev"][1:])

    # --- error cases ---

    def test_patch_missing_key_returns_error(self, server_url):
        r = self._call(server_url, "patch", {
            "key": f"{BASE}/absolutely-no-such-key",
            "op": "append_block",
            "block": {"para": ["x"]},
        })
        assert "error" in r

    def test_patch_index_out_of_range_returns_error(self, server_url):
        r = self._patch(server_url, self.KEY, op="delete_block", index=99)
        assert "error" in r

    def test_patch_invalid_block_id_returns_error(self, server_url):
        r = self._patch(server_url, self.KEY, op="insert_before",
                        block_id="notarealid", block={"para": ["x"]})
        assert "error" in r

    def test_patch_delete_blocks_out_of_range_returns_error(self, server_url):
        r = self._patch(server_url, self.KEY, op="delete_blocks", indices=[0, 99])
        assert "error" in r


# ---------------------------------------------------------------------------
# TestBatch
# ---------------------------------------------------------------------------

class TestBatch(_MCP):
    KEY = f"{BASE}/batch"

    @pytest.fixture(autouse=True)
    def fresh(self, server_url):
        self._put(server_url, self.KEY, _PATCH_DOC)
        yield
        self._delete(server_url, self.KEY)

    def test_batch_multiple_ops_applied_atomically(self, server_url):
        info = self._get(server_url, self.KEY, include_block_ids=True)
        bid1, bid2 = info["block_ids"][1], info["block_ids"][2]
        r = self._batch(server_url, self.KEY, [
            {"op": "replace_block", "block_id": bid1, "block": {"para": ["R1."]}},
            {"op": "delete_block",  "block_id": bid2},
        ], if_rev=info["rev"])
        assert r["status"] == "ok"
        assert r["inserted_block_ids"] == []
        doc = self._get(server_url, self.KEY)
        assert len(doc["content"]) == 2
        assert doc["content"][1] == {"para": ["R1."]}

    def test_batch_appends_return_inserted_block_ids(self, server_url):
        r = self._batch(server_url, self.KEY, [
            {"op": "append_block", "block": {"para": ["A."]}},
            {"op": "append_block", "block": {"para": ["B."]}},
        ])
        assert r["status"] == "ok"
        assert len(r["inserted_block_ids"]) == 2

    def test_batch_if_rev_mismatch_returns_409(self, server_url):
        r = self._batch(server_url, self.KEY, [
            {"op": "patch_meta", "fields": {"title": "x"}},
        ], if_rev="r999")
        assert "error" in r
        assert r.get("status_code") == 409

    def test_batch_ops_as_json_string_are_unwrapped(self, server_url):
        ops = [{"op": "patch_meta", "fields": {"title": "String-ops"}}]
        r = self._batch(server_url, self.KEY, json.dumps(ops))
        assert r["status"] == "ok"
        assert self._get(server_url, self.KEY)["title"] == "String-ops"

    def test_batch_invalid_op_rolls_back_all_changes(self, server_url):
        """A failing op must leave the document unchanged (deep-copy safety)."""
        doc_before = self._get(server_url, self.KEY)
        r = self._batch(server_url, self.KEY, [
            {"op": "patch_meta", "fields": {"title": "Should not persist"}},
            {"op": "delete_block", "index": 99},   # invalid — triggers rollback
        ])
        assert "error" in r
        assert self._get(server_url, self.KEY)["title"] == doc_before["title"]

    def test_batch_with_table_op(self, server_url):
        """table.* ops can be embedded inside a batch."""
        table_doc = {
            "title": "T",
            "content": [{"table": {"columns": ["A", "B"], "rows": [["x", "y"]]}}],
        }
        self._put(server_url, self.KEY, table_doc)
        info = self._get(server_url, self.KEY, include_block_ids=True)
        bid = info["block_ids"][0]
        r = self._batch(server_url, self.KEY, [
            {"op": "table.set_cell", "block": bid, "row": 0, "column": "A", "value": "z"},
        ])
        assert r["status"] == "ok"
        assert self._get(server_url, self.KEY)["content"][0]["table"]["rows"][0][0] == "z"

    def test_batch_increments_rev_once_for_all_ops(self, server_url):
        r1 = self._get(server_url, self.KEY, include_block_ids=True)
        self._batch(server_url, self.KEY, [
            {"op": "append_block", "block": {"para": ["a"]}},
            {"op": "append_block", "block": {"para": ["b"]}},
        ])
        r2 = self._get(server_url, self.KEY, include_block_ids=True)
        assert int(r2["rev"][1:]) == int(r1["rev"][1:]) + 1


# ---------------------------------------------------------------------------
# TestTableOp — every table.* operation
# ---------------------------------------------------------------------------

_TABLE_DOC = {
    "title": "Table Test",
    "content": [{
        "table": {
            "columns": ["Name", "Age", "City"],
            "rows": [
                ["Alice", 30, "London"],
                ["Bob",   25, "Paris"],
                ["Charlie", 35, "Berlin"],
            ],
        }
    }],
}


class TestTableOp(_MCP):
    KEY = f"{BASE}/table"

    @pytest.fixture(autouse=True)
    def fresh(self, server_url):
        self._put(server_url, self.KEY, _TABLE_DOC)
        yield
        self._delete(server_url, self.KEY)

    def _table(self, server_url) -> dict:
        return self._get(server_url, self.KEY)["content"][0]["table"]

    # --- rename_column ---

    def test_rename_column_by_name(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.rename_column",
                           column="Age", new_name="Years")
        assert r["status"] == "ok"
        cols = self._table(server_url)["columns"]
        assert "Years" in cols
        assert "Age" not in cols

    def test_rename_column_by_index(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.rename_column",
                           column=0, new_name="FullName")
        assert r["status"] == "ok"
        assert self._table(server_url)["columns"][0] == "FullName"

    # --- insert_column ---

    def test_insert_column_append(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_column", name="Score")
        assert r["status"] == "ok"
        t = self._table(server_url)
        assert t["columns"][-1] == "Score"
        assert all(len(row) == 4 for row in t["rows"])

    def test_insert_column_at_position_zero(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_column",
                           name="ID", position=0)
        assert r["status"] == "ok"
        assert self._table(server_url)["columns"][0] == "ID"

    def test_insert_column_after_named(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_column",
                           name="Score", after="Name")
        assert r["status"] == "ok"
        assert self._table(server_url)["columns"][1] == "Score"

    def test_insert_column_with_values(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_column",
                           name="Score", values=[10, 20, 30])
        assert r["status"] == "ok"
        t = self._table(server_url)
        ci = t["columns"].index("Score")
        assert [row[ci] for row in t["rows"]] == [10, 20, 30]

    def test_insert_column_with_default(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_column",
                           name="Flag", default=False)
        assert r["status"] == "ok"
        t = self._table(server_url)
        ci = t["columns"].index("Flag")
        assert all(row[ci] is False for row in t["rows"])

    # --- delete_column ---

    def test_delete_column_by_name(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.delete_column", column="Age")
        assert r["status"] == "ok"
        t = self._table(server_url)
        assert "Age" not in t["columns"]
        assert all(len(row) == 2 for row in t["rows"])

    def test_delete_column_by_index(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.delete_column", column=2)
        assert r["status"] == "ok"
        assert len(self._table(server_url)["columns"]) == 2

    # --- move_column ---

    def test_move_column_to_position(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.move_column",
                           column="City", to=0)
        assert r["status"] == "ok"
        t = self._table(server_url)
        assert t["columns"][0] == "City"
        assert t["rows"][0][0] == "London"  # row data follows column

    def test_move_column_after_named(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.move_column",
                           column="Name", after="Age")
        assert r["status"] == "ok"
        t = self._table(server_url)
        name_i = t["columns"].index("Name")
        age_i  = t["columns"].index("Age")
        assert name_i == age_i + 1

    # --- reorder_columns ---

    def test_reorder_columns(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.reorder_columns",
                           order=["City", "Name", "Age"])
        assert r["status"] == "ok"
        t = self._table(server_url)
        assert t["columns"] == ["City", "Name", "Age"]
        assert t["rows"][0] == ["London", "Alice", 30]

    # --- fill_column ---

    def test_fill_column(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.fill_column",
                           column="Age", value=0)
        assert r["status"] == "ok"
        t = self._table(server_url)
        ci = t["columns"].index("Age")
        assert all(row[ci] == 0 for row in t["rows"])

    # --- set_columns ---

    def test_set_columns(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.set_columns",
                           columns=["X", "Y", "Z"])
        assert r["status"] == "ok"
        assert self._table(server_url)["columns"] == ["X", "Y", "Z"]

    # --- insert_row ---

    def test_insert_row_as_list(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_row",
                           position=0, values=["Zoe", 28, "Tokyo"])
        assert r["status"] == "ok"
        t = self._table(server_url)
        assert len(t["rows"]) == 4
        assert t["rows"][0] == ["Zoe", 28, "Tokyo"]

    def test_insert_row_as_dict(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_row",
                           position=0, values={"Name": "Zoe", "Age": 28, "City": "Tokyo"})
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][0] == ["Zoe", 28, "Tokyo"]

    def test_insert_row_at_end(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.insert_row",
                           position=3, values=["Diana", 22, "Rome"])
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][-1] == ["Diana", 22, "Rome"]

    # --- append_row ---

    def test_append_row_as_list(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.append_row",
                           values=["Diana", 22, "Rome"])
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][-1] == ["Diana", 22, "Rome"]

    def test_append_row_as_dict(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.append_row",
                           values={"Name": "Eve", "Age": 19, "City": "Oslo"})
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][-1] == ["Eve", 19, "Oslo"]

    # --- delete_row ---

    def test_delete_row_by_index(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.delete_row", row=1)
        assert r["status"] == "ok"
        t = self._table(server_url)
        assert len(t["rows"]) == 2
        assert t["rows"][1][0] == "Charlie"

    def test_delete_row_by_index_col_value(self, server_url):
        """delete_row supports lookup by index column value (undocumented 'index' param)."""
        self._table_op(server_url, self.KEY, "table.set_index", column="Name")
        r = self._call(server_url, "table_op", {
            "key": self.KEY, "op": "table.delete_row", "block": 0, "index": "Bob",
        })
        assert r["status"] == "ok"
        names = [row[0] for row in self._table(server_url)["rows"]]
        assert "Bob" not in names

    # --- move_row ---

    def test_move_row(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.move_row", row=0, to=2)
        assert r["status"] == "ok"
        t = self._table(server_url)
        assert t["rows"][2][0] == "Alice"
        assert t["rows"][0][0] == "Bob"

    # --- sort ---

    def test_sort_ascending(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.sort", by="Age")
        assert r["status"] == "ok"
        ages = [row[1] for row in self._table(server_url)["rows"]]
        assert ages == sorted(ages)

    def test_sort_descending(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.sort",
                           by="Age", ascending=False)
        assert r["status"] == "ok"
        ages = [row[1] for row in self._table(server_url)["rows"]]
        assert ages == sorted(ages, reverse=True)

    def test_sort_multi_column(self, server_url):
        self._table_op(server_url, self.KEY, "table.append_row",
                       values=["Aaron", 25, "Athens"])  # same Age as Bob
        r = self._table_op(server_url, self.KEY, "table.sort",
                           by=["Age", "Name"])
        assert r["status"] == "ok"
        t = self._table(server_url)
        age_25_names = [row[0] for row in t["rows"] if row[1] == 25]
        assert age_25_names == sorted(age_25_names)

    # --- fill_row ---

    def test_fill_row(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.fill_row", row=1, value="x")
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][1] == ["x", "x", "x"]

    # --- set_cell ---

    def test_set_cell_by_column_name(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.set_cell",
                           row=0, column="City", value="New York")
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][0][2] == "New York"

    def test_set_cell_by_column_index(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.set_cell",
                           row=0, column=1, value=99)
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][0][1] == 99

    def test_set_cell_null_value(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.set_cell",
                           row=0, column="Age", value=None)
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][0][1] is None

    # --- set_caption ---

    def test_set_caption(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.set_caption",
                           caption="My Table")
        assert r["status"] == "ok"
        assert self._table(server_url).get("caption") == "My Table"

    def test_clear_caption(self, server_url):
        self._table_op(server_url, self.KEY, "table.set_caption", caption="My Table")
        r = self._table_op(server_url, self.KEY, "table.set_caption", caption=None)
        assert r["status"] == "ok"
        assert "caption" not in self._table(server_url)

    # --- transpose ---

    def test_transpose(self, server_url):
        t_before = self._table(server_url)
        n_cols = len(t_before["columns"])
        n_rows = len(t_before["rows"])
        r = self._table_op(server_url, self.KEY, "table.transpose")
        assert r["status"] == "ok"
        t = self._table(server_url)
        # new cols = first_col_header + first_col_values (n_rows items)
        assert len(t["columns"]) == 1 + n_rows
        # new rows = one per original non-first column
        assert len(t["rows"]) == n_cols - 1

    # --- set_index / clear_index ---

    def test_set_index_stores_index_col(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.set_index", column="Name")
        assert r["status"] == "ok"
        assert self._table(server_url).get("index_col") == "Name"

    def test_clear_index_removes_index_col(self, server_url):
        self._table_op(server_url, self.KEY, "table.set_index", column="Name")
        r = self._table_op(server_url, self.KEY, "table.clear_index")
        assert r["status"] == "ok"
        assert "index_col" not in self._table(server_url)

    # --- replace ---

    def test_replace_across_table(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.replace",
                           old_value="London", new_value="Manchester")
        assert r["status"] == "ok"
        assert r["replaced"] == 1
        cities = [row[2] for row in self._table(server_url)["rows"]]
        assert "Manchester" in cities
        assert "London" not in cities

    def test_replace_in_single_column(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.replace",
                           column="Age", old_value=30, new_value=31)
        assert r["status"] == "ok"
        ages = [row[1] for row in self._table(server_url)["rows"]]
        assert 31 in ages
        assert 30 not in ages

    def test_replace_no_matches_returns_zero(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.replace",
                           old_value="NoSuchValue", new_value="x")
        assert r["status"] == "ok"
        assert r["replaced"] == 0

    # --- deduplicate ---

    def test_deduplicate_keep_first(self, server_url):
        self._table_op(server_url, self.KEY, "table.append_row",
                       values=["Alice", 30, "London"])  # exact duplicate
        r = self._table_op(server_url, self.KEY, "table.deduplicate", keep="first")
        assert r["status"] == "ok"
        assert r["removed"] == 1
        assert len(self._table(server_url)["rows"]) == 3

    def test_deduplicate_keep_last(self, server_url):
        self._table_op(server_url, self.KEY, "table.append_row",
                       values=["Alice", 30, "London"])
        r = self._table_op(server_url, self.KEY, "table.deduplicate", keep="last")
        assert r["status"] == "ok"
        assert r["removed"] == 1
        assert len(self._table(server_url)["rows"]) == 3

    def test_deduplicate_keep_none_removes_all_duplicates(self, server_url):
        self._table_op(server_url, self.KEY, "table.append_row",
                       values=["Alice", 30, "London"])
        r = self._table_op(server_url, self.KEY, "table.deduplicate", keep="none")
        assert r["status"] == "ok"
        assert r["removed"] == 2  # original + duplicate both gone
        assert len(self._table(server_url)["rows"]) == 2

    def test_deduplicate_subset_of_columns(self, server_url):
        self._table_op(server_url, self.KEY, "table.append_row",
                       values=["Alice", 99, "Tokyo"])  # same Name, different rest
        r = self._table_op(server_url, self.KEY, "table.deduplicate",
                           subset=["Name"], keep="first")
        assert r["status"] == "ok"
        assert r["removed"] == 1

    def test_deduplicate_no_duplicates_removes_nothing(self, server_url):
        r = self._table_op(server_url, self.KEY, "table.deduplicate")
        assert r["status"] == "ok"
        assert r["removed"] == 0

    # --- block addressing ---

    def test_table_op_addressed_by_block_id_string(self, server_url):
        """Block can be addressed by its stable block_id string, not just index."""
        info = self._get(server_url, self.KEY, include_block_ids=True)
        bid = info["block_ids"][0]
        r = self._table_op(server_url, self.KEY, "table.set_cell",
                           block=bid, row=0, column="Age", value=42)
        assert r["status"] == "ok"
        assert self._table(server_url)["rows"][0][1] == 42

    # --- if_rev concurrency ---

    def test_table_op_if_rev_stale_returns_409(self, server_url):
        r = self._call(server_url, "table_op", {
            "key": self.KEY, "op": "table.set_cell", "block": 0,
            "row": 0, "column": "Age", "value": 99, "if_rev": "r999",
        })
        assert "error" in r
        assert r.get("status_code") == 409

    def test_table_op_if_rev_correct_succeeds(self, server_url):
        info = self._get(server_url, self.KEY, include_block_ids=True)
        r = self._call(server_url, "table_op", {
            "key": self.KEY, "op": "table.set_cell", "block": 0,
            "row": 0, "column": "Age", "value": 99, "if_rev": info["rev"],
        })
        assert r["status"] == "ok"

    # --- error cases ---

    def test_bad_block_ref_returns_error(self, server_url):
        r = self._call(server_url, "table_op", {
            "key": self.KEY, "op": "table.set_cell",
            "block": "notarealblockid", "row": 0, "column": "Age", "value": 1,
        })
        assert "error" in r

    def test_bad_column_ref_returns_error(self, server_url):
        r = self._call(server_url, "table_op", {
            "key": self.KEY, "op": "table.rename_column",
            "block": 0, "column": "NoSuchColumn", "new_name": "X",
        })
        assert "error" in r

    def test_unknown_table_op_returns_error(self, server_url):
        r = self._call(server_url, "table_op", {
            "key": self.KEY, "op": "table.no_such_op", "block": 0,
        })
        assert "error" in r

    def test_table_op_on_nonexistent_key_returns_error(self, server_url):
        r = self._call(server_url, "table_op", {
            "key": f"{BASE}/no-such-key", "op": "table.set_cell",
            "block": 0, "row": 0, "column": "A", "value": 1,
        })
        assert "error" in r

    def test_delete_row_out_of_range_returns_error(self, server_url):
        r = self._call(server_url, "table_op", {
            "key": self.KEY, "op": "table.delete_row", "block": 0, "row": 99,
        })
        assert "error" in r


# ---------------------------------------------------------------------------
# TestReorder — block reorder op via MCP tool
# ---------------------------------------------------------------------------

_REORDER_DOC = {
    "title": "Reorder Test",
    "content": [
        {"heading": {"level": 1, "text": "First"}},
        {"para": ["Second."]},
        {"para": ["Third."]},
        {"para": ["Fourth."]},
    ],
}


class TestReorder(_MCP):
    KEY = f"{BASE}/reorder"

    @pytest.fixture(autouse=True)
    def fresh(self, server_url):
        r = self._put(server_url, self.KEY, _REORDER_DOC)
        self._rev = r["rev"]
        self._ids = r["block_ids"]
        yield
        self._delete(server_url, self.KEY)

    def _content(self, server_url) -> list:
        return self._get(server_url, self.KEY)["content"]

    def _reorder(self, server_url, order, **kw) -> dict:
        return self._call(server_url, "reorder", {"key": self.KEY, "order": order, **kw})

    def test_reverse_order(self, server_url):
        reversed_ids = list(reversed(self._ids))
        r = self._reorder(server_url, reversed_ids)
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert c[0] == {"para": ["Fourth."]}
        assert c[1] == {"para": ["Third."]}
        assert c[2] == {"para": ["Second."]}
        assert c[3] == {"heading": {"level": 1, "text": "First"}}

    def test_move_last_to_first(self, server_url):
        new_order = [self._ids[3]] + self._ids[:3]
        r = self._reorder(server_url, new_order)
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert c[0] == {"para": ["Fourth."]}
        assert c[1] == {"heading": {"level": 1, "text": "First"}}

    def test_identity_reorder_preserves_content(self, server_url):
        r = self._reorder(server_url, self._ids)
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert c[0] == {"heading": {"level": 1, "text": "First"}}
        assert c[3] == {"para": ["Fourth."]}

    def test_rev_increments_after_reorder(self, server_url):
        self._reorder(server_url, list(reversed(self._ids)))
        info = self._get(server_url, self.KEY, include_block_ids=True)
        assert int(info["rev"][1:]) > int(self._rev[1:])

    def test_block_ids_unchanged_after_reorder(self, server_url):
        self._reorder(server_url, list(reversed(self._ids)))
        info = self._get(server_url, self.KEY, include_block_ids=True)
        assert set(info["block_ids"]) == set(self._ids)

    def test_if_rev_correct_succeeds(self, server_url):
        r = self._reorder(server_url, list(reversed(self._ids)), if_rev=self._rev)
        assert r["status"] == "ok"

    def test_if_rev_stale_returns_409(self, server_url):
        r = self._reorder(server_url, self._ids, if_rev="r9999")
        assert "error" in r
        assert r.get("status_code") == 409

    def test_missing_id_rejected(self, server_url):
        incomplete = self._ids[:3]  # omit last
        r = self._reorder(server_url, incomplete)
        assert "error" in r
        assert r.get("status_code") == 422

    def test_unknown_id_rejected(self, server_url):
        bad_order = self._ids[:3] + ["BOGUSID"]
        r = self._reorder(server_url, bad_order)
        assert "error" in r
        assert r.get("status_code") == 422

    def test_duplicate_id_rejected(self, server_url):
        dup_order = [self._ids[0]] + self._ids  # first ID appears twice
        r = self._reorder(server_url, dup_order)
        assert "error" in r
        assert r.get("status_code") == 422

    def test_reorder_usable_inside_batch(self, server_url):
        reversed_ids = list(reversed(self._ids))
        r = self._batch(server_url, self.KEY, [
            {"op": "reorder", "order": reversed_ids},
        ])
        assert r["status"] == "ok"
        c = self._content(server_url)
        assert c[0] == {"para": ["Fourth."]}


# ---------------------------------------------------------------------------
# TestListTools — verify the advertised tool set
# ---------------------------------------------------------------------------

class TestListTools(_MCP):

    def test_all_expected_tools_present(self, server_url):
        async def go():
            async with self._client(server_url) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    result = await s.list_tools()
                    return {t.name for t in result.tools}
        names = _run(go())
        expected = {"get", "put", "delete", "keys", "patch", "batch", "table_op", "reorder"}
        assert expected <= names, f"Missing tools: {expected - names}"
