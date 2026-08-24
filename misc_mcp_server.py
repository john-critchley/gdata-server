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
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from email.parser import BytesParser
from email import policy

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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'envoy'))

import gdata_oauth

# Import IMAP client from envoy
try:
    from imap_client import IMAPClient
except ImportError:
    logger = logging.getLogger(__name__)
    logger.warning("Could not import IMAPClient from envoy - email tools will not be available")

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


async def _email_send(to: str, subject: str, body: str, headers: dict = None) -> str:
    """Send an email to the local mail server (Brevo SMTP)."""
    try:
        msg = EmailMessage()
        msg['From'] = 'envoy@critchley.biz'
        msg['To'] = to
        msg['Subject'] = subject
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid(domain='critchley.biz')

        # Add custom headers if provided
        if headers and isinstance(headers, dict):
            for key, value in headers.items():
                if key not in ('From', 'To', 'Subject', 'Date', 'Message-ID'):
                    msg[key] = str(value)

        msg.set_content(body)

        # Send via SMTP
        with smtplib.SMTP('smtp-relay.brevo.com', 587) as server:
            server.starttls()
            server.login('7d832001@smtp-brevo.com', os.getenv('BREVO_SMTP_PASSWORD', ''))
            server.send_message(msg)

        return json.dumps({
            'status': 'sent',
            'to': to,
            'subject': subject,
            'message_id': msg['Message-ID']
        })
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return json.dumps({'status': 'error', 'error': str(e)})


async def _email_list(folder: str = 'INBOX', search_criteria: str = 'ALL', max_results: int = 10) -> str:
    """List emails in a mailbox folder."""
    try:
        with IMAPClient('mail.critchley.biz') as client:
            client.select_folder(folder)
            email_ids = client.search(search_criteria)

            emails = []
            for seq_id in email_ids[-max_results:]:
                try:
                    status, data = client.connection.fetch(seq_id, '(FLAGS RFC822.HEADER)')
                    if status == 'OK' and data:
                        flags_str = data[0][0].decode('utf-8', errors='replace')
                        header_bytes = data[0][1]

                        # Parse headers
                        parser = BytesParser(policy=policy.default)
                        msg = parser.parsebytes(header_bytes)

                        emails.append({
                            'seq': str(seq_id),
                            'from': msg.get('From', ''),
                            'to': msg.get('To', ''),
                            'subject': msg.get('Subject', ''),
                            'date': msg.get('Date', ''),
                            'message_id': msg.get('Message-ID', ''),
                            'flags': flags_str
                        })
                except Exception as e:
                    logger.warning(f"Error parsing email {seq_id}: {e}")

            return json.dumps({
                'folder': folder,
                'count': len(emails),
                'emails': emails
            })
    except Exception as e:
        logger.error(f"Failed to list emails: {e}")
        return json.dumps({'status': 'error', 'error': str(e)})


async def _email_read(folder: str = 'INBOX', seq_id: str = None) -> str:
    """Read full email including body from mailbox."""
    try:
        with IMAPClient('mail.critchley.biz') as client:
            client.select_folder(folder)

            if not seq_id:
                return json.dumps({'status': 'error', 'error': 'seq_id required'})

            status, data = client.connection.fetch(seq_id, '(FLAGS RFC822)')
            if status != 'OK' or not data:
                return json.dumps({'status': 'error', 'error': f'Email {seq_id} not found'})

            flags_str = data[0][0].decode('utf-8', errors='replace')
            email_bytes = data[0][1]

            # Parse full email
            parser = BytesParser(policy=policy.default)
            msg = parser.parsebytes(email_bytes)

            # Extract headers as dict
            headers = {key: msg[key] for key in msg.keys()}

            # Extract body
            body = ''
            if msg.is_multipart():
                for part in msg.iter_parts():
                    if part.get_content_type() == 'text/plain':
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode('utf-8', errors='replace')
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='replace')
                else:
                    body = msg.get_payload()

            return json.dumps({
                'seq': seq_id,
                'folder': folder,
                'headers': headers,
                'body': body,
                'flags': flags_str
            })
    except Exception as e:
        logger.error(f"Failed to read email: {e}")
        return json.dumps({'status': 'error', 'error': str(e)})


