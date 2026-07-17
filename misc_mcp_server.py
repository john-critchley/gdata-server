#!/usr/bin/env python3
"""
misc_mcp_server.py — miscellaneous MCP tools server.

Runs two uvicorn servers:
  - REST/OAuth on --rest-port  (OAuth endpoints, no KV store)
  - MCP        on --mcp-port   (SSE and/or Streamable HTTP)

Transports:
  MCP_SSE=true|false
  MCP_STREAMABLE=true|false

Notes (public store):
  misc-server            — server architecture, ports, OAuth
  misc-server/tools      — tool catalogue with usage links
  location-db            — PostgreSQL/PostGIS location DB hub
  location-db/usage      — pg_query examples and column reference
  location-db/schema     — table DDL and index descriptions
  location-db/architecture — asyncpg pool, dual-write, retry pattern
  mcp-conventions        — general principle: include notes key in tool descriptions
"""

import argparse
import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
import sys

import asyncpg
import fastapi
import uvicorn
import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gdata_oauth

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

PG_DSN = 'postgresql://owntracks_ro@/owntracks'

# Module-level pool shared across all tool handlers.
_pool: asyncpg.Pool | None = None

_SELECT_RE = re.compile(r'^\s*(?:--[^\n]*\n|\s)*select\b', re.IGNORECASE)


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, '').lower()
    if val in ('1', 'true', 'yes'):
        return True
    if val in ('0', 'false', 'no'):
        return False
    return default


def _serialise_value(v):
    """Convert asyncpg value types to JSON-serialisable forms."""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, (list, tuple)):
        return [_serialise_value(i) for i in v]
    return v


def _row_to_dict(record: asyncpg.Record) -> dict:
    return {k: _serialise_value(v) for k, v in dict(record).items()}


async def _pg_query(sql: str) -> str:
    """Run a SELECT and return JSON string, or an error message."""
    if not _SELECT_RE.match(sql):
        return 'Only SELECT statements are permitted.'

    if _pool is None:
        return 'Database pool not available. Please try again.'

    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(sql, timeout=30)
        result = [_row_to_dict(r) for r in rows]
        if len(result) == 500:
            note = f'\n[Result capped at 500 rows. Add LIMIT to your query to be explicit.]'
        else:
            note = ''
        return json.dumps(result, default=str) + note
    except Exception as exc:
        logger.warning('pg_query error: %s', exc)
        return f'Query error: {exc}. Please try again.'


def make_rest_app(rest_port: int = 8220, store_name: str = 'misc') -> fastapi.FastAPI:
    app = fastapi.FastAPI(redirect_slashes=False)
    app.include_router(gdata_oauth.router)

    @app.middleware('http')
    async def add_store_header(request: fastapi.Request, call_next):
        response = await call_next(request)
        response.headers['X-MCP-Store'] = store_name
        return response

    return app


def _make_tool_server(store_name: str = 'misc') -> Server:
    server = Server('misc')

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = [
            types.Tool(
                name='hello',
                description='Say hello. Returns a greeting.',
                inputSchema={'type': 'object', 'properties': {}, 'required': []},
            ),
            types.Tool(
                name='pg_query',
                description=(
                    'Execute a read-only SELECT query against the OwnTracks location database.\n\n'
                    'Database: owntracks\n'
                    'Table: locations\n'
                    'Columns: id, received_at (timestamptz), tst (unix epoch bigint), '
                    'lat (float), lon (float), geom (PostGIS geometry Point/4326), '
                    'acc (horiz accuracy m), vac (vert accuracy m), alt (altitude m), '
                    'batt (battery %), bs (battery status: 1=unplugged 2=charging 3=full), '
                    'conn (w=WiFi m=mobile o=offline), ssid, bssid, '
                    't (trigger: p=ping t=timer u=user c=circular), '
                    'm (monitoring mode), p (pressure hPa), tid, '
                    'motionactivities (text[]), inregions (text[]), topic\n\n'
                    'Spatial: geom is indexed with GiST. Use ::geography for metre-accurate distance.\n'
                    'Use ST_AsGeoJSON(geom) to return geometry as GeoJSON.\n'
                    'Use ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(lon,lat),4326)::geography, metres) for radius search.\n'
                    'Use ST_Distance(geom::geography, ...) for distance in metres.\n'
                    'Use to_timestamp(tst) AT TIME ZONE \'Europe/London\' for readable local times.\n\n'
                    'Only SELECT is permitted. Returns up to 500 rows.\n\n'
                    'Usage notes: location-db/usage (public notes store)'
                ),
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'sql': {
                            'type': 'string',
                            'description': 'A SELECT SQL statement. PostGIS spatial functions are available.',
                        },
                    },
                    'required': ['sql'],
                },
            ),
        ]
        prefix = f'[{store_name} store] '
        for t in tools:
            t.description = prefix + t.description
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        if name == 'hello':
            return [types.TextContent(type='text', text='hello')]
        if name == 'pg_query':
            sql = (arguments.get('sql') or '').strip()
            if not sql:
                return [types.TextContent(type='text', text='sql argument is required.')]
            result = await _pg_query(sql)
            return [types.TextContent(type='text', text=result)]
        return [types.TextContent(type='text', text=f'unknown tool: {name}')]

    return server


