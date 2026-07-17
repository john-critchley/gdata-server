"""
test_notes_web_svg.py — tests for the {"svg": {...}} JSONHTL block in the
public web renderer (notes_web.py).

No display/GUI required — this only exercises the pure HTML-string
renderer, not the wx desktop browsers (see svg_render.py for that side).

Run: python -m pytest test_notes_web_svg.py -v
"""
import base64
import re

import notes_web

SAMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
    '<rect width="100" height="50" fill="red"/></svg>'
)


def _extract_data_uri(html: str) -> str:
    m = re.search(r'src="(data:image/svg\+xml;base64,[^"]+)"', html)
    assert m, f"no data: URI <img> found in: {html!r}"
    return m.group(1)


def test_svg_block_renders_as_data_uri_img():
    html = notes_web._render_block({"svg": {"body": SAMPLE_SVG}})
    src = _extract_data_uri(html)
    b64_payload = src[len("data:image/svg+xml;base64,"):]
    decoded = base64.b64decode(b64_payload).decode("utf-8")
    assert decoded == SAMPLE_SVG


def test_svg_body_is_not_inlined_raw():
    """The whole point: raw <svg>/<script> must never appear verbatim in
    the output, only inside the base64 payload (non-executable as an img).
    """
    malicious = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<script>alert(1)</script></svg>'
    )
    html = notes_web._render_block({"svg": {"body": malicious}})
    assert "<script>" not in html
    assert "<svg" not in html.split("base64,")[0]  # no raw svg tag outside the data URI


def test_svg_alt_is_escaped_and_set():
    html = notes_web._render_block({"svg": {"body": SAMPLE_SVG, "alt": '"><script>x</script>'}})
    assert "<script>" not in html
    assert 'alt="' in html


def test_svg_caption_renders_as_figcaption():
    html = notes_web._render_block({"svg": {"body": SAMPLE_SVG, "caption": "Architecture diagram"}})
    assert "<figcaption>Architecture diagram</figcaption>" in html
    assert "<figure>" in html


def test_svg_without_caption_has_no_figcaption():
    html = notes_web._render_block({"svg": {"body": SAMPLE_SVG}})
    assert "<figcaption>" not in html


def test_svg_missing_body_renders_nothing():
    assert notes_web._render_block({"svg": {}}) == ""
    assert notes_web._render_block({"svg": {"body": ""}}) == ""
    assert notes_web._render_block({"svg": "not a dict"}) == ""


def test_svg_block_reachable_via_render_block_dispatch():
    """Regression guard: svg must be wired into the block-type dispatch,
    not just exist as a standalone function."""
    html = notes_web._render_block({"svg": {"body": SAMPLE_SVG}})
    assert html != ""
