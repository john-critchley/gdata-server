"""
svg_render.py — renders {"svg": {...}} and {"image": {...}} JSONHTL blocks
for the wx desktop browsers (notes_browser.py and notes_browser_runnable.py).

wx.html.HtmlWindow cannot render SVG at all, and (separately, per
sheet_ui.py) its data: URI support is unreliable past a few KB anyway. So
each image is registered in wx.MemoryFSHandler under a content-hash
filename and referenced via a memory: URI <img> tag — same page-loading
mechanism wx already uses for any other image:

- svg blocks are rasterized to a PNG first, via cairosvg — not wx.svg
  (wxPython's bundled NanoSVG-based renderer), which was confirmed
  empirically to silently drop <text> elements entirely (0-length shape
  list for a rect+text SVG), unusable for actual diagrams.
- image blocks are already raster (e.g. a fixed-note matplotlib plot) —
  no rasterization needed, just base64-decode straight into a wx.Image.

Registrations are keyed by a hash of the source bytes, not by note/block
position, so viewing the same image again (including re-navigating to the
same note) does not re-register or leak memory; only genuinely new
content adds an entry. Entries are never removed, but this bounds growth
to the number of *distinct* images seen in a session rather than the
number of times any note is viewed.
"""
import base64
import hashlib
import io

import cairosvg
import wx


_memory_fs_ready = False
_registered = set()


def _ensure_memory_fs():
    global _memory_fs_ready
    if not _memory_fs_ready:
        wx.FileSystem.AddHandler(wx.MemoryFSHandler())
        _memory_fs_ready = True


def _register_wx_image(name: str, wx_image: "wx.Image") -> None:
    _ensure_memory_fs()
    wx.MemoryFSHandler.AddFile(name, wx_image, wx.BITMAP_TYPE_PNG)
    _registered.add(name)


def _register_svg(svg_bytes: bytes) -> str | None:
    """Rasterize svg_bytes and register it in the memory filesystem.

    Returns the memory: filename, or None if the SVG is malformed.
    """
    digest = hashlib.sha1(svg_bytes).hexdigest()[:16]
    name = f"svg_{digest}.png"
    if name in _registered:
        return name
    try:
        png_bytes = cairosvg.svg2png(bytestring=svg_bytes)
        wx_image = wx.Image(io.BytesIO(png_bytes), wx.BITMAP_TYPE_PNG)
        if not wx_image.IsOk():
            return None
    except Exception:
        return None
    _register_wx_image(name, wx_image)
    return name


def _register_raster(raw_bytes: bytes) -> str | None:
    """Load already-raster image bytes (e.g. PNG) and register them.

    Returns the memory: filename, or None if the bytes aren't a decodable
    image.
    """
    digest = hashlib.sha1(raw_bytes).hexdigest()[:16]
    name = f"img_{digest}.png"
    if name in _registered:
        return name
    try:
        wx_image = wx.Image(io.BytesIO(raw_bytes))
        if not wx_image.IsOk():
            return None
    except Exception:
        return None
    _register_wx_image(name, wx_image)
    return name


def _wrap_with_caption(img_html: str, caption, escape) -> str:
    if not caption:
        return img_html
    # <p>, not <div> — this renderer's other block types (see _render_para
    # etc.) all use <p> for block-level content; wx's simplified HTML
    # engine isn't a full CSS box model, so sticking to tags already
    # proven to lay out as a new block here matters.
    return f'<p>{img_html}</p><p style="font-size: 0.9em; color: #888;">{escape(str(caption))}</p>'


def svg_block_to_html(svg_dict, escape) -> str:
    """Render an {"svg": {...}} block's value to an HTML fragment.

    `escape` is the caller's own html-escaping function, used for the
    alt/caption text so this matches each renderer's existing escaping
    convention.
    """
    if not isinstance(svg_dict, dict):
        return ""
    body = svg_dict.get("body", "")
    if not isinstance(body, str) or not body.strip():
        return ""
    name = _register_svg(body.encode("utf-8"))
    if name is None:
        return ""
    alt = escape(svg_dict.get("alt", "") or "")
    img = f'<img src="memory:{name}" alt="{alt}">'
    return _wrap_with_caption(img, svg_dict.get("caption"), escape)


def image_block_to_html(image_dict, escape) -> str:
    """Render an {"image": {...}} block's value to an HTML fragment.

    `data` is base64-encoded raster bytes (e.g. PNG from a fixed-note's
    matplotlib output) — decoded directly into a wx.Image, no
    rasterization step needed (unlike svg).
    """
    if not isinstance(image_dict, dict):
        return ""
    data = image_dict.get("data", "")
    if not isinstance(data, str) or not data.strip():
        return ""
    try:
        raw_bytes = base64.b64decode(data)
    except Exception:
        return ""
    name = _register_raster(raw_bytes)
    if name is None:
        return ""
    alt = escape(image_dict.get("alt", "") or "")
    img = f'<img src="memory:{name}" alt="{alt}">'
    return _wrap_with_caption(img, image_dict.get("caption"), escape)
