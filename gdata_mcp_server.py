#!/usr/bin/python
"""
gdata_mcp_server.py — combined REST + MCP server over gdbm.

Runs two uvicorn servers in one process (shared db, no locking conflicts):
  - REST API on --rest-port (default 8020)  — same interface as gdata_server.py
  - MCP      on --mcp-port  (default 8022)  — SSE and/or Streamable HTTP

Transport flags (env vars, default both enabled):
  MCP_SSE=true|false          enable SSE transport  (GET /mcp/ + POST /mcp/messages)
  MCP_STREAMABLE=true|false   enable Streamable HTTP (POST /mcp/)

All db access is serialised via a single asyncio.Lock.
"""

import argparse
import asyncio
import copy
import json
import logging
import os
import random
import string
import sys

import fastapi
import uvicorn
import urllib.parse
from fastapi.responses import Response
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import contextlib
import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gdata
import gdata_oauth

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ---------------------------------------------------------------------------
# DB singleton + lock
# ---------------------------------------------------------------------------

_db = None
_db_lock = asyncio.Lock()


def _open_db(path: str):
    global _db
    if _db is None:
        _db = gdata.gdata_local_simple(gdbm_file=path)
    return _db


# ---------------------------------------------------------------------------
# Sidecar helpers (mirrors gdata_server.py — keep in sync)
# ---------------------------------------------------------------------------

_SIDECAR_PREFIX = '\x00'


def _sidecar_key(key: str) -> str:
    return _SIDECAR_PREFIX + key


def _new_block_id() -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _increment_rev(rev: str) -> str:
    return f'r{int(rev[1:]) + 1}'


def _get_sidecar(db, key: str) -> dict | None:
    sk = _sidecar_key(key)
    if sk not in db:
        return None
    try:
        return json.loads(db[sk])
    except (json.JSONDecodeError, KeyError):
        return None


def _save_sidecar(db, key: str, sidecar: dict):
    db[_sidecar_key(key)] = json.dumps(sidecar)


def _get_or_create_sidecar(db, key: str, content: list) -> dict:
    sidecar = _get_sidecar(db, key)
    if sidecar is None:
        sidecar = {'rev': 'r1', 'block_ids': [_new_block_id() for _ in content]}
        _save_sidecar(db, key, sidecar)
    elif len(sidecar.get('block_ids', [])) != len(content):
        sidecar['block_ids'] = [_new_block_id() for _ in content]
        _save_sidecar(db, key, sidecar)
    return sidecar


