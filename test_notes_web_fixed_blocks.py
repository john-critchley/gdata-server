"""
test_notes_web_fixed_blocks.py — tests for the image and details JSONHTL
block types (added for "fixing" a runnable note's output into a static
note — see notes-browser/fixed-notes).

No display/GUI required — pure HTML-string renderer tests, same style as
test_notes_web_svg.py.

Run: python -m pytest test_notes_web_fixed_blocks.py -v
"""
import base64
import re

import notes_web

# A real, valid 4x4 red PNG (generated via Pillow) — used instead of a
# hand-rolled byte string so downstream desktop-side tests (which actually
# decode it into a wx.Image) have something genuinely valid to work with.
TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAEElEQVR4nGP8z4AATAxEcQAz0QEHOoQ+uAAAAABJRU5ErkJggg=="


def test_image_block_renders_data_uri():
    html = notes_web._render_block({"image": {"format": "png", "data": TINY_PNG_B64}})
    m = re.search(r'src="(data:image/png;base64,[^"]+)"', html)
    assert m
    assert m.group(1) == f"data:image/png;base64,{TINY_PNG_B64}"


def test_image_block_default_format_png():
    html = notes_web._render_block({"image": {"data": TINY_PNG_B64}})
    assert "data:image/png;base64," in html


def test_image_block_caption_and_alt():
    html = notes_web._render_block({"image": {"data": TINY_PNG_B64, "alt": "A plot", "caption": "Fig. 1"}})
    assert 'alt="A plot"' in html
    assert "<figcaption>Fig. 1</figcaption>" in html


def test_image_block_missing_data_renders_nothing():
    assert notes_web._render_block({"image": {}}) == ""
    assert notes_web._render_block({"image": {"data": ""}}) == ""


def test_image_alt_is_escaped():
    html = notes_web._render_block({"image": {"data": TINY_PNG_B64, "alt": '"><script>x</script>'}})
    assert "<script>" not in html


def test_details_renders_native_element():
    html = notes_web._render_block({"details": {"summary": "Cell: fetch", "content": [{"para": ["hidden text"]}]}})
    assert html.startswith("<details>")
    assert "<summary>Cell: fetch</summary>" in html
    assert "<p>hidden text</p>" in html
    assert html.endswith("</details>")


def test_details_summary_is_escaped():
    html = notes_web._render_block({"details": {"summary": "<script>x</script>", "content": []}})
    assert "<script>" not in html


def test_details_nested_blocks_use_full_block_dispatch():
    """content can hold any block type, not just para — e.g. a codeblock,
    matching the actual fixed-note use case (code tucked inside details)."""
    html = notes_web._render_block({
        "details": {"summary": "Cell: fetch (code)", "content": [
            {"codeblock": {"lang": "python", "body": "print('hi')"}}
        ]}
    })
    assert "<pre>" in html
    assert "print(&#x27;hi&#x27;)" in html or "print('hi')" in html


def test_details_missing_summary_and_content_dont_crash():
    assert notes_web._render_block({"details": {}}) == "<details><summary></summary></details>"


def test_page_only_gets_expand_all_control_when_details_present():
    doc_with = {"title": "t", "content": [{"details": {"summary": "s", "content": [{"para": ["x"]}]}}]}
    doc_without = {"title": "t", "content": [{"para": ["x"]}]}
    assert 'class="expand-all-btn"' in notes_web._render_page("k", doc_with)
    assert 'class="expand-all-btn"' not in notes_web._render_page("k", doc_without)


def test_expand_all_uses_inline_onclick_not_a_script_tag():
    """Per-item toggle is genuinely zero-JS (native <details>/<summary>).
    "Expand all" is the one exception: a pure-CSS checkbox+sibling-selector
    version was tried first but confirmed empirically not to work (browsers
    suppress collapsed <details> content via native rendering suppression
    tied to the `open` DOM attribute, not an overridable `display` rule) —
    see JSONHTL_SPEC "details". So this button needs a real, tiny bit of
    JS. Kept as an inline onclick attribute rather than a <script> block,
    which is what this test actually guards.
    """
    doc = {"title": "t", "content": [{"details": {"summary": "s", "content": []}}]}
    page = notes_web._render_page("k", doc)
    assert "onclick=" in page
    assert ".open = true" in page
    assert "<script" not in page
