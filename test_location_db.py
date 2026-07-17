"""
test_location_db.py — tests for the OwnTracks PostgreSQL location database,
reached via the [postgres] stunnel tunnel (local :5432 -> gravlax:15432 ->
the owntracks Unix socket). See location-db/architecture in notes.

Requires the stunnel tunnel to be up and gravlax reachable. Skipped
automatically if a connection can't be made.

Run: python -m pytest test_location_db.py -v
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DB_HOST = "127.0.0.1"
DB_PORT = 5432
DB_NAME = "owntracks"
DB_USER = "owntracks_ro"

EXPECTED_COLUMNS = {
    "id", "received_at", "tst", "lat", "lon", "geom", "acc", "vac", "alt",
    "batt", "bs", "conn", "ssid", "bssid", "t", "m", "p", "tid",
    "motionactivities", "inregions", "topic",
}


def _try_connect():
    try:
        return psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
            connect_timeout=5,
        )
    except Exception as exc:
        pytest.skip(f"cannot reach location db via tunnel: {exc!r}")


@pytest.fixture
def conn():
    c = _try_connect()
    yield c
    c.close()


def test_connect_and_select_one(conn):
    cur = conn.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone() == (1,)


def test_locations_table_has_expected_columns(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'locations'"
    )
    cols = {row[0] for row in cur.fetchall()}
    assert EXPECTED_COLUMNS <= cols


def test_can_read_rows_with_sane_shape(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT tst, lat, lon, acc FROM locations ORDER BY tst DESC LIMIT 5"
    )
    rows = cur.fetchall()
    assert len(rows) > 0
    for tst, lat, lon, acc in rows:
        assert isinstance(tst, int)
        assert -90.0 <= lat <= 90.0
        assert -180.0 <= lon <= 180.0


def test_read_only_user_cannot_write(conn):
    """owntracks_ro must be SELECT-only — this is the safety property the
    pg_query MCP tool and this tunnel both rely on.
    """
    cur = conn.cursor()
    with pytest.raises(psycopg2.Error):
        cur.execute(
            "INSERT INTO locations (received_at, tst, lat, lon, topic) "
            "VALUES (now(), 0, 0.0, 0.0, 'test/permission-probe')"
        )
        conn.commit()
    conn.rollback()


def test_postgis_extension_available(conn):
    cur = conn.cursor()
    cur.execute("SELECT extname FROM pg_extension WHERE extname = 'postgis'")
    assert cur.fetchone() is not None
