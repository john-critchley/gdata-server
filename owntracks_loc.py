#!/usr/bin/python3
"""OwnTracks /loc WSGI endpoint — accepts POST, stores to SQLite."""

import json
import os
import sqlite3
import sys
import time
import urllib.request

_WSGI_DIR = os.path.dirname(os.path.abspath(__file__))
if _WSGI_DIR not in sys.path:
    sys.path.insert(0, _WSGI_DIR)

try:
    import owntracks_pg as _pg
except Exception as _pg_import_err:
    print(f'owntracks_loc: failed to import owntracks_pg: {_pg_import_err}', file=sys.stderr)
    _pg = None

DB_PATH     = '/var/lib/owntracks/locations.db'
NOTES_URL   = 'http://127.0.0.1:8020/loc'
CONFIG_PATH = '/etc/owntracks/config.json'
MAX_BODY    = 16384


def _uuid_to_who(topic):
    """Return the 'who' name for a topic UUID, or None if not found."""
    try:
        with open(CONFIG_PATH) as f:
            import json as _json
            users = _json.load(f).get('users', {})
        for name, uuid in users.items():
            if uuid.lower() in (topic or '').lower():
                return name
    except Exception:
        pass
    return None


def _init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id          INTEGER PRIMARY KEY,
            received_at INTEGER NOT NULL,
            topic       TEXT,
            tst         INTEGER,
            lat         REAL NOT NULL,
            lon         REAL NOT NULL,
            acc         REAL,
            batt        INTEGER,
            raw_json    TEXT NOT NULL
        )
    """)
    conn.commit()


def application(environ, start_response):
    if environ.get('REQUEST_METHOD') != 'POST':
        start_response('405 Method Not Allowed',
                       [('Content-Type', 'text/plain'), ('Allow', 'POST')])
        return [b'Method Not Allowed\n']

    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0

    if length > MAX_BODY:
        start_response('413 Payload Too Large', [('Content-Type', 'text/plain')])
        return [b'Payload Too Large\n']

    raw_bytes = environ['wsgi.input'].read(max(length, MAX_BODY))

    try:
        payload = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        start_response('400 Bad Request', [('Content-Type', 'text/plain')])
        return [b'Invalid JSON\n']

    if not isinstance(payload, dict) or payload.get('_type') != 'location':
        start_response('400 Bad Request', [('Content-Type', 'text/plain')])
        return [b'_type must be "location"\n']

    try:
        lat = float(payload['lat'])
        lon = float(payload['lon'])
        tst = int(payload['tst'])
    except (KeyError, ValueError, TypeError):
        start_response('400 Bad Request', [('Content-Type', 'text/plain')])
        return [b'lat, lon and tst are required\n']

    topic = payload.get('topic')
    acc   = payload.get('acc')
    batt  = payload.get('batt')
    # Store the original payload in full; DB permissions protect location data.
    raw_json = raw_bytes.decode('utf-8', errors='replace')

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        _init_db(conn)
        conn.execute(
            """INSERT INTO locations
               (received_at, topic, tst, lat, lon, acc, batt, raw_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (int(time.time()), topic, tst, lat, lon, acc, batt, raw_json),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        import sys
        print(f'owntracks_loc: storage error: {exc}', file=sys.stderr)
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [b'Storage error\n']

    received_at = int(time.time())

    if _pg is not None:
        _pg.insert_location(payload, received_at)

    _write_note(received_at, tst, lat, lon, acc, batt, topic, payload)

    start_response('204 No Content', [])
    return []


def _write_note(received_at, tst, lat, lon, acc, batt, topic, payload):
    import sys
    def _fmt_ts(epoch):
        t = time.gmtime(epoch)
        return f'{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} UTC'

    rows = [
        ['Received',   _fmt_ts(received_at)],
        ['GPS time',   _fmt_ts(tst)],
        ['Lat',        f'{lat:.6f}'],
        ['Lon',        f'{lon:.6f}'],
        ['Accuracy',   f'{acc:.0f} m' if acc is not None else '-'],
        ['Battery',    f'{batt}%'     if batt is not None else '-'],
        ['Topic',      topic or '-'],
        ['Altitude',   f'{payload["alt"]} m' if 'alt' in payload else '-'],
        ['Speed',      f'{payload["vel"]} km/h' if 'vel' in payload else '-'],
        ['Connection', payload.get('conn', '-')],
    ]

    doc = {
        'title': 'Current Location',
        'content': [
            {'heading': {'level': 1, 'text': 'Current Location'}},
            {'table': {'columns': ['Field', 'Value'], 'rows': rows}},
        ],
    }

    try:
        body = json.dumps(doc).encode()
        req  = urllib.request.Request(
            NOTES_URL, data=body, method='PUT',
            headers={'Content-Type': 'application/json'},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        print(f'owntracks_loc: note write failed: {exc}', file=sys.stderr)
