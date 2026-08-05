"""Conformance tests for codeblock canonicalisation.

Covers the two guarantees this feature adds:

  1. Renderer parity: a codeblock stored with non-canonical aliases
     (language/text) renders its code in *every* renderer -- the bug that
     motivated this was code showing on desktop but empty on the web.

  2. The visible-not-silent 203 contract on the REST surface: PUT normalises +
     self-heals + flags 203; GET of legacy non-canonical storage serves
     canonical content + flags 203 until a sweep (GET->PUT) flips it to 200.
"""
import json
import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
_tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_tf.close()
os.environ.setdefault("OAUTH_TOKEN_FILE", _tf.name)

import gdata_mcp_server as S
from gdata_mcp_server import make_rest_app
import notes_web
import jsonhtl_md

ALIASED_BLOCK = {"codeblock": {"language": "perl", "text": "print 42;"}}
LEGACY_DOC = {"title": "Legacy", "content": [ALIASED_BLOCK]}
CANONICAL_DOC = {"title": "Clean", "content": [
    {"codeblock": {"lang": "perl", "body": "print 42;"}}]}

LEGACY_KEY = "_canon/legacy"
PUT_KEY = "_canon/put"


# ---- 1. renderer parity (pure, no server) ---------------------------------

def test_all_renderers_show_aliased_code():
    html = notes_web._render_block(ALIASED_BLOCK)
    md = "\n".join(jsonhtl_md.render_block(ALIASED_BLOCK))
    # web: code present, and the alias 'language' surfaced as the language class
    assert "print 42;" in html
    assert 'class="language-perl"' in html
    # markdown: fenced with lang + body
    assert "print 42;" in md
    assert "```perl" in md


# ---- 2. REST 203 contract --------------------------------------------------

@pytest.fixture
def client():
    f = tempfile.NamedTemporaryFile(suffix=".gdbm", delete=False)
    f.close()
    S._db = None
    db = S._open_db(f.name)
    # seed legacy storage directly, bypassing db_put's normalisation
    db[LEGACY_KEY] = json.dumps(LEGACY_DOC)
    app = make_rest_app(f.name)
    with TestClient(app) as c:
        yield c
    S._db = None
    for p in (f.name, f.name + ".db"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def test_put_aliased_returns_203_with_warnings(client):
    r = client.put(f"/{PUT_KEY}", json=LEGACY_DOC)
    assert r.status_code == 203
    body = r.json()
    assert body["status"] == "normalised"
    assert body["warnings"] and any("lang" in w for w in body["warnings"])
    assert r.headers["Warning"].startswith("299")
    assert json.loads(r.headers["X-GData-Warnings"]) == body["warnings"]


def test_put_self_heals_so_next_get_is_200(client):
    client.put(f"/{PUT_KEY}", json=LEGACY_DOC)
    r = client.get(f"/{PUT_KEY}")
    assert r.status_code == 200            # storage already canonical
    assert r.json()["content"][0]["codeblock"] == {"lang": "perl", "body": "print 42;"}


def test_put_canonical_is_plain_200(client):
    r = client.put(f"/{PUT_KEY}", json=CANONICAL_DOC)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "warnings" not in r.json()


def test_get_legacy_returns_203_canonical_body(client):
    r = client.get(f"/{LEGACY_KEY}")
    assert r.status_code == 203
    assert r.headers["Warning"].startswith("299")
    # served canonical even though storage is still non-canonical
    assert r.json()["content"][0]["codeblock"] == {"lang": "perl", "body": "print 42;"}


def test_sweep_get_then_put_flips_legacy_to_200(client):
    # a corpus sweep is just: GET (canonical) -> PUT back
    canonical = client.get(f"/{LEGACY_KEY}").json()
    put = client.put(f"/{LEGACY_KEY}", json=canonical)
    assert put.status_code == 200          # canonical in -> clean write
    assert client.get(f"/{LEGACY_KEY}").status_code == 200
