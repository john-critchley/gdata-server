"""HTML inline-span rendering tests for notes_web.

Covers the standardised dict-form inline spans (_INLINE_SPAN_TAGS) added so the
web renderer matches the desktop browser: {"strong"|"em"|"italic"|"bold"|"code"}
plus links, escaping, and the spanning-emphasis second pass in _render_inlines.
"""

import notes_web as w


def test_dict_span_types_map_to_tags():
    assert w._render_inline({"code": "c"}) == "<code>c</code>"
    assert w._render_inline({"bold": "b"}) == "<strong>b</strong>"
    assert w._render_inline({"strong": "s"}) == "<strong>s</strong>"
    assert w._render_inline({"italic": "i"}) == "<em>i</em>"
    assert w._render_inline({"em": "e"}) == "<em>e</em>"


def test_span_content_is_html_escaped():
    assert w._render_inline({"strong": "<x>&"}) == "<strong>&lt;x&gt;&amp;</strong>"
    assert w._render_inline({"code": "a<b"}) == "<code>a&lt;b</code>"


def test_unknown_dict_renders_empty():
    # A dict whose first key isn't a known span/link type is dropped, not dumped.
    assert w._render_inline({"mystery": "z"}) == ""


def test_link_internal_and_external():
    internal = w._render_inline({"link": {"href": "Foo/Bar", "text": "FB"}})
    assert internal == '<a href="/notes/Foo/Bar">FB</a>'
    external = w._render_inline({"link": {"href": "https://example.com", "text": "E"}})
    assert 'href="https://example.com"' in external


def test_string_markdown_inline():
    # Plain string items go through _md_inline (markdown emphasis + escaping).
    out = w._render_inline("say **hi** and `x` and *lo*")
    assert "<strong>hi</strong>" in out
    assert "<code>x</code>" in out
    assert "<em>lo</em>" in out


def test_render_inlines_emphasis_spanning_items():
    # A **bold** span split across items by an intervening span should still
    # close, via the second-pass regex in _render_inlines.
    out = w._render_inlines(["**foo ", {"code": "c"}, " bar**"])
    assert "<strong>foo <code>c</code> bar</strong>" in out


def test_para_block_uses_inline_spans():
    html = w._render_block({"para": ["a ", {"em": "b"}, " c"]})
    assert html == "<p>a <em>b</em> c</p>"