async def _email_list_folders() -> str:
    """List all mailbox folders."""
    try:
        with IMAPClient('mail.critchley.biz') as client:
            folders = client.list_folders()
            return json.dumps({
                'status': 'success',
                'folders': folders,
                'count': len(folders)
            })
    except Exception as e:
        logger.error(f"Failed to list folders: {e}")
        return json.dumps({'status': 'error', 'error': str(e)})


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
            types.Tool(
                name='email_send',
                description='Send an email to the local mail server. For full documentation and testing workflows, see notes key: misc-server/email-tools',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'to': {
                            'type': 'string',
                            'description': 'Email recipient address',
                        },
                        'subject': {
                            'type': 'string',
                            'description': 'Email subject line',
                        },
                        'body': {
                            'type': 'string',
                            'description': 'Email body text',
                        },
                        'headers': {
                            'type': 'object',
                            'description': 'Optional additional email headers (dict). Do not include From, To, Subject, Date, Message-ID.',
                        },
                    },
                    'required': ['to', 'subject', 'body'],
                },
            ),
            types.Tool(
                name='email_list',
                description='List emails in a mailbox folder with headers. See misc-server/email-tools for examples and IMAP search syntax.',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'folder': {
                            'type': 'string',
                            'description': 'Mailbox folder name (default: INBOX)',
                        },
                        'search_criteria': {
                            'type': 'string',
                            'description': 'IMAP search criteria (default: ALL). E.g., UNSEEN, SEEN, FROM "address", SUBJECT "text"',
                        },
                        'max_results': {
                            'type': 'integer',
                            'description': 'Maximum number of results to return (default: 10)',
                        },
                    },
                    'required': [],
                },
            ),
            types.Tool(
                name='email_read',
                description='Read a full email including body and all headers from mailbox. See notes key misc-server/email-tools for workflow examples.',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'folder': {
                            'type': 'string',
                            'description': 'Mailbox folder name (default: INBOX)',
                        },
                        'seq_id': {
                            'type': 'string',
                            'description': 'Email sequence ID (returned from email_list)',
                        },
                    },
                    'required': ['seq_id'],
                },
            ),
            types.Tool(
                name='email_list_folders',
                description='List all mailbox folders available on the server. Documentation: misc-server/email-tools',
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
        if name == 'pg_query':
            sql = (arguments.get('sql') or '').strip()
            if not sql:
                return [types.TextContent(type='text', text='sql argument is required.')]
            result = await _pg_query(sql)
            return [types.TextContent(type='text', text=result)]
        if name == 'email_send':
            to = arguments.get('to', '').strip()
            subject = arguments.get('subject', '').strip()
            body = arguments.get('body', '').strip()
            headers = arguments.get('headers')
            if not to or not subject:
                return [types.TextContent(type='text', text='to and subject arguments are required.')]
            result = await _email_send(to, subject, body, headers)
            return [types.TextContent(type='text', text=result)]
        if name == 'email_list':
            folder = arguments.get('folder', 'INBOX').strip()
            search_criteria = arguments.get('search_criteria', 'ALL').strip()
            max_results = int(arguments.get('max_results', 10))
            result = await _email_list(folder, search_criteria, max_results)
            return [types.TextContent(type='text', text=result)]
        if name == 'email_read':
            folder = arguments.get('folder', 'INBOX').strip()
            seq_id = arguments.get('seq_id', '').strip()
            if not seq_id:
                return [types.TextContent(type='text', text='seq_id argument is required.')]
            result = await _email_read(folder, seq_id)
            return [types.TextContent(type='text', text=result)]
        if name == 'email_list_folders':
            result = await _email_list_folders()
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
