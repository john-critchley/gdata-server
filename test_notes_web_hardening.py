"""Renderer hardening tests: a malformed (double-encoded / list-wrapped /
non-dict) document must never 500 with a traceback. _coerce_doc auto-recovers
the known corruption shapes; anything left non-dict yields a clean error page.
"""

import json

import notes_web as w


GOOD = {"title": "T", "content": [{"para": ["hi ", {"strong": "x"}]}]}


# --- _coerce_doc ---------------------------------------------------------

def test_coerce_passes_through_dict():
    assert w._coerce_doc(GOOD) is GOOD


def test_coerce_unwraps_single_and_double_encoding():
    once = json.dumps(GOOD)                 # double-encoded (str)
    twice = json.dumps(once)                # triple-encoded (str of str)
    assert w._coerce_doc(once) == GOOD
    assert w._coerce_doc(twice) == GOOD


def test_coerce_unwraps_list_wrapped_ops():
    assert w._coerce_doc([GOOD]) == GOOD


def test_coerce_leaves_unrecoverable_as_is():
    # A plain non-JSON string can't become a dict; returned unchanged, no raise.
    assert w._coerce_doc("just text") == "just text"


# --- _render_page guard --------------------------------------------------

def test_render_page_on_non_dict_returns_error_page_not_raise():
    html = w._render_page("K", "not a dict")
    assert "Note could not be rendered" in html
    assert "K" in html


# --- _serve_note end to end ---------------------------------------------

def test_serve_note_recovers_double_encoded_for_all_formats():
    enc = json.dumps(GOOD)  # simulate the double-encoding corruption
    # HTML now renders (recovered) instead of 500
    r_html = w._serve_note("T", enc, "text/html")
    assert r_html.status_code == 200
    # JSON passthrough returns the recovered object, not the quoted string
    r_json = w._serve_note("T", enc, "application/json")
    assert json.loads(r_json.body.decode()) == GOOD
    # Markdown recovered too
    r_md = w._serve_note("T", enc, "text/markdown")
    assert "**x**" in r_md.body.decode()


def test_serve_note_unrecoverable_is_clean_500_not_exception():
    r = w._serve_note("K", "just text", "text/html")
    assert r.status_code == 500
    assert "Note could not be rendered" in r.body.decode()
    assert r.headers["vary"] == "Accept"
