"""Markdown/plain output of nested `section` blocks (jsonhtl_md.render_block).

The md/plain content-negotiated format must keep block coverage in step with the
HTML renderers — sections included. See proposals/section-editing.
"""
from jsonhtl_md import render_block


def _md(block, level=2):
    return "\n".join(render_block(block, level))


def test_section_to_markdown_nested_and_trailing():
    md = _md({"section": {"title": "Discussion", "content": [
        {"para": ["lead"]},
        {"section": {"title": "Fixes", "content": [{"para": ["fix"]}]}},
        {"para": ["concluding"]},
    ]}})
    assert "## Discussion" in md
    assert "### Fixes" in md
    assert "lead" in md and "fix" in md and "concluding" in md
    # outer-level trailing paragraph comes after the sub-section heading
    assert md.index("### Fixes") < md.index("concluding")


def test_section_level_override_and_clamp():
    assert "#### T" in _md({"section": {"title": "T", "level": 4, "content": []}})
    assert _md({"section": {"title": "T", "level": 9, "content": []}}).startswith("###### T")


def test_section_missing_title_no_heading_line():
    md = _md({"section": {"content": [{"para": ["body"]}]}})
    assert "body" in md
    assert not md.lstrip().startswith("#")


def test_deep_nesting_caps_heading_hashes_at_6():
    doc = {"title": "deep", "content": []}
    for i in range(6):
        doc = {"title": f"L{i}", "content": [{"section": doc}]}
    md = _md({"section": doc})
    assert "#######" not in md  # never 7 hashes
    assert "######" in md
