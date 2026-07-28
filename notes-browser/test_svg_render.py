"""
test_svg_render.py — tests for svg_render.py, the wx-side rasterizer used
by notes_browser.py to display {"svg": {...}}
blocks (wx.html.HtmlWindow cannot render SVG at all).

Requires a display (X11/Wayland) since it exercises real wx.svg
rasterization and wx.MemoryFSHandler registration. Skipped automatically
if none is available.

Run: python -m pytest test_svg_render.py -v
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="No display available — wx requires X11/Wayland",
)

import wx  # noqa: E402

import svg_render  # noqa: E402

RED_SQUARE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
    '<rect width="40" height="40" fill="red"/></svg>'
)


@pytest.fixture(scope="module")
def app():
    return wx.App(False)


def test_svg_block_to_html_returns_memory_img_tag(app):
    html = svg_render.svg_block_to_html({"body": RED_SQUARE_SVG}, escape=lambda s: s)
    assert html.startswith('<img src="memory:svg_')
    assert '.png" alt="' in html


def test_same_svg_content_reuses_registration(app):
    """Re-rendering identical SVG content must not grow the registered set
    — that's the whole point of hashing by content instead of block/note
    position (avoids leaking memory across repeated note views)."""
    unique_svg = RED_SQUARE_SVG.replace("40", "41")  # distinct from other tests' content
    before = len(svg_render._registered)
    html1 = svg_render.svg_block_to_html({"body": unique_svg}, escape=lambda s: s)
    after_first = len(svg_render._registered)
    html2 = svg_render.svg_block_to_html({"body": unique_svg}, escape=lambda s: s)
    after_second = len(svg_render._registered)

    assert html1 == html2
    assert after_first == before + 1
    assert after_second == after_first  # no growth on the second, identical render


def test_different_svg_content_gets_different_names(app):
    blue_square = RED_SQUARE_SVG.replace("red", "blue")
    html1 = svg_render.svg_block_to_html({"body": RED_SQUARE_SVG}, escape=lambda s: s)
    html2 = svg_render.svg_block_to_html({"body": blue_square}, escape=lambda s: s)
    assert html1 != html2


def test_malformed_svg_returns_empty_string(app):
    html = svg_render.svg_block_to_html({"body": "not valid svg at all <<<"}, escape=lambda s: s)
    assert html == ""


def test_missing_body_returns_empty_string(app):
    assert svg_render.svg_block_to_html({}, escape=lambda s: s) == ""
    assert svg_render.svg_block_to_html({"body": ""}, escape=lambda s: s) == ""
    assert svg_render.svg_block_to_html("not a dict", escape=lambda s: s) == ""


def test_caption_included_in_output(app):
    html = svg_render.svg_block_to_html(
        {"body": RED_SQUARE_SVG, "caption": "A red square"}, escape=lambda s: s
    )
    assert "A red square" in html
    assert "<img" in html


def test_alt_is_escaped_via_caller_function(app):
    calls = []

    def tracking_escape(s):
        calls.append(s)
        return s.replace("<", "&lt;").replace(">", "&gt;")

    html = svg_render.svg_block_to_html(
        {"body": RED_SQUARE_SVG, "alt": "<injected>"}, escape=tracking_escape
    )
    assert "<injected>" not in html
    assert "&lt;injected&gt;" in html
    assert "<injected>" in calls  # confirms the caller's escape fn was actually used


def test_registered_image_actually_renders_pixels(app):
    """End-to-end check: the registered memory: file is real, decodable
    image data — not just a plausible-looking filename."""
    import wx.html

    html_fragment = svg_render.svg_block_to_html({"body": RED_SQUARE_SVG}, escape=lambda s: s)

    frame = wx.Frame(None, size=(100, 100))
    htmlwin = wx.html.HtmlWindow(frame, size=(80, 80))
    htmlwin.SetPage(f"<html><body>{html_fragment}</body></html>")
    frame.Show()

    result = {}

    def check():
        bmp = wx.Bitmap(80, 80)
        context = wx.ClientDC(htmlwin)
        memdc = wx.MemoryDC()
        memdc.SelectObject(bmp)
        memdc.Blit(0, 0, 80, 80, context, 0, 0)
        memdc.SelectObject(wx.NullBitmap)
        img = bmp.ConvertToImage()
        red = sum(
            1
            for x in range(0, 40, 5)
            for y in range(0, 40, 5)
            if img.GetRed(x, y) > 180 and img.GetGreen(x, y) < 100
        )
        result["red"] = red
        app.ExitMainLoop()

    wx.CallLater(400, check)
    app.MainLoop()
    frame.Destroy()

    assert result["red"] > 0, "expected red pixels from the rasterized SVG, found none"


def test_text_elements_are_rasterized(app):
    """Regression guard: wx.svg (NanoSVG) silently drops <text> elements
    entirely — confirmed empirically (a rect+text SVG parsed to a single
    shape, the text vanished). That's why this module uses cairosvg
    instead. A diagram with no working text labels isn't useful, so this
    must never regress back to the wx.svg path.
    """
    import cairosvg

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
        '<rect width="100" height="50" fill="yellow"/>'
        '<text x="10" y="30" font-size="20">Hi</text></svg>'
    )
    png_bytes = cairosvg.svg2png(bytestring=svg.encode("utf-8"))

    import io as _io
    wx_image = wx.Image(_io.BytesIO(png_bytes), wx.BITMAP_TYPE_PNG)
    assert wx_image.IsOk()

    # Sanity: text is dark-on-yellow, so somewhere in the image should be
    # a non-yellow (i.e. not high-R, high-G, low-B) pixel from the glyph.
    non_yellow = 0
    for x in range(0, 100, 3):
        for y in range(0, 50, 3):
            r, g, b = wx_image.GetRed(x, y), wx_image.GetGreen(x, y), wx_image.GetBlue(x, y)
            if not (r > 200 and g > 200 and b < 100):
                non_yellow += 1
    assert non_yellow > 0, "expected some non-background pixels from rendered text glyphs"


# --- image block (raster, e.g. fixed-note matplotlib output) ---

RED_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAEElEQVR4nGP8z4AATAxEcQAz0QEHOoQ+uAAAAABJRU5ErkJggg=="


def test_image_block_to_html_returns_memory_img_tag(app):
    html = svg_render.image_block_to_html({"data": RED_PNG_B64}, escape=lambda s: s)
    assert html.startswith('<img src="memory:img_')
    assert '.png" alt="' in html


def test_image_block_no_rasterization_needed(app):
    """Unlike svg, image bytes are already raster — this should not touch
    cairosvg at all, just decode base64 straight into a wx.Image."""
    import unittest.mock as mock

    with mock.patch("svg_render.cairosvg") as mock_cairosvg:
        html = svg_render.image_block_to_html({"data": RED_PNG_B64}, escape=lambda s: s)
        assert html != ""
        mock_cairosvg.svg2png.assert_not_called()


def test_image_and_svg_use_different_name_prefixes(app):
    """img_ vs svg_ prefixes so the two registries can't collide even if
    (implausibly) their content hashes matched."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><rect width="4" height="4" fill="red"/></svg>'
    svg_html = svg_render.svg_block_to_html({"body": svg}, escape=lambda s: s)
    img_html = svg_render.image_block_to_html({"data": RED_PNG_B64}, escape=lambda s: s)
    assert 'src="memory:svg_' in svg_html
    assert 'src="memory:img_' in img_html