def _apply_op(op_body: dict, doc: dict, block_ids: list) -> dict:
    """Apply one patch op to doc and block_ids in-place (no DB access)."""
    op = op_body.get('op')

    if op == 'patch_meta':
        fields = op_body.get('fields')
        if not isinstance(fields, dict):
            raise fastapi.HTTPException(status_code=400, detail="'fields' must be an object")
        if 'content' in fields:
            raise fastapi.HTTPException(status_code=400, detail="'content' cannot be updated via patch_meta")
        doc.update(fields)
        return {'status': 'ok'}

    content = doc.get('content')
    if content is None:
        raise fastapi.HTTPException(status_code=409, detail="document has no 'content' key")
    if not isinstance(content, list):
        raise fastapi.HTTPException(
            status_code=400,
            detail="'content' is not a list; cannot patch block-level (PUT the full document first)"
        )

    block_id = op_body.get('block_id')

    if op == 'insert_before':
        if block_id is None:
            raise fastapi.HTTPException(status_code=400, detail="'block_id' is required for insert_before")
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for insert_before")
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        new_id = _new_block_id()
        content.insert(idx, block)
        block_ids.insert(idx, new_id)
        return {'status': 'ok', 'inserted_block_id': new_id}

    if op == 'insert_after':
        if block_id is None:
            raise fastapi.HTTPException(status_code=400, detail="'block_id' is required for insert_after")
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for insert_after")
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        new_id = _new_block_id()
        content.insert(idx + 1, block)
        block_ids.insert(idx + 1, new_id)
        return {'status': 'ok', 'inserted_block_id': new_id}

    if op == 'replace_block' and block_id is not None:
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for replace_block")
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        content[idx] = block
        return {'status': 'ok'}

    if op == 'delete_block' and block_id is not None:
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        content.pop(idx)
        block_ids.pop(idx)
        return {'status': 'ok'}

    if op == 'append_block':
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for append_block")
        new_id = _new_block_id()
        content.append(block)
        block_ids.append(new_id)
        return {'status': 'ok', 'inserted_block_id': new_id}

    if op == 'delete_blocks':
        indices = op_body.get('indices')
        if not isinstance(indices, list) or not indices:
            raise fastapi.HTTPException(status_code=400, detail="'indices' must be a non-empty list for delete_blocks")
        if not all(isinstance(i, int) and i >= 0 for i in indices):
            raise fastapi.HTTPException(status_code=400, detail="all indices must be non-negative integers")
        max_index = len(content) - 1
        out_of_range = [i for i in indices if i > max_index]
        if out_of_range:
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"indices out of range: {out_of_range} (content has {len(content)} block(s))"
            )
        for i in sorted(set(indices), reverse=True):
            content.pop(i)
            block_ids.pop(i)
        return {'status': 'ok'}

    if op in ('insert_block', 'replace_block', 'delete_block'):
        index = op_body.get('index')
        if index is None:
            raise fastapi.HTTPException(status_code=400, detail=f"'index' is required for {op}")
        if not isinstance(index, int) or index < 0:
            raise fastapi.HTTPException(status_code=400, detail="'index' must be a non-negative integer")
        max_index = len(content) if op == 'insert_block' else len(content) - 1
        if index > max_index:
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"index {index} out of range (content has {len(content)} block(s))"
            )
        if op == 'delete_block':
            content.pop(index)
            block_ids.pop(index)
            return {'status': 'ok'}
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail=f"'block' is required for {op}")
        if op == 'insert_block':
            new_id = _new_block_id()
            content.insert(index, block)
            block_ids.insert(index, new_id)
            return {'status': 'ok', 'inserted_block_id': new_id}
        else:
            content[index] = block
            return {'status': 'ok'}

    raise fastapi.HTTPException(
        status_code=400,
        detail=(
            f"unknown op: {op!r}. Supported: append_block, insert_block, replace_block, "
            "delete_block, delete_blocks, patch_meta, insert_before, insert_after, "
            "batch, get_with_block_ids"
        )
    )


# ---------------------------------------------------------------------------
# Async DB operations
# ---------------------------------------------------------------------------

async def db_get(db_path: str, key: str):
    async with _db_lock:
        db = _open_db(db_path)
        if key not in db:
            return None, False
        return db[key], True


async def db_put(db_path: str, key: str, value_json: str, if_rev: str | None = None):
    async with _db_lock:
        db = _open_db(db_path)
        old_sidecar = _get_sidecar(db, key)

        if if_rev is not None:
            current_rev = old_sidecar['rev'] if old_sidecar else None
            if current_rev != if_rev:
                raise fastapi.HTTPException(
                    status_code=412,
                    detail=f"revision mismatch: expected {if_rev!r}, current rev is {current_rev!r}"
                )

        db[key] = value_json

        try:
            doc = json.loads(value_json)
            content = doc.get('content', []) if isinstance(doc, dict) else []
            if not isinstance(content, list):
                content = []
        except json.JSONDecodeError:
            content = []

        new_sidecar = {
            'rev': _increment_rev(old_sidecar['rev'] if old_sidecar else 'r0'),
            'block_ids': [_new_block_id() for _ in content],
        }
        _save_sidecar(db, key, new_sidecar)


async def db_delete(db_path: str, key: str) -> bool:
    async with _db_lock:
        db = _open_db(db_path)
        if key not in db:
            return False
        del db[key]
        sk = _sidecar_key(key)
        if sk in db:
            del db[sk]
        return True


