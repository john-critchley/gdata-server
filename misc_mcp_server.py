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
    if _pool is None:
        raise RuntimeError('Database pool not available.')
    async with _pool.acquire() as conn:
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
        if name == 'check':
            result = await _run_check(arguments.get('target') or 'all')
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
