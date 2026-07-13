#!/usr/bin/python3
"""PostgreSQL mirror for OwnTracks location data.

Called from owntracks_loc.py after successful SQLite write.
All errors are caught and logged; nothing here can affect the caller.

Connects via local Unix socket as DB user 'owntracks' (trust auth).

Notes (public store):
  location-db/architecture — module design, dual-write, retry pattern
  location-db/schema       — table DDL and column descriptions
"""

import json
import logging
import sys

DSN = "dbname=owntracks user=owntracks"

_conn = None


def _get_conn():
    global _conn
    try:
        import psycopg2
        if _conn is None or _conn.closed:
            _conn = psycopg2.connect(DSN)
            _conn.autocommit = True
        return _conn
    except Exception as exc:
        logging.error("owntracks_pg: connect failed: %s", exc)
        return None


def insert_location(payload, received_at):
    """Insert a location payload dict into PostgreSQL.

    payload     — the parsed JSON dict from the OwnTracks POST body
    received_at — Unix timestamp (int) when the request was received
    """
    try:
        _insert(payload, received_at)
    except Exception as exc:
        logging.error("owntracks_pg: insert_location error: %s", exc)


def _insert(payload, received_at):
    import psycopg2
    from datetime import datetime, timezone

    lat = float(payload['lat'])
    lon = float(payload['lon'])
    tst = int(payload['tst'])

    def _f(key):
        v = payload.get(key)
        return float(v) if v is not None else None

    def _i(key):
        v = payload.get(key)
        return int(v) if v is not None else None

    def _s(key):
        v = payload.get(key)
        return str(v) if v is not None else None

    def _arr(key):
        v = payload.get(key)
        if isinstance(v, list):
            return v if v else None
        return None

    received_dt = datetime.fromtimestamp(received_at, tz=timezone.utc)

    row = {
        'received_at':      received_dt,
        'tst':              tst,
        'lat':              lat,
        'lon':              lon,
        'acc':              _f('acc'),
        'vac':              _f('vac'),
        'alt':              _f('alt'),
        'batt':             _i('batt'),
        'bs':               _i('bs'),
        'conn':             _s('conn'),
        'ssid':             _s('ssid'),
        'bssid':            _s('bssid'),
        't':                _s('t'),
        'm':                _i('m'),
        'p':                _f('p'),
        'tid':              _s('tid'),
        'motionactivities': _arr('motionactivities'),
        'inregions':        _arr('inregions'),
        'topic':            payload.get('topic'),
    }

    conn = _get_conn()
    if conn is None:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO locations (
                    received_at, tst, lat, lon, geom,
                    acc, vac, alt, batt, bs, conn, ssid, bssid,
                    t, m, p, tid, motionactivities, inregions, topic
                ) VALUES (
                    %(received_at)s, %(tst)s, %(lat)s, %(lon)s,
                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                    %(acc)s, %(vac)s, %(alt)s, %(batt)s, %(bs)s,
                    %(conn)s, %(ssid)s, %(bssid)s,
                    %(t)s, %(m)s, %(p)s, %(tid)s,
                    %(motionactivities)s, %(inregions)s, %(topic)s
                )
                ON CONFLICT (tst, topic) DO NOTHING
            """, row)
    except psycopg2.Error as exc:
        logging.error("owntracks_pg: SQL error: %s", exc)
        # Reset connection so next call gets a fresh one
        global _conn
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


def backfill_from_sqlite(db_path, batch_size=500):
    """Backfill all rows from the SQLite database into PostgreSQL.

    Reads raw_json from each SQLite row and calls _insert().
    Skips rows that already exist (ON CONFLICT DO NOTHING).
    Prints progress to stdout.
    """
    import sqlite3

    sqlite_conn = sqlite3.connect(db_path, timeout=10)
    rows = sqlite_conn.execute(
        "SELECT received_at, raw_json FROM locations ORDER BY tst ASC"
    ).fetchall()
    sqlite_conn.close()

    total = len(rows)
    print(f"Backfilling {total} rows from SQLite into PostgreSQL...", flush=True)

    inserted = skipped = errors = 0
    for i, (received_at, raw_json) in enumerate(rows):
        try:
            payload = json.loads(raw_json)
            _insert(payload, received_at)
            inserted += 1
        except Exception as exc:
            errors += 1
            logging.error("owntracks_pg: backfill row %d error: %s", i, exc)

        if (i + 1) % batch_size == 0:
            print(f"  {i+1}/{total}...", flush=True)

    print(f"Done. inserted={inserted} skipped/conflict={total-inserted-errors} errors={errors}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    backfill_from_sqlite('/var/lib/owntracks/locations.db')