async def db_keys(db_path: str) -> list[str]:
    async with _db_lock:
        return sorted(k for k in _open_db(db_path).keys() if not k.startswith(_SIDECAR_PREFIX))


async def db_dump(db_path: str) -> dict:
    async with _db_lock:
        db = _open_db(db_path)
        items = {}
        for k in sorted(k for k in db.keys() if not k.startswith(_SIDECAR_PREFIX)):
            raw = db[k]
            try:
                items[k] = json.loads(raw)
            except json.JSONDecodeError:
                items[k] = raw
        return items


async def db_flush(db_path: str):
    async with _db_lock:
        _open_db(db_path).db.sync()


async def db_patch(db_path: str, key: str, body: dict) -> dict:
    """
    Atomic read-modify-write for block-level patch operations.
    Returns a result dict; raises fastapi.HTTPException on error.
    """
    async with _db_lock:
        db = _open_db(db_path)

        if key not in db:
            raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")

        try:
            doc = json.loads(db[key])
        except json.JSONDecodeError:
            raise fastapi.HTTPException(status_code=400, detail="document is not valid JSON")

        if not isinstance(doc, dict):
            raise fastapi.HTTPException(status_code=400, detail="document is not a JSON object")

        op = body.get('op')

        # --- Read-only op ---
        if op == 'get_with_block_ids':
            content = doc.get('content')
            blocks = content if isinstance(content, list) else []
            sidecar = _get_or_create_sidecar(db, key, blocks)
            return {'document': doc, 'rev': sidecar['rev'], 'block_ids': sidecar['block_ids']}

        # --- Concurrency guard ---
        if_rev = body.get('if_rev')
        if if_rev is not None:
            sidecar = _get_sidecar(db, key)
            current_rev = sidecar['rev'] if sidecar else None
            if current_rev != if_rev:
                raise fastapi.HTTPException(
                    status_code=409,
                    detail=f"revision mismatch: expected {if_rev!r}, current rev is {current_rev!r}"
                )

        # --- Load/create sidecar ---
        content = doc.get('content')
        blocks = content if isinstance(content, list) else []
        sidecar = _get_or_create_sidecar(db, key, blocks)
        block_ids = sidecar['block_ids']

        # --- Batch op ---
        if op == 'batch':
            ops = body.get('ops')
            if isinstance(ops, str):
                try:
                    ops = json.loads(ops)
                except json.JSONDecodeError:
                    raise fastapi.HTTPException(status_code=400, detail="'ops' could not be parsed as JSON")
            if not isinstance(ops, list) or not ops:
                raise fastapi.HTTPException(status_code=400, detail="'ops' must be a non-empty list")

            doc_work = copy.deepcopy(doc)
            block_ids_work = list(block_ids)
            inserted_block_ids = []
            for op_body in ops:
                if not isinstance(op_body, dict):
                    raise fastapi.HTTPException(status_code=400, detail="each op in batch must be a JSON object")
                r = _apply_op(op_body, doc_work, block_ids_work)
                if 'inserted_block_id' in r:
                    inserted_block_ids.append(r['inserted_block_id'])

            new_rev = _increment_rev(sidecar['rev'])
            sidecar['rev'] = new_rev
            sidecar['block_ids'] = block_ids_work
            db[key] = json.dumps(doc_work)
            _save_sidecar(db, key, sidecar)
            return {'status': 'ok', 'rev': new_rev, 'inserted_block_ids': inserted_block_ids}

        # --- Single op ---
        result = _apply_op(body, doc, block_ids)
        new_rev = _increment_rev(sidecar['rev'])
        sidecar['rev'] = new_rev
        sidecar['block_ids'] = block_ids
        db[key] = json.dumps(doc)
        _save_sidecar(db, key, sidecar)
        result['rev'] = new_rev
        return result


# ---------------------------------------------------------------------------
# REST app (FastAPI) — same interface as gdata_server.py
# ---------------------------------------------------------------------------

_SOURCE_MTIME = os.path.getmtime(__file__)


