#!/usr/bin/python
"""
gdata_mcp_server.py — combined REST + MCP server over gdbm.

Runs two uvicorn servers in one process (shared db, no locking conflicts):
  - REST API on --rest-port (default 8020)  — same interface as gdata_server.py
  - MCP  SSE on --mcp-port  (default 8022)

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

import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport

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

    return app


# ---------------------------------------------------------------------------
# MCP app (SSE)
# ---------------------------------------------------------------------------

def make_mcp_app(db_path: str) -> Starlette:
    mcp_server = Server("gdata")
    sse = SseServerTransport("/mcp/messages")  # where clients POST replies

    @mcp_server.list_tools()
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
        ]

    @mcp_server.call_tool()
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
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as e:
            result = {"error": str(e)}

        return [types.TextContent(type="text", text=json.dumps(result))]

    async def handle_sse(request: Request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await mcp_server.run(
                streams[0], streams[1],
                mcp_server.create_initialization_options()
            )

    app = Starlette(routes=[
        Route("/mcp/", endpoint=handle_sse),
        Mount("/mcp/messages", app=sse.handle_post_message),
    ])
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

    logging.info(f"REST on {args.host}:{args.rest_port}  |  MCP SSE on {args.host}:{args.mcp_port}  |  db={args.db}")

    await asyncio.gather(
        uvicorn.Server(rest_cfg).serve(),
        uvicorn.Server(mcp_cfg).serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
