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
import json
import logging
import os
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


async def db_get(db_path: str, key: str):
    async with _db_lock:
        db = _open_db(db_path)
        if key not in db:
            return None, False
        return db[key], True


async def db_put(db_path: str, key: str, value_json: str):
    async with _db_lock:
        _open_db(db_path)[key] = value_json


async def db_delete(db_path: str, key: str) -> bool:
    async with _db_lock:
        db = _open_db(db_path)
        if key not in db:
            return False
        del db[key]
        return True


async def db_keys(db_path: str) -> list[str]:
    async with _db_lock:
        return sorted(list(_open_db(db_path).keys()))


async def db_dump(db_path: str) -> dict:
    async with _db_lock:
        db = _open_db(db_path)
        items = {}
        for k in sorted(db.keys()):
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

        if op == 'patch_meta':
            fields = body.get('fields')
            if not isinstance(fields, dict):
                raise fastapi.HTTPException(status_code=400, detail="'fields' must be an object")
            if 'content' in fields:
                raise fastapi.HTTPException(status_code=400, detail="'content' cannot be updated via patch_meta")
            doc.update(fields)
            db[key] = json.dumps(doc)
            return {'status': 'ok'}

        content = doc.get('content')
        if content is None:
            raise fastapi.HTTPException(status_code=409, detail="document has no 'content' key")
        if not isinstance(content, list):
            raise fastapi.HTTPException(
                status_code=400,
                detail="'content' is not a list; cannot patch block-level (PUT the full document first)"
            )

        if op == 'append_block':
            block = body.get('block')
            if block is None:
                raise fastapi.HTTPException(status_code=400, detail="'block' is required for append_block")
            content.append(block)
            db[key] = json.dumps(doc)
            return {'status': 'ok'}

        if op in ('insert_block', 'replace_block', 'delete_block'):
            index = body.get('index')
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
                db[key] = json.dumps(doc)
                return {'status': 'ok'}
            block = body.get('block')
            if block is None:
                raise fastapi.HTTPException(status_code=400, detail=f"'block' is required for {op}")
            if op == 'insert_block':
                content.insert(index, block)
            else:
                content[index] = block
            db[key] = json.dumps(doc)
            return {'status': 'ok'}

        raise fastapi.HTTPException(
            status_code=400,
            detail=f"unknown op: {op!r}. Supported: append_block, insert_block, replace_block, delete_block, patch_meta"
        )


# ---------------------------------------------------------------------------
# REST app (FastAPI) — same interface as gdata_server.py
# ---------------------------------------------------------------------------

def make_rest_app(db_path: str) -> fastapi.FastAPI:
    app = fastapi.FastAPI()
    app.include_router(gdata_oauth.router)

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
                description="Get a value by key. Returns the parsed JSON value.",
                inputSchema={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="put",
                description="Store a value under a key.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"description": "Any JSON-serialisable value"},
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
                    "Apply a block-level patch operation to a JSONHTL note. "
                    "Prefer this over put() for targeted edits to avoid rewriting the full document. "
                    "ops: append_block, insert_block, replace_block, delete_block, patch_meta."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Note key"},
                        "op": {
                            "type": "string",
                            "enum": ["append_block", "insert_block", "replace_block", "delete_block", "patch_meta"],
                            "description": "Operation to perform",
                        },
                        "block": {
                            "type": "object",
                            "description": "JSONHTL block — required for append_block, insert_block, replace_block",
                        },
                        "index": {
                            "type": "integer",
                            "description": "0-based block index — required for insert_block, replace_block, delete_block",
                        },
                        "fields": {
                            "type": "object",
                            "description": "Metadata fields to update — required for patch_meta (title, version, updated, tags; not content)",
                        },
                    },
                    "required": ["key", "op"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        try:
            if name == "get":
                key = arguments["key"]
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
                # claude.ai MCP client serialises object values to strings; unwrap if needed
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass
                await db_put(db_path, key, json.dumps(value))
                result = {"status": "ok"}
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
                body = {k: arguments[k] for k in ("op", "block", "index", "fields") if k in arguments}
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