def make_rest_app(db_path: str) -> fastapi.FastAPI:
    app = fastapi.FastAPI()
    app.include_router(gdata_oauth.router)

    @app.middleware("http")
    async def auto_reload(request: fastapi.Request, call_next):
        response = await call_next(request)
        if os.path.getmtime(__file__) != _SOURCE_MTIME:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        return response

    def _key(path: str) -> str:
        return urllib.parse.unquote(path.strip('/'))

    @app.get("/{path:path}")
    async def get_item(path: str):
        key = _key(path)
        raw, found = await db_get(db_path, key)
        if not found:
            raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
        return Response(content=raw, media_type="application/json")

    @app.put("/{path:path}")
    async def put_item(path: str, request: fastapi.Request):
        key = _key(path)
        body = (await request.body()).decode('utf-8')
        await db_put(db_path, key, body)
        return {'status': 'ok'}

    @app.delete("/{path:path}")
    async def delete_item(path: str):
        key = _key(path)
        deleted = await db_delete(db_path, key)
        if not deleted:
            raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
        return {'status': 'deleted'}

    @app.head("/{path:path}")
    async def head_item(path: str):
        key = _key(path)
        _, found = await db_get(db_path, key)
        return Response(status_code=200 if found else 404)

    @app.post("/")
    async def post_op(body: dict):
        op = body.get('op')
        if op == 'keys':
            return {'keys': await db_keys(db_path)}
        if op == 'dump':
            return {'items': await db_dump(db_path)}
        if op == 'flush':
            await db_flush(db_path)
            return {'status': 'flushed'}
        if op == 'stop':
            import signal, threading
            def _shutdown():
                import time; time.sleep(0.1)
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=_shutdown, daemon=True).start()
            return {'status': 'stopping'}
        return {
            'error': f'unknown op: {op!r}',
            'supported_ops': ['keys', 'dump', 'flush', 'stop'],
        }

    @app.post("/{path:path}")
    async def post_patch(path: str, body: dict):
        key = _key(path)
        return await db_patch(db_path, key, body)

    return app


# ---------------------------------------------------------------------------
# MCP app (SSE and/or Streamable HTTP, controlled by env vars)
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, '').lower()
    if val in ('1', 'true', 'yes'):
        return True
    if val in ('0', 'false', 'no'):
        return False
    return default


