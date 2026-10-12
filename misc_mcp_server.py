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
import re
import os
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

import gdata_oauth
import misc_webdav
from mcp_tool_plugins import discover_tools

# Import IMAP client (local copy in gdata-server)
try:
    from imap_client import IMAPClient
except ImportError as e:
    IMAPClient = None
    logger = logging.getLogger(__name__)
    logger.error(f"Could not import IMAPClient: {e}")

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

PG_LOGINS = ('owntracks_ro', 'owntracks_rw', 'owntracks_adm')

# Separate connections keep each request under its selected database identity.
_pools: dict[str, asyncpg.Pool] = {}


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


def _email_send_sync(to: str, subject: str, body: str, headers: dict = None) -> str:
    """Send an email via WebDAV delivery."""
    try:
        import io
        import netrc
        import time
        import zlib
        import webdav4.client

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
        raw_msg = msg.as_bytes()

        # Connect to WebDAV and deliver
        user, _, password = netrc.netrc().authenticators('webdav.critchley.biz')
        client = webdav4.client.Client('https://webdav.critchley.biz/mail/envoy',
                                       auth=(user, password))

        # Generate filename: E-<timestamp_us>-<crc>.eml
        timestamp_us = int(time.time() * 1_000_000)
        extra = f"{subject}{to}".encode('utf-8')
        crc = zlib.crc32(extra) & 0xffffffff
        filename = f"E-{timestamp_us}-{crc:08x}.eml"

        # Upload to new/ directory
        client.upload_fileobj(io.BytesIO(raw_msg), f'new/{filename}')

        return json.dumps({
            'status': 'sent',
            'to': to,
            'subject': subject,
            'message_id': msg['Message-ID']
        })
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return json.dumps({'status': 'error', 'error': str(e)})