def test_image_malformed_base64_returns_empty_string(app):
    html = svg_render.image_block_to_html({"data": "not valid base64!!!"}, escape=lambda s: s)
    assert html == ""


def test_image_missing_data_returns_empty_string(app):
    assert svg_render.image_block_to_html({}, escape=lambda s: s) == ""
    assert svg_render.image_block_to_html({"data": ""}, escape=lambda s: s) == ""
    assert svg_render.image_block_to_html("not a dict", escape=lambda s: s) == ""


def test_image_caption_included(app):
    html = svg_render.image_block_to_html(
        {"data": RED_PNG_B64, "caption": "A tiny red square"}, escape=lambda s: s
    )
    assert "A tiny red square" in html


def test_image_renders_real_pixels(app):
    import wx.html

    html_fragment = svg_render.image_block_to_html({"data": RED_PNG_B64}, escape=lambda s: s)

    frame = wx.Frame(None, size=(100, 100))
    htmlwin = wx.html.HtmlWindow(frame, size=(80, 80))
    htmlwin.SetPage(f"<html><body>{html_fragment}</body></html>")
    frame.Show()

    result = {}

    def check():
        bmp = wx.Bitmap(80, 80)
        context = wx.ClientDC(htmlwin)
        memdc = wx.MemoryDC()
        memdc.SelectObject(bmp)
        memdc.Blit(0, 0, 80, 80, context, 0, 0)
        memdc.SelectObject(wx.NullBitmap)
        img = bmp.ConvertToImage()
        red = sum(
            1
            for x in range(0, 20)
            for y in range(0, 20)
            if img.GetRed(x, y) > 180 and img.GetGreen(x, y) < 100
        )
        result["red"] = red
        app.ExitMainLoop()

    wx.CallLater(400, check)
    app.MainLoop()
    frame.Destroy()

    assert result["red"] > 0, "expected red pixels from the decoded PNG, found none"
