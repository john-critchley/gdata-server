#!/usr/bin/env python3
"""
misc_mcp_server.py — miscellaneous MCP tools server.

Runs two uvicorn servers:
  - REST/OAuth on --rest-port  (OAuth endpoints, no KV store)
  - MCP        on --mcp-port   (SSE and/or Streamable HTTP)

Transports:
  MCP_SSE=true|false
  MCP_STREAMABLE=true|false
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sys

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


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, '').lower()
    if val in ('1', 'true', 'yes'):
        return True
    if val in ('0', 'false', 'no'):
        return False
    return default


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
        ]
        prefix = f'[{store_name} store] '
        for t in tools:
            t.description = prefix + t.description
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        if name == 'hello':
            return [types.TextContent(type='text', text='hello')]
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
    gdata_oauth.startup_check()
    parser = argparse.ArgumentParser(description='misc MCP server')
    parser.add_argument('--rest-port', type=int, default=int(os.getenv('MISC_REST_PORT', 8220)))
    parser.add_argument('--mcp-port',  type=int, default=int(os.getenv('MISC_MCP_PORT',  8223)))
    parser.add_argument('--host',      default=os.getenv('MISC_HOST', '127.0.0.1'))
    parser.add_argument('--name',      default=os.getenv('GDATA_STORE_NAME', 'misc'))
    args = parser.parse_args()

    log_level = os.getenv('LOG_LEVEL', 'info').lower()

    rest_app = make_rest_app(args.rest_port, args.name)
    mcp_app  = make_mcp_app(args.name)

    rest_cfg = uvicorn.Config(rest_app, host=args.host, port=args.rest_port, log_level=log_level)
    mcp_cfg  = uvicorn.Config(mcp_app,  host=args.host, port=args.mcp_port,  log_level=log_level)

    logger.info(f'REST on {args.host}:{args.rest_port}  |  MCP on {args.host}:{args.mcp_port}  |  name={args.name}')

    await asyncio.gather(
        uvicorn.Server(rest_cfg).serve(),
        uvicorn.Server(mcp_cfg).serve(),
    )


if __name__ == '__main__':
    asyncio.run(main())
