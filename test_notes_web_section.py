"""Tests for nested `section` block rendering in notes_web (Phase 0).

A section renders <section><hN>title</hN> + recursed content, level following
nesting depth (base 2), overridable via section['level'], capped at h6. Non-
breaking: all existing block types keep rendering as before. See
proposals/section-editing.
"""
import notes_web as w


def test_section_renders_heading_and_content():
    html = w._render_block({"section": {"title": "Discussion", "content": [{"para": ["hi"]}]}})
    assert "<section>" in html and "</section>" in html
    assert "<h2>Discussion</h2>" in html
    assert "<p>hi</p>" in html


def test_nested_section_deepens_level():
    html = w._render_block({"section": {"title": "Outer", "content": [
        {"para": ["a"]},
        {"section": {"title": "Inner", "content": [{"para": ["b"]}]}},
    ]}})
    assert "<h2>Outer</h2>" in html
    assert "<h3>Inner</h3>" in html
    assert html.index("<h2>Outer</h2>") < html.index("<h3>Inner</h3>")


def test_section_explicit_level_override():
    html = w._render_block({"section": {"title": "T", "level": 4, "content": []}})
    assert "<h4>T</h4>" in html


def test_section_title_is_escaped():
    html = w._render_block({"section": {"title": "a<b>&c", "content": []}})
    assert "a&lt;b&gt;&amp;c" in html


def test_deep_nesting_caps_at_h6():
    doc = {"title": "deepest", "content": []}
    for i in range(6):
        doc = {"title": f"L{i}", "content": [{"section": doc}]}
    html = w._render_block({"section": doc})
    assert "<h6>" in html
    assert "<h7>" not in html


def test_outer_level_trailing_paragraph():
    # The case the flat model could not express: a paragraph that belongs to the
    # OUTER section, after an inner sub-section.
    html = w._render_block({"section": {"title": "Discussion", "content": [
        {"para": ["A"]},
        {"section": {"title": "Fixes", "content": [{"para": ["F"]}]}},
        {"para": ["concluding"]},
    ]}})
    assert html.index("<h3>Fixes</h3>") < html.index("<p>concluding</p>")
    assert html.rindex("</section>") > html.index("<p>concluding</p>")


def test_section_content_can_hold_links_and_tables():
    html = w._render_block({"section": {"title": "S", "content": [
        {"para": ["see ", {"link": {"href": "other", "text": "other"}}]},
        {"table": {"columns": ["A"], "rows": [["1"]]}},
    ]}})
    assert '<a href="/notes/other">other</a>' in html
    assert "<td>1</td>" in html


def test_render_content_threads_level_to_top_level_sections():
    html = w._render_content([{"section": {"title": "Top", "content": [{"para": ["x"]}]}}])
    assert "<h2>Top</h2>" in html
