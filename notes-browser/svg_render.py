"""
svg_render.py — renders {"svg": {...}} JSONHTL blocks for the wx desktop
browsers (notes_browser.py and notes_browser_runnable.py).

wx.html.HtmlWindow cannot render SVG at all, and (separately, per
sheet_ui.py) its data: URI support is unreliable past a few KB anyway. So
each svg block is rasterized to a PNG at render time and registered in
wx.MemoryFSHandler under a content-hash filename, then referenced via a
memory: URI <img> tag — same page-loading mechanism wx already uses for
any other image.

Registrations are keyed by a hash of the SVG source, not by note/block
position, so viewing the same diagram again (including re-navigating to
the same note) does not re-register or leak memory; only genuinely new
SVG content adds an entry. Entries are never removed, but this bounds
growth to the number of *distinct* diagrams seen in a session rather than
the number of times any note is viewed.

Rasterization uses cairosvg, not wx.svg (wxPython's bundled NanoSVG-based
renderer) — confirmed empirically that wx.svg silently drops <text>
elements entirely (0-length shape list for a rect+text SVG), which would
make it useless for actual diagrams. cairosvg produces a real PNG, fed
into wx.Image the same proven way as the matplotlib show(fig) fix.
"""
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
    _ensure_memory_fs()
    wx.MemoryFSHandler.AddFile(name, wx_image, wx.BITMAP_TYPE_PNG)
    _registered.add(name)
    return name


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
    caption = svg_dict.get("caption")
    if caption:
        # <p>, not <div> — this renderer's other block types (see
        # _render_para etc.) all use <p> for block-level content; wx's
        # simplified HTML engine isn't a full CSS box model, so sticking
        # to tags already proven to lay out as a new block here matters.
        return f'<p>{img}</p><p style="font-size: 0.9em; color: #888;">{escape(str(caption))}</p>'
    return img