def make_mcp_app(store_name: str = 'misc') -> Starlette:
    enable_sse        = _env_bool('MCP_SSE',        True)
    enable_streamable = _env_bool('MCP_STREAMABLE', True)

    if not enable_sse and not enable_streamable:
        raise RuntimeError('At least one of MCP_SSE or MCP_STREAMABLE must be enabled')

    routes = []

    if enable_sse:
        sse_server = _make_tool_server(store_name)
        sse = SseServerTransport('/mcp/messages/')

        async def handle_sse(request: Request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await sse_server.run(streams[0], streams[1], sse_server.create_initialization_options())
            from starlette.responses import Response as SR
            return SR()

        routes += [
            Route('/mcp/', endpoint=handle_sse, methods=['GET']),
            Mount('/mcp/messages', app=sse.handle_post_message),
        ]

    if enable_streamable:
        streamable_server = _make_tool_server(store_name)
        session_manager = StreamableHTTPSessionManager(streamable_server, stateless=True)

        @contextlib.asynccontextmanager
        async def streamable_lifespan(app):
            async with session_manager.run():
                yield

        routes.append(Mount('/mcp', app=session_manager.handle_request))

    if enable_streamable:
        app = Starlette(routes=routes, lifespan=streamable_lifespan)
    else:
        app = Starlette(routes=routes)

    return gdata_oauth.BearerMiddleware(app)


async def main():
    global _pool

    gdata_oauth.startup_check()
    parser = argparse.ArgumentParser(description='misc MCP server')
    parser.add_argument('--rest-port', type=int, default=int(os.getenv('MISC_REST_PORT', 8220)))
    parser.add_argument('--mcp-port',  type=int, default=int(os.getenv('MISC_MCP_PORT',  8223)))
    parser.add_argument('--host',      default=os.getenv('MISC_HOST', '127.0.0.1'))
    parser.add_argument('--name',      default=os.getenv('GDATA_STORE_NAME', 'misc'))
    args = parser.parse_args()

    log_level = os.getenv('LOG_LEVEL', 'info').lower()

    logger.info('Connecting to PostgreSQL pool...')
    _pool = await asyncpg.create_pool(
        PG_DSN,
        min_size=1,
        max_size=3,
        max_inactive_connection_lifetime=300,
        max_queries=50_000,
        command_timeout=30,
    )
    logger.info('PostgreSQL pool ready.')

    rest_app = make_rest_app(args.rest_port, args.name)
    mcp_app  = make_mcp_app(args.name)

    rest_cfg = uvicorn.Config(rest_app, host=args.host, port=args.rest_port, log_level=log_level)
    mcp_cfg  = uvicorn.Config(mcp_app,  host=args.host, port=args.mcp_port,  log_level=log_level)

    logger.info(f'REST on {args.host}:{args.rest_port}  |  MCP on {args.host}:{args.mcp_port}  |  name={args.name}')

    try:
        await asyncio.gather(
            uvicorn.Server(rest_cfg).serve(),
            uvicorn.Server(mcp_cfg).serve(),
        )
    finally:
        await _pool.close()
        logger.info('PostgreSQL pool closed.')


if __name__ == '__main__':
    asyncio.run(main())