def _email_list_sync(folder: str = 'INBOX', search_criteria: str = 'ALL', max_results: int = 10) -> str:
    """List emails in a mailbox folder."""
    try:
        with IMAPClient('mail.critchley.biz') as client:
            client.select_folder(folder, readonly=True)
            email_ids = client.search(search_criteria)

            emails = []
            for seq_id in email_ids[-max_results:]:
                try:
                    status, data = client.connection.fetch(seq_id, '(UID FLAGS BODY.PEEK[HEADER])')
                    if status == 'OK' and data:
                        flags_str = data[0][0].decode('utf-8', errors='replace')
                        header_bytes = data[0][1]

                        # Parse headers
                        parser = BytesParser(policy=policy.default)
                        msg = parser.parsebytes(header_bytes)

                        emails.append({
                            'seq': seq_id.decode('ascii') if isinstance(seq_id, bytes) else str(seq_id),
                            'uid': (re.search(r'\bUID (\d+)', flags_str).group(1) if re.search(r'\bUID (\d+)', flags_str) else None),
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


def _email_read_sync(folder: str = 'INBOX', seq_id: str = None, uid: str = None) -> str:
    """Read full email including body from mailbox."""
    try:
        with IMAPClient('mail.critchley.biz') as client:
            client.select_folder(folder, readonly=True)

            if not seq_id and not uid:
                return json.dumps({'status': 'error', 'error': 'uid or seq_id required'})

            if uid:
                status, data = client.connection.uid('FETCH', uid, '(FLAGS BODY.PEEK[])')
            else:
                status, data = client.connection.fetch(seq_id, '(FLAGS BODY.PEEK[])')
            if status != 'OK' or not data:
                return json.dumps({'status': 'error', 'error': f'Email {seq_id} not found'})

            flags_str = data[0][0].decode('utf-8', errors='replace')
            email_bytes = data[0][1]

            # Parse full email
            parser = BytesParser(policy=policy.default)
            msg = parser.parsebytes(email_bytes)

            # Extract headers as dict
            headers = {key: msg[key] for key in msg.keys()}

            # Walk nested MIME parts and honour the declared charset.
            body = ''
            for part in msg.walk():
                if part.get_content_type() == 'text/plain' and part.get_content_disposition() != 'attachment':
                    body = part.get_content()
                    break
            if not body and not msg.is_multipart():
                body = msg.get_content()

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


def _email_list_folders_sync() -> str:
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


async def _email_send(to, subject, body, headers=None):
    return await asyncio.to_thread(_email_send_sync, to, subject, body, headers)

async def _email_list(folder='INBOX', search_criteria='ALL', max_results=10):
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 500:
        return json.dumps({'status': 'error', 'error': 'max_results must be between 1 and 500'})
    return await asyncio.to_thread(_email_list_sync, folder, search_criteria, max_results)

async def _email_read(folder='INBOX', seq_id=None, uid=None):
    return await asyncio.to_thread(_email_read_sync, folder, seq_id, uid)

async def _email_list_folders():
    return await asyncio.to_thread(_email_list_folders_sync)


async def _pg_query(sql: str, login: str = 'owntracks_ro') -> str:
    """Run SQL under the selected login; PostgreSQL enforces permissions."""
    if login not in PG_LOGINS:
        return f'Unknown database login. Choose one of: {", ".join(PG_LOGINS)}.'

    pool = _pools.get(login)
    if pool is None:
        return 'Database pool not available. Please try again.'

    try:
        async with pool.acquire() as conn:
            # Inspect before executing: falling back after execution could repeat a write.
            statement = await conn.prepare(sql, timeout=30)
            if not statement.get_attributes():
                status = await conn.execute(sql, timeout=30)
                return json.dumps({'status': status})
            rows = await statement.fetch(timeout=30)
        result = [_row_to_dict(r) for r in rows]
        return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception('pg_query error (login=%s)', login)
        return f'Query error: {exc}. Please try again.'


# ---------------------------------------------------------------------------
# Monitoring / health checks (the `check` tool)
#
# Two kinds of check behind one interface:
#   - SQL checks reuse the asyncpg pool (everything reachable as owntracks_ro).
#   - Host checks (disk) use os.statvfs — the metrics SQL cannot see, notably
#     filesystem free space, which is what actually triggered the low-disk
#     incident. SQL cannot even reveal the data_directory to a non-superuser,
#     which is exactly why disk monitoring lives out here.
#
# Each check returns a dict with status: ok | warn | crit | info.
# check('all') rolls them up with an overall_status so a cron can alert on
# status != ok using the identical code path.
#
# Notes (public store): location-db/monitoring
# ---------------------------------------------------------------------------

# Filesystem paths to watch. Postgres data dir is /mnt/postgres/15/main, so the
# DB lives on /mnt; / is the OS/root volume. Override with MONITOR_DISK_PATHS
# (comma-separated).
_DISK_PATHS = [p for p in os.getenv('MONITOR_DISK_PATHS', '/,/mnt').split(',') if p]
_DISK_WARN_PCT = float(os.getenv('MONITOR_DISK_WARN_PCT', '20'))
_DISK_CRIT_PCT = float(os.getenv('MONITOR_DISK_CRIT_PCT', '10'))

_STATUS_RANK = {'ok': 0, 'info': 0, 'warn': 1, 'crit': 2}


def _worst(statuses) -> str:
    return max(statuses, key=lambda s: _STATUS_RANK.get(s, 0), default='ok')


async def _fetchrows(sql: str) -> list[dict]:
    """Run a SELECT via the pool and return rows as dicts (raises on error)."""
    pool = _pools.get('owntracks_ro')
    if pool is None:
        raise RuntimeError('Database pool not available.')
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, timeout=30)
    return [_row_to_dict(r) for r in rows]


async def _check_disk() -> dict:
    filesystems = []
    statuses = []
    for path in _DISK_PATHS:
        try:
            st = os.statvfs(path)
        except OSError as exc:
            filesystems.append({'path': path, 'error': str(exc)})
            statuses.append('warn')
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        free_pct = round(100.0 * free / total, 1) if total else 0.0
        if free_pct < _DISK_CRIT_PCT:
            status = 'crit'
        elif free_pct < _DISK_WARN_PCT:
            status = 'warn'
        else:
            status = 'ok'
        statuses.append(status)
        filesystems.append({
            'path': path,
            'total_gb': round(total / 1e9, 2),
            'used_gb': round((total - free) / 1e9, 2),
            'free_gb': round(free / 1e9, 2),
            'free_pct': free_pct,
            'status': status,
        })
    return {
        'status': _worst(statuses),
        'thresholds_pct': {'warn': _DISK_WARN_PCT, 'crit': _DISK_CRIT_PCT},
        'filesystems': filesystems,
    }


async def _check_db_size() -> dict:
    rows = await _fetchrows(
        "SELECT datname AS database, pg_database_size(datname) AS bytes, "
        "pg_size_pretty(pg_database_size(datname)) AS size "
        "FROM pg_database WHERE datistemplate=false ORDER BY 2 DESC")
    return {'status': 'info', 'databases': rows}


async def _check_connections() -> dict:
    row = (await _fetchrows(
        "SELECT current_setting('max_connections')::int AS max_connections, "
        "(SELECT count(*) FROM pg_stat_activity) AS current, "
        "round(100.0*(SELECT count(*) FROM pg_stat_activity)"
        "/current_setting('max_connections')::int,1) AS pct"))[0]
    pct = float(row['pct'])
    status = 'crit' if pct >= 90 else 'warn' if pct >= 80 else 'ok'
    return {'status': status, **row}


async def _check_long_queries() -> dict:
    rows = await _fetchrows(
        "SELECT pid, usename, state, "
        "extract(epoch FROM now()-query_start)::int AS running_secs, "
        "left(regexp_replace(query,'\\s+',' ','g'),120) AS query "
        "FROM pg_stat_activity "
        "WHERE state='active' AND now()-query_start > interval '30 seconds' "
        "AND pid<>pg_backend_pid() ORDER BY 4 DESC")
    if not rows:
        return {'status': 'ok', 'long_running': []}
    worst = max(r['running_secs'] for r in rows)
    status = 'crit' if worst >= 300 else 'warn'
    return {'status': status, 'long_running': rows}


async def _check_bloat() -> dict:
    rows = await _fetchrows(
        "SELECT relname AS \"table\", n_live_tup AS live, n_dead_tup AS dead, "
        "round(100.0*n_dead_tup/nullif(n_live_tup+n_dead_tup,0),2) AS dead_pct, "
        "last_autovacuum "
        "FROM pg_stat_user_tables WHERE n_live_tup+n_dead_tup>0 "
        "ORDER BY n_dead_tup DESC LIMIT 10")
    worst = max((float(r['dead_pct'] or 0) for r in rows), default=0.0)
    status = 'crit' if worst >= 40 else 'warn' if worst >= 20 else 'ok'
    return {'status': status, 'tables': rows}


async def _check_xid() -> dict:
    rows = await _fetchrows(
        "SELECT datname, age(datfrozenxid) AS xid_age, "
        "round(100.0*age(datfrozenxid)/2000000000,4) AS pct_to_wraparound "
        "FROM pg_database WHERE datistemplate=false ORDER BY 2 DESC")
    worst = max((float(r['pct_to_wraparound']) for r in rows), default=0.0)
    status = 'crit' if worst >= 80 else 'warn' if worst >= 50 else 'ok'
    return {'status': status, 'databases': rows}


async def _check_locks() -> dict:
    row = (await _fetchrows(
        "SELECT (SELECT count(*) FROM pg_locks WHERE NOT granted) AS waiting_locks, "
        "(SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock') "
        "AS sessions_waiting"))[0]
    status = 'warn' if (row['waiting_locks'] or 0) > 0 else 'ok'
    return {'status': status, **row}


async def _check_cache() -> dict:
    row = (await _fetchrows(
        "SELECT round(100.0*blks_hit/nullif(blks_hit+blks_read,0),2) AS cache_hit_pct, "
        "deadlocks, temp_files, pg_size_pretty(temp_bytes) AS temp_written "
        "FROM pg_stat_database WHERE datname=current_database()"))[0]
    hit = float(row['cache_hit_pct'] or 100)
    status = 'warn' if hit < 90 else 'ok'
    return {'status': status, **row}


async def _check_freshness() -> dict:
    # Informational only: OwnTracks may be in Manual mode, so a long gap since
    # the last fix is not in itself a fault. See location-db/monitoring.
    row = (await _fetchrows(
        "SELECT max(to_timestamp(tst)) AS latest_fix_utc, "
        "extract(epoch FROM now()-max(to_timestamp(tst)))::int AS age_secs, "
        "count(*) FILTER (WHERE to_timestamp(tst) > now()-interval '24 hours') "
        "AS fixes_last_24h FROM locations"))[0]
    return {'status': 'info', **row}


async def _check_instance() -> dict:
    row = (await _fetchrows(
        "SELECT current_setting('server_version') AS version, "
        "extract(epoch FROM now()-pg_postmaster_start_time())::int AS uptime_secs, "
        "pg_is_in_recovery() AS in_recovery, "
        "(SELECT count(*) FROM pg_stat_replication) AS replica_count"))[0]
    return {'status': 'info', **row}


_CHECKS = {
    'disk':         _check_disk,
    'db_size':      _check_db_size,
    'connections':  _check_connections,
    'long_queries': _check_long_queries,
    'bloat':        _check_bloat,
    'xid':          _check_xid,
    'locks':        _check_locks,
    'cache':        _check_cache,
    'freshness':    _check_freshness,
    'instance':     _check_instance,
}


async def _run_check(target: str) -> str:
    target = (target or 'all').strip().lower()
    if target in ('all', 'summary'):
        results = {}
        for name, fn in _CHECKS.items():
            try:
                results[name] = await fn()
            except Exception as exc:
                logger.warning('check %s failed: %s', name, exc)
                results[name] = {'status': 'warn', 'error': str(exc)}
        overall = _worst([r.get('status', 'ok') for r in results.values()])
        return json.dumps({'target': 'all', 'overall_status': overall,
                           'checks': results}, default=str)
    fn = _CHECKS.get(target)
    if fn is None:
        return json.dumps({'error': f'unknown target: {target}',
                           'available': sorted(_CHECKS) + ['all']})
    try:
        result = await fn()
    except Exception as exc:
        logger.warning('check %s failed: %s', target, exc)
        return json.dumps({'target': target, 'status': 'warn', 'error': str(exc)})
    return json.dumps({'target': target, **result}, default=str)


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
    # Bump this server version whenever Misc changes; shown in MCP client setup.
    server = Server('misc', version='1.27.3')

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = [
            types.Tool(
                name='hello',
                description='Say hello. Returns a greeting. For overview of all misc connector services, see notes key: misc-server',
                inputSchema={'type': 'object', 'properties': {}, 'required': []},
            ),
            types.Tool(
                name='pg_query',
                description=(
                    'Execute SQL against OwnTracks. Read notes location-db/usage for '
                    'parameters, permissions and examples; location-db/schema for schema.'
                ),
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'sql': {
                            'type': 'string',
                            'description': 'One SQL statement. PostGIS spatial functions are available.',
                        },
                        'login': {
                            'type': 'string',
                            'enum': list(PG_LOGINS),
                            'default': 'owntracks_ro',
                            'description': 'Database login whose permissions apply to this statement.',
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
                        'uid': {'type': 'string', 'pattern': '^[1-9][0-9]*$', 'description': 'Stable IMAP UID from email_list'},
                        'seq_id': {
                            'type': 'string',
                            'description': 'Legacy sequence ID; prefer the stable uid from email_list',
                        },
                    },
                    'anyOf': [{'required': ['uid']}, {'required': ['seq_id']}],
                },
            ),
            types.Tool(
                name='email_list_folders',
                description='List all mailbox folders available on the server. Documentation: misc-server/email-tools',
                inputSchema={'type': 'object', 'properties': {}, 'required': []},
            ),
        ]
        tools.append(types.Tool(
                name='check',
                description=(
                    'Health/monitoring check for the gravlax database host.\n\n'
                    'Pass a target naming what to inspect; omit or use "all" for a '
                    'full rollup with an overall_status.\n\n'
                    'Targets:\n'
                    '  disk         - filesystem free space (/ and /mnt; host-level, invisible to SQL)\n'
                    '  db_size      - size of each database\n'
                    '  connections  - backend count vs max_connections\n'
                    '  long_queries - active queries running > 30s\n'
                    '  bloat        - dead-tuple % and last autovacuum per table\n'
                    '  xid          - transaction-id wraparound headroom\n'
                    '  locks        - waiting / blocked locks\n'
                    '  cache        - buffer cache hit ratio, deadlocks, temp usage\n'
                    '  freshness    - latest OwnTracks fix age (informational; Manual mode may be stale legitimately)\n'
                    '  instance     - version, uptime, recovery/replication\n'
                    '  all          - run every check (default)\n\n'
                    'Each check returns status: ok | warn | crit | info.\n\n'
                    'Usage notes: location-db/monitoring (public notes store)'
                ),
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'target': {
                            'type': 'string',
                            'description': 'What to check; omit for "all".',
                            'enum': ['all', 'disk', 'db_size', 'connections',
                                     'long_queries', 'bloat', 'xid', 'locks',
                                     'cache', 'freshness', 'instance'],
                        },
                    },
                    'required': [],
                },
            ))
        tools.append(types.Tool(name='webdav', description='Read/write WebDAV files. See notes misc-server/webdav.', inputSchema=misc_webdav.TOOL_SCHEMA))
        plugin_names = {tool.name for tool in tools}
        for plugin in discover_tools():
            if plugin.name in plugin_names:
                logger.warning('Skipping plugin tool that conflicts with built-in: %s', plugin.name)
                continue
            tools.append(types.Tool(
                name=plugin.name,
                description=plugin.description,
                inputSchema=plugin.input_schema,
            ))
        prefix = f'[{store_name} store] '
        for t in tools:
            t.description = prefix + t.description
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        if name == 'webdav':
            return [types.TextContent(type='text', text=await misc_webdav.call(arguments))]
        if name == 'check':
            return [types.TextContent(type='text', text=await _run_check(arguments.get('target') or 'all'))]
        if name == 'hello':
            return [types.TextContent(type='text', text='hello')]
        if name == 'pg_query':
            sql = (arguments.get('sql') or '').strip()
            if not sql:
                return [types.TextContent(type='text', text='sql argument is required.')]
            result = await _pg_query(sql, arguments.get('login', 'owntracks_ro'))
            return [types.TextContent(type='text', text=result)]
        if name == 'email_send':
            to = arguments.get('to', '').strip()
            subject = arguments.get('subject', '').strip()
            body = arguments.get('body', '')
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
            uid = arguments.get('uid', '').strip()
            if not seq_id and not uid:
                return [types.TextContent(type='text', text='uid or seq_id argument is required.')]
            result = await _email_read(folder, seq_id, uid) if uid else await _email_read(folder, seq_id)
            return [types.TextContent(type='text', text=result)]
        if name == 'email_list_folders':
            result = await _email_list_folders()
            return [types.TextContent(type='text', text=result)]
        for plugin in discover_tools():
            if plugin.name == name:
                result = await plugin.invoke(arguments)
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
    gdata_oauth.startup_check()
    parser = argparse.ArgumentParser(description='misc MCP server')
    parser.add_argument('--rest-port', type=int, default=int(os.getenv('MISC_REST_PORT', 8220)))
    parser.add_argument('--mcp-port',  type=int, default=int(os.getenv('MISC_MCP_PORT',  8223)))
    parser.add_argument('--rest-host', default=None)
    parser.add_argument('--mcp-host',  default=None)
    parser.add_argument('--host',      default=None, help='(deprecated: use --rest-host and --mcp-host)')
    parser.add_argument('--name',      default=os.getenv('GDATA_STORE_NAME', 'misc'))
    args = parser.parse_args()

    legacy_host = args.host or os.getenv('MISC_HOST', '127.0.0.1')
    args.rest_host = args.rest_host or args.host or os.getenv('REST_HOST', legacy_host)
    args.mcp_host = args.mcp_host or args.host or os.getenv('MCP_HOST', legacy_host)

    log_level = os.getenv('LOG_LEVEL', 'info').lower()

    logger.info('Connecting to PostgreSQL pools...')
    async with contextlib.AsyncExitStack() as pool_stack:
        for login in PG_LOGINS:
            try:
                _pools[login] = await pool_stack.enter_async_context(asyncpg.create_pool(
                    database='owntracks',
                    user=login,
                    min_size=1,
                    max_size=3,
                    max_inactive_connection_lifetime=300,
                    max_queries=50_000,
                    command_timeout=30,
                ))
            except Exception:
                logger.exception('PostgreSQL pool unavailable (login=%s)', login)
        logger.info('PostgreSQL pools ready.')

        rest_app = make_rest_app(args.rest_port, args.name)
        mcp_app  = make_mcp_app(args.name)

        rest_cfg = uvicorn.Config(rest_app, host=args.rest_host, port=args.rest_port, log_level=log_level)
        mcp_cfg  = uvicorn.Config(mcp_app,  host=args.mcp_host, port=args.mcp_port,  log_level=log_level)

        logger.info(f'REST on {args.rest_host}:{args.rest_port}  |  MCP on {args.mcp_host}:{args.mcp_port}  |  name={args.name}')

        try:
            await asyncio.gather(
                uvicorn.Server(rest_cfg).serve(),
                uvicorn.Server(mcp_cfg).serve(),
            )
        finally:
            _pools.clear()


if __name__ == '__main__':
    asyncio.run(main())
