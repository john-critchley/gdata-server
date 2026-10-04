"""Read-only access to local mailbox and mail-analysis GDBM stores.

The public functions in this module are deliberately independent of the MCP
server so they can be registered by another server later.
"""

import asyncio
import json
import logging
import os
from email.parser import BytesParser
from email import policy

from gdata import gdata as GDataJSON, gdata_raw
from mcp_tool_plugins import mcp_tool


logger = logging.getLogger(__name__)

MAILBOX_PATHS = {
    'envoy': os.path.expanduser('~/.email3.meta.gdbm'),
    'other': os.path.expanduser('~/.email3-other.meta.gdbm'),
}
MAIL_PATH = os.path.expanduser('~/.email3.mail.gdbm')
JOBS_PATH = os.path.expanduser('~/.jobserve.gdbm')
BOOKINGS_PATH = os.path.expanduser('~/.dl.gdbm')
MAX_SCAN_RECORDS = 5000
MAX_EMAIL_BYTES = 5 * 1024 * 1024


def _norm(value):
    """Remove leading BOM/encoding bytes used by the mail spool."""
    if isinstance(value, bytes):
        index = 0
        while index < len(value) and value[index] > 0x7f:
            index += 1
        return value[index:]
    index = 0
    while index < len(value) and ord(value[index]) > 0x7f:
        index += 1
    return value[index:]


def _limit(value, default=50):
    try:
        return max(1, min(int(value), 500))
    except (TypeError, ValueError):
        return default


def _locked_or_error(exc):
    text = str(exc)
    if 'lock' in text.lower() or getattr(exc, 'errno', None) in (11, 35):
        return {'status': 'locked', 'error': 'File locked during write; retrying...'}
    logger.error('Mailbox GDBM error: %s', exc)
    return {'status': 'error', 'error': text}


def _record_matches(record, query):
    if not query:
        return True
    return query.casefold() in json.dumps(record, ensure_ascii=False, default=str).casefold()


def _mailbox_search_sync(mailbox_type='envoy', to=None, query=None, limit=50):
    if mailbox_type == 'all':
        mailbox_types = ('envoy', 'other')
    elif mailbox_type in MAILBOX_PATHS:
        mailbox_types = (mailbox_type,)
    else:
        return {'status': 'error', 'error': "mailbox_type must be 'envoy', 'other', or 'all'"}

    results = []
    scanned = 0
    try:
        for current_type in mailbox_types:
            path = MAILBOX_PATHS[current_type]
            if not os.path.exists(path):
                if mailbox_type == 'all':
                    continue
                return {'status': 'error', 'error': f'Mailbox does not exist: {path}'}
            with GDataJSON(gdbm_file=path, mode='r') as db:
                for uidl in sorted(db.keys()):
                    scanned += 1
                    if scanned > MAX_SCAN_RECORDS:
                        return {'status': 'ok', 'count': len(results), 'results': results, 'truncated': True}
                    try:
                        record = db[uidl]
                        if not isinstance(record, dict):
                            record = {'value': record}
                        if to is not None and record.get('to') != to:
                            continue
                        if not _record_matches(record, query):
                            continue
                        results.append({'mailbox': current_type, 'uidl': uidl, **record})
                        if len(results) >= _limit(limit):
                            return {'status': 'ok', 'count': len(results), 'results': results}
                    except Exception as exc:
                        logger.warning('Skipping corrupt mailbox record %r: %s', uidl, exc)
        return {'status': 'ok', 'count': len(results), 'results': results}
    except Exception as exc:
        return _locked_or_error(exc)


@mcp_tool(
    'mailbox_search',
    'Search read-only local email metadata GDBM stores. mailbox_type is envoy, other, or all.',
    {
        'type': 'object',
        'properties': {
            'mailbox_type': {'type': 'string', 'enum': ['envoy', 'other', 'all'], 'default': 'envoy'},
            'to': {'type': 'string', 'description': 'Exact recipient filter.'},
            'query': {'type': 'string', 'description': 'Case-insensitive text search across metadata.'},
            'limit': {'type': 'integer', 'default': 50, 'maximum': 500},
        },
        'required': [],
    },
)
async def mailbox_search(mailbox_type='envoy', to=None, query=None, limit=50):
    return json.dumps(await asyncio.to_thread(_mailbox_search_sync, mailbox_type, to, query, limit))


def _list_json_store_sync(path, result_key, query=None, limit=50):
    rows = []
    scanned = 0
    try:
        if not os.path.exists(path):
            return {'status': 'error', 'error': f'Database does not exist: {path}'}
        with GDataJSON(gdbm_file=path, mode='r') as db:
            for key in sorted(db.keys()):
                scanned += 1
                if scanned > MAX_SCAN_RECORDS:
                    return {'status': 'ok', 'count': len(rows), result_key: rows, 'truncated': True}
                try:
                    record = db[key]
                    if not _record_matches(record, query):
                        continue
                    rows.append({'key': key, result_key: record})
                    if len(rows) >= _limit(limit):
                        break
                except Exception as exc:
                    logger.warning('Skipping corrupt record %r in %s: %s', key, path, exc)
        return {'status': 'ok', 'count': len(rows), result_key: rows}
    except Exception as exc:
        return _locked_or_error(exc)


@mcp_tool(
    'bookings_list',
    'List read-only David Lloyd booking records from the local GDBM store.',
    {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': 'Case-insensitive text search across booking records.'},
            'limit': {'type': 'integer', 'default': 50, 'maximum': 500},
        },
        'required': [],
    },
)
async def bookings_list(query=None, limit=50):
    return json.dumps(await asyncio.to_thread(_list_json_store_sync, BOOKINGS_PATH, 'bookings', query, limit))


@mcp_tool(
    'jobs_search',
    'Search read-only JobServe analysis records from the local GDBM store.',
    {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': 'Case-insensitive text search across job records.'},
            'limit': {'type': 'integer', 'default': 50, 'maximum': 500},
        },
        'required': [],
    },
)
async def jobs_search(query=None, limit=50):
    return json.dumps(await asyncio.to_thread(_list_json_store_sync, JOBS_PATH, 'jobs', query, limit))


def _email_read_local_sync(uidl):
    try:
        with gdata_raw(gdbm_file=MAIL_PATH, mode='r') as db:
            raw = db[uidl.encode('utf-8')]
        if len(raw) > MAX_EMAIL_BYTES:
            return {'status': 'error', 'error': f'Email exceeds the {MAX_EMAIL_BYTES} byte read limit'}
        message = BytesParser(policy=policy.default).parsebytes(_norm(raw))
        headers = {key: str(message[key]) for key in message.keys()}
        if message.is_multipart():
            parts = [part for part in message.walk() if part.get_content_type() == 'text/plain']
            body = parts[0].get_content() if parts else ''
        else:
            body = message.get_content()
        return {'status': 'ok', 'uidl': uidl, 'headers': headers, 'body': body}
    except KeyError:
        return {'status': 'error', 'error': f'Email {uidl} not found'}
    except Exception as exc:
        return _locked_or_error(exc)


@mcp_tool(
    'email_read_local',
    'Read one raw email from the local read-only email3 GDBM store by UIDL.',
    {
        'type': 'object',
        'properties': {
            'uidl': {'type': 'string', 'description': 'UIDL key from the local email store.'},
        },
        'required': ['uidl'],
    },
)
async def email_read_local(uidl):
    if not uidl:
        return json.dumps({'status': 'error', 'error': 'uidl is required'})
    return json.dumps(await asyncio.to_thread(_email_read_local_sync, str(uidl)))