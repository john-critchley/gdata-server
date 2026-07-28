"""Tests for the shared JSONHTL -> Markdown renderer (jsonhtl_md).

This module is imported by both notes_web.py (text/markdown, text/plain) and
notes_to_pdf.py, so these lock in inline spans, block coverage (including the
svg/image/details/table cases the exporter's old private copy missed), and the
whole-note wrapper.
"""

import base64

import jsonhtl_md as m


# --- inline_text ---------------------------------------------------------

def test_inline_spans_to_markdown():
    assert m.inline_text({"code": "c"}) == "`c`"
    assert m.inline_text({"bold": "b"}) == "**b**"
    assert m.inline_text({"strong": "s"}) == "**s**"
    assert m.inline_text({"italic": "i"}) == "*i*"
    assert m.inline_text({"em": "e"}) == "*e*"


def test_inline_link_and_nested_list():
    assert m.inline_text({"link": {"href": "K", "text": "T"}}) == "[T](K)"
    # A link with no href degrades to just its text.
    assert m.inline_text({"link": {"text": "T"}}) == "T"
    assert m.inline_text(["a ", {"strong": "b"}, " c"]) == "a **b** c"


# --- tables --------------------------------------------------------------

def test_table_uses_canonical_columns():
    block = {"table": {"columns": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]}}
    out = m.render_block(block)
    assert out[0] == "| A | B |"
    assert out[1] == "| --- | --- |"
    assert "| 1 | 2 |" in out and "| 3 | 4 |" in out


def test_table_legacy_headers_alias_still_works():
    block = {"table": {"headers": ["X"], "rows": [["v"]]}}
    assert "| X |" in m.render_block(block)[0]


def test_cell_escaping_pipes_and_newlines():
    assert m.markdown_escape_cell("a|b\nc") == "a\\|b<br>c"


# --- media blocks (viewable in Markdown too) -----------------------------

def test_svg_block_becomes_data_uri_image_with_caption():
    out = "\n".join(m.render_block({"svg": {"body": "<svg/>", "caption": "diagram"}}))
    b64 = base64.b64encode(b"<svg/>").decode()
    assert f"![](data:image/svg+xml;base64,{b64})" in out
    assert "*diagram*" in out


def test_image_block_becomes_data_uri_image():
    out = "\n".join(m.render_block({"image": {"data": "AAAA", "format": "png", "alt": "plot"}}))
    assert "![plot](data:image/png;base64,AAAA)" in out


def test_svg_and_image_missing_payload_render_nothing():
    assert m.render_block({"svg": {}}) == []
    assert m.render_block({"image": {"data": ""}}) == []


# --- details / lists / headings / code -----------------------------------

def test_details_summary_and_nested_content():
    out = "\n".join(m.render_block({"details": {"summary": "More", "content": [{"para": ["inner"]}]}}))
    assert "**More**" in out
    assert "inner" in out


def test_lists_ordered_unordered_and_label():
    ul = m.render_block({"bullet_list": ["one", "two"]})
    assert ul[0] == "- one" and ul[1] == "- two"
    ol = m.render_block({"list": {"ordered": True, "label": "Steps", "items": ["a", "b"]}})
    assert "**Steps**" in ol
    assert "1. a" in ol and "2. b" in ol


def test_heading_level_clamped_and_codeblock_lang():
    assert m.render_block({"heading": {"level": 9, "text": "H"}})[0] == "###### H"
    cb = m.render_block({"codeblock": {"lang": "python", "body": "x = 1"}})
    assert cb[0] == "```python" and cb[1] == "x = 1" and cb[2] == "```"


# --- whole-note wrapper --------------------------------------------------

def test_note_to_markdown_title_and_metadata():
    doc = {"title": "T", "tags": ["a", "b"], "content": [{"para": ["hi"]}]}
    md = m.note_to_markdown(doc, "T")
    assert md.startswith("# T\n")
    assert "hi" in md
    assert "## Metadata" in md
    assert '- **tags:** `["a", "b"]`' in md


def test_note_to_markdown_bare_content_list():
    # A non-dict note (bare content list) uses the key as title, no metadata.
    md = m.note_to_markdown([{"para": ["x"]}], "K")
    assert md.startswith("# K\n")
    assert "## Metadata" not in md