def _make_tool_server(db_path: str) -> Server:
    """Create and return a Server instance with all gdata tools registered.

    Called once per transport so each transport has its own Server instance
    with independent session state — they must not be shared.
    Both instances operate on the same database via the shared db_* functions.
    """
    server = Server("gdata")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="get",
                description=(
                    "Get a value by key. Returns the parsed JSON value. "
                    "Use include_block_ids=True before a batch edit to obtain stable block IDs and the current rev. "
                    "If unfamiliar with this notes system, call with key='README' first."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "include_block_ids": {
                            "type": "boolean",
                            "description": "If true, returns {document, rev, block_ids} instead of the raw document. Use before batch edits.",
                        },
                    },
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="put",
                description=(
                    "Store a full document under a key. Regenerates all block IDs. "
                    "Use patch() or batch() for targeted edits to individual blocks."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"description": "Any JSON-serialisable value"},
                        "if_rev": {
                            "type": "string",
                            "description": "Optimistic concurrency token. Returns 412 if current rev differs. Optional.",
                        },
                    },
                    "required": ["key", "value"],
                },
            ),
            types.Tool(
                name="delete",
                description="Delete a key.",
                inputSchema={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="keys",
                description="List all keys in the database.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="dump",
                description="Return all key/value pairs.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="patch",
                description=(
                    "Apply a single block-level patch to a JSONHTL note. "
                    "For multi-step edits, use get(include_block_ids=True) then batch() instead — "
                    "index-based ops drift after each mutation. "
                    "ID-based ops (supply block_id) are preferred: insert_before, insert_after, "
                    "replace_block+block_id, delete_block+block_id. "
                    "Index-based ops are fine for simple one-shot changes."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Note key"},
                        "op": {
                            "type": "string",
                            "enum": [
                                "append_block", "insert_block", "replace_block",
                                "delete_block", "delete_blocks", "patch_meta",
                                "insert_before", "insert_after",
                            ],
                            "description": "Operation. ID-based: replace_block+block_id, delete_block+block_id, insert_before, insert_after. Index-based: append_block, insert_block, replace_block+index, delete_block+index, delete_blocks.",
                        },
                        "block_id": {
                            "type": "string",
                            "description": "Stable server-assigned block ID (from get(include_block_ids=True) or batch response). Required for insert_before/insert_after; preferred over index for replace_block/delete_block.",
                        },
                        "block": {
                            "description": "JSONHTL block — required for append_block, insert_block, replace_block, insert_before, insert_after (may be a JSON-encoded string; handler unwraps automatically)",
                        },
                        "index": {
                            "type": "integer",
                            "description": "0-based block index — for insert_block, replace_block, delete_block when not using block_id. Avoid for multi-step edits: drifts after each mutation.",
                        },
                        "indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "List of 0-based block indices — required for delete_blocks.",
                        },
                        "fields": {
                            "description": "Metadata fields to update — required for patch_meta (title, version, updated, tags; not content). May be a JSON-encoded string; handler unwraps automatically.",
                        },
                        "if_rev": {
                            "type": "string",
                            "description": "Optimistic concurrency token from get(include_block_ids=True). Returns 409 if current rev differs. Optional but recommended for coordinated edits.",
                        },
                    },
                    "required": ["key", "op"],
                },
            ),
            types.Tool(
                name="batch",
                description=(
                    "Apply multiple patch operations atomically — all succeed or none are applied. "
                    "Preferred for multi-step edits. "
                    "Workflow: (1) get(key, include_block_ids=True) to obtain block IDs and rev; "
                    "(2) batch(key, ops=[...], if_rev=rev) using block_id in each op to avoid index drift. "
                    "Returns 409 if if_rev doesn't match (concurrent modification) — re-read and retry."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Note key"},
                        "ops": {
                            "description": (
                                "List of patch ops to apply sequentially. "
                                "Each op is a dict with 'op' and relevant params (block_id, block, fields, etc.). "
                                "May be a JSON-encoded string; handler unwraps automatically."
                            ),
                        },
                        "if_rev": {
                            "type": "string",
                            "description": "Optimistic concurrency token from get(include_block_ids=True). Strongly recommended. Returns 409 on mismatch.",
                        },
                    },
                    "required": ["key", "ops"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        try:
            if name == "get":
                key = arguments["key"]
                include_block_ids = bool(arguments.get("include_block_ids", False))
                if include_block_ids:
                    try:
                        result = await db_patch(db_path, key, {"op": "get_with_block_ids"})
                    except fastapi.HTTPException as e:
                        result = {"error": e.detail, "status_code": e.status_code}
                else:
                    raw, found = await db_get(db_path, key)
                    if not found:
                        result = {"error": f"key not found: {key}"}
                    else:
                        try:
                            result = json.loads(raw)
                        except json.JSONDecodeError:
                            result = raw
            elif name == "put":
                key = arguments["key"]
                value = arguments["value"]
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass
                if_rev = arguments.get("if_rev") or None
                try:
                    await db_put(db_path, key, json.dumps(value), if_rev=if_rev)
                    result = {"status": "ok"}
                except fastapi.HTTPException as e:
                    result = {"error": e.detail, "status_code": e.status_code}
            elif name == "delete":
                key = arguments["key"]
                deleted = await db_delete(db_path, key)
                result = {"status": "deleted"} if deleted else {"error": f"key not found: {key}"}
            elif name == "keys":
                result = {"keys": await db_keys(db_path)}
            elif name == "dump":
                result = {"items": await db_dump(db_path)}
            elif name == "patch":
                key = arguments["key"]
                body = {k: arguments[k] for k in
                        ("op", "block_id", "block", "index", "indices", "fields", "if_rev")
                        if k in arguments}
                for field in ("block", "fields"):
                    if isinstance(body.get(field), str):
                        try:
                            body[field] = json.loads(body[field])
                        except json.JSONDecodeError:
                            pass
                try:
                    result = await db_patch(db_path, key, body)
                except fastapi.HTTPException as e:
                    result = {"error": e.detail, "status_code": e.status_code}
            elif name == "batch":
                key = arguments["key"]
                ops = arguments.get("ops", [])
                if isinstance(ops, str):
                    try:
                        ops = json.loads(ops)
                    except json.JSONDecodeError:
                        return [types.TextContent(type="text", text=json.dumps(
                            {"error": "'ops' could not be parsed as JSON"}))]
                if_rev = arguments.get("if_rev") or None
                body = {"op": "batch", "ops": ops}
                if if_rev:
                    body["if_rev"] = if_rev
                try:
                    result = await db_patch(db_path, key, body)
                except fastapi.HTTPException as e:
                    result = {"error": e.detail, "status_code": e.status_code}
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as e:
            result = {"error": str(e)}

        return [types.TextContent(type="text", text=json.dumps(result))]

    return server


def make_mcp_app(db_path: str) -> Starlette:
    enable_sse        = _env_bool('MCP_SSE',        True)
    enable_streamable = _env_bool('MCP_STREAMABLE', True)

    if not enable_sse and not enable_streamable:
        raise RuntimeError("At least one of MCP_SSE or MCP_STREAMABLE must be enabled")

    routes = []

    if enable_sse:
        sse_server = _make_tool_server(db_path)
        sse = SseServerTransport("/mcp/messages/")

        async def handle_sse(request: Request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await sse_server.run(
                    streams[0], streams[1],
                    sse_server.create_initialization_options()
                )
            from starlette.responses import Response as StarletteResponse
            return StarletteResponse()

        routes += [
            Route("/mcp/", endpoint=handle_sse, methods=["GET"]),
            Mount("/mcp/messages", app=sse.handle_post_message),
        ]

    if enable_streamable:
        streamable_server = _make_tool_server(db_path)
        session_manager = StreamableHTTPSessionManager(streamable_server, stateless=True)

        @contextlib.asynccontextmanager
        async def streamable_lifespan(app):
            async with session_manager.run():
                yield

        routes.append(Mount("/mcp", app=session_manager.handle_request))

    transports = []
    if enable_sse:        transports.append("SSE")
    if enable_streamable: transports.append("Streamable HTTP")
    logging.info(f"MCP transports enabled: {', '.join(transports)}")

    if enable_streamable:
        app = Starlette(routes=routes, lifespan=streamable_lifespan)
    else:
        app = Starlette(routes=routes)

    return gdata_oauth.BearerMiddleware(app)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    gdata_oauth.startup_check()
    parser = argparse.ArgumentParser(description="gdata REST + MCP server")
    parser.add_argument("--rest-port", type=int, default=int(os.getenv("GDATA_SERVER_PORT", 8020)))
    parser.add_argument("--mcp-port",  type=int, default=int(os.getenv("GDATA_MCP_PORT",    8022)))
    parser.add_argument("--host",      default=os.getenv("GDATA_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--db",        default=os.getenv("GDBM_PATH", ".gdbm"))
    args = parser.parse_args()

    log_level = os.getenv('LOG_LEVEL', 'info').lower()

    rest_app = make_rest_app(args.db)
    mcp_app  = make_mcp_app(args.db)

    rest_cfg = uvicorn.Config(rest_app, host=args.host, port=args.rest_port, log_level=log_level)
    mcp_cfg  = uvicorn.Config(mcp_app,  host=args.host, port=args.mcp_port,  log_level=log_level)

    logging.info(f"REST on {args.host}:{args.rest_port}  |  MCP on {args.host}:{args.mcp_port}  |  db={args.db}")

    await asyncio.gather(
        uvicorn.Server(rest_cfg).serve(),
        uvicorn.Server(mcp_cfg).serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
