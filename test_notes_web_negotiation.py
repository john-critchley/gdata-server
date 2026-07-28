"""Content-negotiation tests for notes_web: Accept header -> response format.

Format is chosen purely from Accept (the whole URL tail is the note key), so
these lock in that mapping and the rendered media types.
"""

import notes_web as w


def test_negotiate_defaults_to_html():
    assert w.negotiate_format(None) == "html"
    assert w.negotiate_format("") == "html"
    assert w.negotiate_format("*/*") == "html"
    # Typical browser Accept
    assert w.negotiate_format(
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ) == "html"


def test_negotiate_specific_types():
    assert w.negotiate_format("application/json") == "json"
    assert w.negotiate_format("application/jsonhtl+json") == "json"
    assert w.negotiate_format("text/markdown") == "markdown"
    assert w.negotiate_format("text/plain") == "plain"
    assert w.negotiate_format("application/yaml") == "yaml"
    assert w.negotiate_format("text/yaml") == "yaml"


def test_negotiate_q_values_and_406():
    # Higher q wins over source order
    assert w.negotiate_format("text/html;q=0.1, application/json;q=0.9") == "json"
    # Nothing we can produce, no wildcard -> 406 (None)
    assert w.negotiate_format("application/pdf") is None


def _doc():
    return {"title": "T", "tags": ["a", "b"],
            "content": [{"para": ["hi ", {"strong": "x"}, " ", {"em": "y"}]}]}


def test_serve_note_media_types_and_vary():
    doc = _doc()
    checks = {
        "application/json": "application/json",
        "application/yaml": "application/yaml",
        "text/markdown": "text/markdown; charset=utf-8",
        "text/plain": "text/plain; charset=utf-8",
    }
    for accept, ctype in checks.items():
        r = w._serve_note("T", doc, accept)
        assert r.status_code == 200
        assert r.headers["content-type"] == ctype
        assert r.headers["vary"] == "Accept"


def test_serve_note_markdown_body():
    body = w._serve_note("T", _doc(), "text/markdown").body.decode()
    assert "# T" in body
    assert "**x**" in body and "*y*" in body   # strong/em standardised
    assert "- **tags:**" in body               # metadata section


def test_serve_note_406_body():
    r = w._serve_note("T", _doc(), "application/pdf")
    assert r.status_code == 406
    assert r.headers["vary"] == "Accept"
