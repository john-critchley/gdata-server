"""
sheet_ui.py — Runnable Sheet UI classes for notes_browser_runnable.py

Classes:
  ProsePanel         — JSONHTL prose blocks rendered as wx.html.HtmlWindow
  SheetCellPanel     — One executable cell (code, inputs, run button, output)
  RunnableSheetPanel — Full sheet: toolbar + scroller of ProsePanel/SheetCellPanel
"""

import datetime
import base64
import io
import itertools
import json
import os
import re
import html as _html
import copy

import wx
import wx.html

from urllib.parse import unquote
from sheet_kernel import SheetKernel, detect_inputs

ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')

# ---------------------------------------------------------------------------
# Inline image rendering (show(fig) -> ["img", {"src": "data:..."}])
#
# wx.html.HtmlWindow's data: URI support is unreliable once the payload gets
# past a few KB (confirmed empirically: a solid-colour 1400x650 PNG — a
# realistic matplotlib figure size — renders as a broken-image icon via
# data:, but renders correctly via wx.MemoryFSHandler). The kernel
# (sheet_kernel.py) stays UI-agnostic and always emits a data: URI so it's
# testable without wx/a display; this module re-homes that payload into
# wx's in-memory filesystem right before rendering.
# ---------------------------------------------------------------------------

_memory_fs_ready = False
_memory_fs_counter = itertools.count()


def _ensure_memory_fs():
    global _memory_fs_ready
    if not _memory_fs_ready:
        wx.FileSystem.AddHandler(wx.MemoryFSHandler())
        _memory_fs_ready = True


def _register_data_uri_image(data_uri: str) -> str:
    """Decode a data:image/...;base64,... URI, register it with
    wx.MemoryFSHandler, and return the memory: filename (caller must
    RemoveFile it later to avoid leaking memory).
    """
    header, _, payload = data_uri.partition(",")
    raw = base64.b64decode(payload)
    _ensure_memory_fs()
    name = f"show_img_{next(_memory_fs_counter)}.png"
    wx_image = wx.Image(io.BytesIO(raw), wx.BITMAP_TYPE_PNG)
    wx.MemoryFSHandler.AddFile(name, wx_image, wx.BITMAP_TYPE_PNG)
    return name


def _rehome_data_uri_images(node, registered: list):
    """Recursively rewrite ["img", {"src": "data:image/...;base64,..."}]
    nodes in a JSONML tree to memory: URIs, appending each registered
    filename to `registered` so the caller can clean it up later.
    """
    if not isinstance(node, list) or not node:
        return node
    tag = node[0] if isinstance(node[0], str) else None
    if tag == "img" and len(node) > 1 and isinstance(node[1], dict):
        src = node[1].get("src", "")
        if src.startswith("data:image/"):
            name = _register_data_uri_image(src)
            registered.append(name)
            new_attrs = dict(node[1])
            new_attrs["src"] = f"memory:{name}"
            return [node[0], new_attrs] + list(node[2:])
        return node
    return [
        _rehome_data_uri_images(child, registered) if isinstance(child, list) else child
        for child in node
    ]


def _clean_output(text):
    return ANSI_RE.sub('', text) if text else ''


# ---------------------------------------------------------------------------
# ProsePanel

def jsonml_to_html(node) -> str:
    """Recursively convert a JSONML node to an HTML string.

    JSONML: ["tagname", {attrs}, child1, child2, ...]
    or a plain string (leaf).
    """
    if isinstance(node, str):
        return _html.escape(node)
    if not isinstance(node, list) or not node:
        return ""
    tag = str(node[0])
    attrs = node[1] if len(node) > 1 and isinstance(node[1], dict) else {}
    first_child = 2 if (len(node) > 1 and isinstance(node[1], dict)) else 1
    children = node[first_child:]
    attr_str = "".join(
        f' {_html.escape(k)}="{_html.escape(str(v))}"' for k, v in attrs.items()
    )
    inner = "".join(jsonml_to_html(child) for child in children)
    style_block = ""
    if tag == "table":
        style_block = (
            "<style>"
            "table{border-collapse:collapse;margin:6px 0;}"
            "th,td{border:1px solid #bbb;padding:3px 8px;font-family:monospace;font-size:10pt;}"
            "caption{font-size:9pt;color:#555;margin-bottom:3px;}"
            "th{background:#2a2a4a;color:#fff;}"
            "</style>"
        )
    return f"{style_block}<{tag}{attr_str}>{inner}</{tag}>"


# ---------------------------------------------------------------------------

class _ProseHtmlWindow(wx.html.HtmlWindow):
    def __init__(self, parent, browser):
        wx.html.HtmlWindow.__init__(self, parent)
        self._browser = browser

    def OnLinkClicked(self, link):
        if getattr(self._browser, '_nav_locked', False):
            return
        href = link.GetHref()
        if href.startswith('navigate://'):
            self._browser._navigate_to(unquote(href[len('navigate://'):]))
        elif href.startswith('http://') or href.startswith('https://'):
            import webbrowser
            webbrowser.open(href)
        else:
            self._browser._navigate_to(href)


class ProsePanel(wx.Panel):
    """Renders a run of non-executable JSONHTL content blocks as HTML."""

    def __init__(self, parent, browser, blocks, renderer):
        wx.Panel.__init__(self, parent)
        self._browser = browser
        self._last_width = -1

        html_body = renderer._render_jsonhtl_blocks(blocks)
        html = f"<html><body style='margin:8px'>{html_body}</body></html>"

        self.htmlwin = _ProseHtmlWindow(self, browser)
        self.htmlwin.SetPage(html)
        # Don't compute height at construction — width is unknown (often 0).
        # EVT_SIZE fires once the panel has a real width.
        self.htmlwin.SetMinSize((-1, 30))
        self.SetMinSize((-1, 30))

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.htmlwin, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self.Bind(wx.EVT_SIZE, self._on_size)

    def _on_size(self, event):
        event.Skip()
        if getattr(self, '_in_size_handler', False):
            return
        w = self.GetClientSize().width
        if w < 5 or w == self._last_width:
            return
        self._last_width = w
        self._in_size_handler = True
        try:
            # Force htmlwin to render at the new width so ir.GetHeight() is accurate
            cur_h = max(self.htmlwin.GetSize().height, 30)
            self.htmlwin.SetSize(wx.Size(w, cur_h))
            ir = self.htmlwin.GetInternalRepresentation()
            h = (ir.GetHeight() + 10) if ir else 30
            self.htmlwin.SetMinSize((-1, h))
            self.SetMinSize((-1, h))
            wx.CallAfter(self._refit_scroller)
        finally:
            self._in_size_handler = False

    def _refit_scroller(self):
        scroller = self.GetParent()
        if scroller and hasattr(scroller, 'FitInside'):
            scroller.Layout()
            scroller.FitInside()


class _PlotPanel(wx.Panel):
    """Simple line-plot renderer for plot() output specs."""

    def __init__(self, parent, spec):
        wx.Panel.__init__(self, parent, style=wx.BORDER_SIMPLE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.spec = spec or {}
        self.series = []
        for s in self.spec.get('series', []) or []:
            xs = list(s.get('x', []) or [])
            ys = list(s.get('y', []) or [])
            n = min(len(xs), len(ys))
            if n > 0:
                self.series.append((xs[:n], ys[:n]))
        self.title = str(self.spec.get('title', 'Plot'))
        self.x_label = str(self.spec.get('x_label', 'x'))
        self.y_label = str(self.spec.get('y_label', 'y'))
        self.SetMinSize((-1, 250))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def _on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(255, 255, 255)))
        dc.Clear()
        if w < 20 or h < 20:
            return

        left, top, right, bottom = 56, 28, 16, 34
        px0 = left
        py0 = h - bottom
        px1 = max(px0 + 10, w - right)
        py1 = top

        dc.SetPen(wx.Pen(wx.Colour(220, 220, 220), 1))
        for i in range(1, 5):
            y = py1 + int(i * (py0 - py1) / 5)
            dc.DrawLine(px0, y, px1, y)

        dc.SetPen(wx.Pen(wx.Colour(60, 60, 60), 1))
        dc.DrawLine(px0, py0, px1, py0)
        dc.DrawLine(px0, py1, px0, py0)

        if not self.series:
            dc.DrawText("(empty plot)", px0 + 8, py1 + 8)
            return

        all_x = []
        all_y = []
        for xs, ys in self.series:
            all_x.extend(xs)
            all_y.extend(ys)
        xmin, xmax = min(all_x), max(all_x)
        ymin, ymax = min(all_y), max(all_y)

        if xmin == xmax:
            xmax = xmin + 1.0
        if ymin == ymax:
            ymax = ymin + 1.0

        def map_x(xv):
            return px0 + int((xv - xmin) / (xmax - xmin) * (px1 - px0))

        def map_y(yv):
            return py0 - int((yv - ymin) / (ymax - ymin) * (py0 - py1))

        colours = [
            wx.Colour(31, 119, 180),
            wx.Colour(214, 39, 40),
            wx.Colour(44, 160, 44),
            wx.Colour(148, 103, 189),
        ]
        for i, (xs, ys) in enumerate(self.series):
            pts = [wx.Point(map_x(xv), map_y(yv)) for xv, yv in zip(xs, ys)]
            if len(pts) >= 2:
                dc.SetPen(wx.Pen(colours[i % len(colours)], 2))
                dc.DrawLines(pts)
            elif len(pts) == 1:
                dc.SetBrush(wx.Brush(colours[i % len(colours)]))
                dc.SetPen(wx.Pen(colours[i % len(colours)], 1))
                dc.DrawCircle(pts[0].x, pts[0].y, 2)

        # --- Labels and ticks ---
        dc.SetTextForeground(wx.Colour(30, 30, 30))

        # Title (centred at top)
        title_font = wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        dc.SetFont(title_font)
        tw, th = dc.GetTextExtent(self.title)
        dc.DrawText(self.title, px0 + (px1 - px0 - tw) // 2, 4)

        label_font = wx.Font(8, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        dc.SetFont(label_font)

        # X-axis label (centred below axis)
        xw, xh = dc.GetTextExtent(self.x_label)
        dc.DrawText(self.x_label, px0 + (px1 - px0 - xw) // 2, py0 + xh + 2)

        # Y-axis label (rotated, left of axis) — wx.DC.DrawRotatedText
        yw, yh = dc.GetTextExtent(self.y_label)
        dc.DrawRotatedText(self.y_label, max(0, px0 - yh - 2), py1 + (py0 - py1 + yw) // 2, 90)

        # Y-axis tick values (min and max)
        tick_font = wx.Font(7, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        dc.SetFont(tick_font)
        for val, py in [(ymax, py1), (ymin, py0)]:
            s = f'{val:.3g}'
            sw, sh = dc.GetTextExtent(s)
            dc.DrawText(s, px0 - sw - 3, py - sh // 2)

        # X-axis tick values (min and max)
        for val, px in [(xmin, px0), (xmax, px1)]:
            s = f'{val:.3g}'
            sw, sh = dc.GetTextExtent(s)
            dc.DrawText(s, px - sw // 2, py0 + 2)


# ---------------------------------------------------------------------------
# SheetCellPanel
# ---------------------------------------------------------------------------

class SheetCellPanel(wx.Panel):
    """One executable code cell: header, code display, inputs, output."""

    def __init__(self, parent, sheet_panel, cell_id, cell_spec):
        wx.Panel.__init__(self, parent, style=wx.BORDER_SIMPLE)
        self.sheet_panel = sheet_panel
        self.cell_id = cell_id
        self.cell_spec = cell_spec
        self.input_ctrls = []
        self._last_show_items = []
        self._last_output = ''
        self._memory_fs_names = []
        self.SetBackgroundColour(wx.Colour(248, 249, 252))

        mono = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        ui = wx.Font(9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        ui_bold = wx.Font(9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- Header ---
        header = wx.Panel(self)
        hs = wx.BoxSizer(wx.HORIZONTAL)
        name = cell_spec.get('name') or cell_id
        self.name_label = wx.StaticText(header, label=name)
        self.lang_label = wx.StaticText(header, label=cell_spec.get('lang', ''))
        self.status_label = wx.StaticText(header, label='')
        self.run_button = wx.Button(header, label='Run')
        header.SetBackgroundColour(wx.Colour(236, 240, 247))
        self.name_label.SetFont(ui_bold)
        self.name_label.SetForegroundColour(wx.Colour(30, 40, 70))
        self.lang_label.SetFont(ui)
        self.lang_label.SetForegroundColour(wx.Colour(55, 82, 140))
        self.status_label.SetFont(ui_bold)
        self.status_label.SetForegroundColour(wx.Colour(90, 90, 90))
        hs.Add(self.name_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        hs.Add(self.lang_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        hs.AddStretchSpacer(1)
        hs.Add(self.status_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        hs.AddStretchSpacer(1)
        hs.Add(self.run_button, 0, wx.ALL, 4)
        header.SetSizer(hs)
        outer.Add(header, 0, wx.EXPAND)

        # --- Code ---
        body = cell_spec.get('body', '')
        self.code_ctrl = wx.TextCtrl(
            self, value=body,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SIMPLE,
        )
        self.code_ctrl.SetFont(mono)
        self.code_ctrl.SetBackgroundColour(wx.Colour(30, 33, 40))
        self.code_ctrl.SetForegroundColour(wx.Colour(224, 228, 236))
        n_lines = max(1, self.code_ctrl.GetNumberOfLines())
        line_h = self.code_ctrl.GetCharHeight()
        self.code_ctrl.SetMinSize((-1, min(n_lines * line_h + 8, 400)))
        outer.Add(self.code_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # --- Inputs ---
        self.input_panel = wx.Panel(self)
        input_sizer = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        input_sizer.AddGrowableCol(1, 1)
        for i, prompt in enumerate(cell_spec.get('input_prompts', [])):
            label = prompt or f'Input {i + 1}'
            lbl = wx.StaticText(self.input_panel, label=label)
            ctrl = wx.TextCtrl(self.input_panel, value='')
            ctrl.field_id = f'{cell_id}/{i}'
            input_sizer.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)
            input_sizer.Add(ctrl, 1, wx.EXPAND)
            self.input_ctrls.append(ctrl)
        self.input_panel.SetSizer(input_sizer)
        if not cell_spec.get('input_prompts'):
            self.input_panel.Hide()
        outer.Add(self.input_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # --- Output host (stdout ctrl + per-show() sub-areas) ---
        self.output_host = wx.Panel(self)
        self.output_host.SetSizer(wx.BoxSizer(wx.VERTICAL))
        self.output_host.Hide()
        self.stdout_ctrl = None  # wx.TextCtrl created on demand in set_output()
        outer.Add(self.output_host, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.SetSizer(outer)
        self.run_button.Bind(wx.EVT_BUTTON, lambda e: self.sheet_panel.run_cell(self.cell_id))

    def set_running(self, running):
        self.run_button.Enable(not running)
        if running:
            self.status_label.SetLabel('Running…')

    def set_status(self, text):
        self.status_label.SetLabel(text)

    def _append_show_item(self, item) -> None:
        """Add one show()-produced item as a new child widget in output_host."""
        mono = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        sizer = self.output_host.GetSizer()
        if isinstance(item, dict) and item.get('kind') == 'plot':
            plot_panel = _PlotPanel(self.output_host, item)
            sizer.Add(plot_panel, 0, wx.EXPAND | wx.TOP, 4)
            self.sheet_panel._bind_mousewheel_chain(plot_panel)
        elif isinstance(item, dict) and item.get('kind') == 'image':
            if item.get('format') != 'png':
                raise ValueError(f"Unsupported image format: {item.get('format')!r}")
            png_bytes = base64.b64decode(item.get('data', ''))
            image = wx.Image(io.BytesIO(png_bytes), wx.BITMAP_TYPE_PNG)
            bitmap = wx.Bitmap(image)
            image_ctrl = wx.StaticBitmap(self.output_host, bitmap=bitmap)
            image_ctrl.SetMinSize((image.GetWidth(), image.GetHeight()))
            sizer.Add(image_ctrl, 0, wx.TOP, 4)
            self.sheet_panel._bind_mousewheel_chain(image_ctrl)
        elif isinstance(item, dict) and item.get('kind') == 'html':
            htmlwin = wx.html.HtmlWindow(self.output_host, style=wx.BORDER_SIMPLE)
            htmlwin.SetMinSize((-1, 300))
            htmlwin.SetPage(item.get('content', ''))
            sizer.Add(htmlwin, 0, wx.EXPAND | wx.TOP, 4)
            self.sheet_panel._bind_mousewheel_chain(htmlwin)
            def _fit_html(hw=htmlwin):
                ir = hw.GetInternalRepresentation()
                if ir:
                    hw.SetMinSize((-1, max(80, ir.GetHeight() + 20)))
                hw.GetParent().Layout()
                cell = hw.GetParent().GetParent()
                if cell:
                    cell.Layout()
                    scroller = cell.GetParent()
                    if scroller and hasattr(scroller, 'FitInside'):
                        scroller.FitInside()
            wx.CallAfter(_fit_html)
        elif isinstance(item, list):
            item = _rehome_data_uri_images(item, self._memory_fs_names)
            html_str = jsonml_to_html(item)
            htmlwin = wx.html.HtmlWindow(self.output_host, style=wx.BORDER_SIMPLE)
            htmlwin.SetMinSize((-1, 200))
            htmlwin.SetPage(html_str)
            sizer.Add(htmlwin, 0, wx.EXPAND | wx.TOP, 4)
            self.sheet_panel._bind_mousewheel_chain(htmlwin)
            def _fit(hw=htmlwin):
                ir = hw.GetInternalRepresentation()
                if ir:
                    hw.SetMinSize((-1, max(80, ir.GetHeight() + 20)))
                hw.GetParent().Layout()
                cell = hw.GetParent().GetParent()
                if cell:
                    cell.Layout()
                    scroller = cell.GetParent()
                    if scroller and hasattr(scroller, 'FitInside'):
                        scroller.FitInside()
            wx.CallAfter(_fit)
        else:
            txt = str(item)
            ctrl = wx.TextCtrl(
                self.output_host, value=txt,
                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SIMPLE,
            )
            ctrl.SetFont(mono)
            ctrl.SetBackgroundColour(wx.Colour(240, 248, 255))
            ctrl.SetForegroundColour(wx.Colour(30, 30, 80))
            n_lines = max(1, txt.count('\n') + 1)
            line_h = ctrl.GetCharHeight()
            ctrl.SetMinSize((-1, min(n_lines * line_h + 6, 400)))
            sizer.Add(ctrl, 0, wx.EXPAND | wx.TOP, 4)
            self.sheet_panel._bind_mousewheel_chain(ctrl)
        self.output_host.Layout()
        self._refit_sheet_scroller()

    def _refit_sheet_scroller(self):
        """Walk up to the ScrolledWindow and update its virtual size."""
        win = self.GetParent()
        while win is not None:
            if isinstance(win, wx.ScrolledWindow):
                win.Layout()
                win.FitInside()
                return
            win = win.GetParent()

    def get_stdout_text(self) -> str:
        """Return current stdout/stderr text (empty string if no output shown)."""
        if self.stdout_ctrl and self.stdout_ctrl.IsShown():
            return self.stdout_ctrl.GetValue()
        return ''

    def get_show_items(self):
        return copy.deepcopy(self._last_show_items)

    def set_output(self, output, error=None, ok=True, show_items=()):
        self.clear_output(reset_state=False)
        parts = [_clean_output(s) for s in [output, error] if s]
        self._last_output = '\n'.join(parts)
        self._last_show_items = copy.deepcopy(list(show_items or ()))
        sizer = self.output_host.GetSizer()
        mono = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        has_stdout = bool(output or error)
        if has_stdout:
            text = self._last_output
            ctrl = wx.TextCtrl(
                self.output_host, value=text,
                style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SIMPLE,
            )
            ctrl.SetFont(mono)
            if not ok:
                ctrl.SetBackgroundColour(wx.Colour(80, 30, 30))
                ctrl.SetForegroundColour(wx.Colour(255, 180, 180))
            else:
                ctrl.SetBackgroundColour(wx.Colour(45, 45, 45))
                ctrl.SetForegroundColour(wx.Colour(220, 220, 220))
            n_lines = max(1, text.count('\n') + 1)
            line_h = ctrl.GetCharHeight()
            ctrl.SetMinSize((-1, min(n_lines * line_h + 8, 400)))
            sizer.Add(ctrl, 0, wx.EXPAND)
            self.stdout_ctrl = ctrl
            self.sheet_panel._bind_mousewheel_chain(ctrl)
        for item in (show_items or ()):
            self._append_show_item(item)
        if has_stdout or show_items:
            self.output_host.Show()
        self.Layout()
        p = self.GetParent()
        if p:
            p.Layout()
        self._refit_sheet_scroller()

    def clear_output(self, reset_state=True):
        sizer = self.output_host.GetSizer()
        for child in list(self.output_host.GetChildren()):
            child.Destroy()
        sizer.Clear()
        self.stdout_ctrl = None
        for name in self._memory_fs_names:
            wx.MemoryFSHandler.RemoveFile(name)
        self._memory_fs_names = []
        if reset_state:
            self._last_output = ''
            self._last_show_items = []
        self.output_host.Hide()
        self.Layout()
        p = self.GetParent()
        if p:
            p.Layout()

    def get_input_values(self):
        return [ctrl.GetValue() for ctrl in self.input_ctrls]


# ---------------------------------------------------------------------------
# RunnableSheetPanel
# ---------------------------------------------------------------------------

class RunnableSheetPanel(wx.Panel):
    """Full runnable sheet: toolbar + scrolled content of ProsePanel/SheetCellPanel."""

    def __init__(self, parent, browser, key, data):
        wx.Panel.__init__(self, parent)
        self.browser = browser
        self.key = key
        self.data = data
        self.cells = {}
        self.exec_cell_order = []
        self.cell_panels = {}
        self.is_running = False
        self._busy_visual = False
        self.kernel = SheetKernel()
        self.SetBackgroundColour(wx.Colour(242, 244, 249))

        outer = wx.BoxSizer(wx.VERTICAL)

        # --- Toolbar ---
        tb = wx.Panel(self)
        tbs = wx.BoxSizer(wx.HORIZONTAL)
        self.run_all_btn  = wx.Button(tb, label='Run All')
        self.save_btn     = wx.Button(tb, label='Save')
        self.fix_btn      = wx.Button(tb, label='Fix as New Note…')
        self.clear_btn    = wx.Button(tb, label='Clear Outputs')
        self.restart_btn  = wx.Button(tb, label='Restart Kernel')
        self.export_btn   = wx.Button(tb, label='Save Data…')
        self.import_btn   = wx.Button(tb, label='Load Data…')
        self.status_label = wx.StaticText(tb, label='')
        self.status_label.SetForegroundColour(wx.Colour(65, 65, 65))
        for btn in (self.run_all_btn, self.save_btn, self.fix_btn, self.clear_btn,
                    self.restart_btn, self.export_btn, self.import_btn):
            tbs.Add(btn, 0, wx.ALL, 4)
        tbs.AddStretchSpacer(1)
        tbs.Add(self.status_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        tb.SetSizer(tbs)
        outer.Add(tb, 0, wx.EXPAND)

        self.run_all_btn.Bind(wx.EVT_BUTTON,  lambda e: self._on_run_all())
        self.save_btn.Bind(wx.EVT_BUTTON,     lambda e: self._on_save_page())
        self.fix_btn.Bind(wx.EVT_BUTTON,      lambda e: self._on_fix_as_new_note())
        self.clear_btn.Bind(wx.EVT_BUTTON,    lambda e: self._on_clear_outputs())
        self.restart_btn.Bind(wx.EVT_BUTTON,  lambda e: self._on_restart_kernel())
        self.export_btn.Bind(wx.EVT_BUTTON,   lambda e: self._on_export())
        self.import_btn.Bind(wx.EVT_BUTTON,   lambda e: self._on_import())

        # --- Scroller ---
        self.scroller = wx.ScrolledWindow(self, style=wx.VSCROLL)
        self.scroller.SetScrollRate(10, 10)
        self.blocks_sizer = wx.BoxSizer(wx.VERTICAL)
        self.scroller.SetSizer(self.blocks_sizer)
        outer.Add(self.scroller, 1, wx.EXPAND)
        self.SetSizer(outer)

        self._build_content(data, browser)
        self.scroller.FitInside()
        self._bind_mousewheel_chain(self.scroller)

    def _build_content(self, data, browser):
        content = data.get('content', [])
        renderer = browser.renderer
        seen_names = {}  # name -> count

        # First pass: count names to detect duplicates
        for block in content:
            if isinstance(block, dict) and 'codeblock' in block:
                cb = block['codeblock']
                if isinstance(cb, dict) and cb.get('exec') is True:
                    n = cb.get('name', '')
                    if n:
                        seen_names[n] = seen_names.get(n, 0) + 1

        prose_run = []
        cb_idx = 0
        for block in content:
            is_exec = (
                isinstance(block, dict)
                and 'codeblock' in block
                and isinstance(block['codeblock'], dict)
                and block['codeblock'].get('exec') is True
            )
            if isinstance(block, dict) and 'codeblock' in block:
                cb_idx += 1

            if not is_exec:
                prose_run.append(block)
                continue

            if prose_run:
                pp = ProsePanel(self.scroller, browser, prose_run, renderer)
                self.blocks_sizer.Add(pp, 0, wx.EXPAND | wx.BOTTOM, 2)
                prose_run = []

            cb = block['codeblock']
            name = cb.get('name', '')
            # Canonical key: name if unique, else codeblock index string
            canon = name if (name and seen_names.get(name, 0) == 1) else str(cb_idx - 1)

            body = cb.get('body', '')
            prompts = detect_inputs(body)
            cell_spec = {
                'key': canon,
                'name': name,
                'lang': cb.get('lang', ''),
                'body': body,
                'exec': True,
                'input_prompts': prompts,
            }
            self.cells[canon] = cell_spec
            self.exec_cell_order.append(canon)

            cp = SheetCellPanel(self.scroller, self, canon, cell_spec)
            # Pre-populate output from document if present
            doc_output = cb.get('output', '')
            if doc_output:
                cp.set_output(doc_output, ok=True)
                if cp.stdout_ctrl:
                    cp.stdout_ctrl.SetBackgroundColour(wx.Colour(240, 240, 200))
                    cp.stdout_ctrl.SetForegroundColour(wx.Colour(60, 60, 60))
                    cp.stdout_ctrl.Refresh()

            self.cell_panels[canon] = cp
            self.blocks_sizer.Add(cp, 0, wx.EXPAND | wx.ALL, 4)
            self._bind_mousewheel_chain(cp)

        if prose_run:
            pp = ProsePanel(self.scroller, browser, prose_run, renderer)
            self.blocks_sizer.Add(pp, 0, wx.EXPAND | wx.BOTTOM, 2)
            self._bind_mousewheel_chain(pp)

    def _bind_mousewheel_chain(self, win):
        if not isinstance(win, wx.Window):
            return
        try:
            win.Unbind(wx.EVT_MOUSEWHEEL, handler=self._on_mousewheel_chain)
        except Exception:
            pass
        win.Bind(wx.EVT_MOUSEWHEEL, self._on_mousewheel_chain)
        for child in win.GetChildren():
            self._bind_mousewheel_chain(child)

    def _window_can_scroll(self, win, rotation):
        try:
            vrange = int(win.GetScrollRange(wx.VERTICAL))
            pos = int(win.GetScrollPos(wx.VERTICAL))
            thumb = int(win.GetScrollThumb(wx.VERTICAL))
        except Exception:
            return False

        if vrange <= 0:
            return False
        max_pos = max(0, vrange - max(0, thumb))
        if rotation > 0:
            return pos > 0
        return pos < max_pos

    def _scroll_main_for_wheel(self, rotation, event):
        wheel_delta = max(1, int(event.GetWheelDelta()) if hasattr(event, 'GetWheelDelta') else 120)
        lines_per = max(1, int(event.GetLinesPerAction()) if hasattr(event, 'GetLinesPerAction') else 3)
        ticks = max(1, abs(int(rotation)) // wheel_delta)
        lines = ticks * lines_per
        if rotation > 0:
            self.scroller.ScrollLines(-lines)
        else:
            self.scroller.ScrollLines(lines)

    def _on_mousewheel_chain(self, event):
        if self.is_running:
            self._set_busy_visual(True)
        obj = event.GetEventObject()
        rotation = event.GetWheelRotation()
        if rotation == 0:
            event.Skip()
            return

        # If an inner scrollable control can still scroll in this direction,
        # let it consume wheel first.
        win = obj if isinstance(obj, wx.Window) else None
        while win and win is not self and win is not self.scroller:
            if self._window_can_scroll(win, rotation):
                event.Skip()
                return
            win = win.GetParent()

        # Inner control is at limit (or not scrollable): continue with sheet scroll.
        self._scroll_main_for_wheel(rotation, event)

    # --- Execution ---

    def _set_busy_visual(self, busy):
        """Show/hide wait cursor while the sheet is executing."""
        if busy:
            if self._busy_visual:
                return
            self._busy_visual = True
            wait = wx.Cursor(wx.CURSOR_WAIT)
            targets = [self, self.scroller, self.browser]
            content_host = getattr(self.browser, 'content_host', None)
            if content_host is not None:
                targets.append(content_host)
            for t in targets:
                try:
                    t.SetCursor(wait)
                except Exception:
                    pass
            wx.YieldIfNeeded()
            return

        if not self._busy_visual:
            return
        self._busy_visual = False
        targets = [self, self.scroller, self.browser]
        content_host = getattr(self.browser, 'content_host', None)
        if content_host is not None:
            targets.append(content_host)
        for t in targets:
            try:
                t.SetCursor(wx.NullCursor)
            except Exception:
                pass
        wx.YieldIfNeeded()

    def scroll_cell_into_view(self, cell_id):
        """Scroll the sheet so the named cell panel is visible."""
        panel = self.cell_panels.get(cell_id)
        if panel is None:
            return
        # Get panel position relative to the scroller's virtual canvas
        pos = panel.GetPosition()
        # Convert from panel coords to scroller virtual coords
        scr_pos = panel.GetParent().ScreenToClient(panel.ClientToScreen(wx.Point(0, 0)))
        # GetScrollPos returns scroll units; GetScrollPixelsPerUnit gives pixels per unit
        ppux, ppuy = self.scroller.GetScrollPixelsPerUnit()
        # Current scroll offset in pixels
        sx = self.scroller.GetScrollPos(wx.HORIZONTAL) * (ppux or 1)
        sy = self.scroller.GetScrollPos(wx.VERTICAL) * (ppuy or 1)
        # Panel top/bottom in virtual coordinates
        virt_top = sy + scr_pos.y
        virt_bot = virt_top + panel.GetSize().height
        view_h = self.scroller.GetClientSize().height
        # Scroll so panel top is near top of view (with small margin)
        target_y = max(0, virt_top - 20)
        self.scroller.Scroll(0, target_y // (ppuy or 1))

    def _scroll_cell_into_view_after_layout(self, cell_id):
        """Scroll after output/layout changes have had a chance to settle."""
        def _scroll():
            self.Layout()
            self.scroller.Layout()
            self.scroller.FitInside()
            self.scroll_cell_into_view(cell_id)
        wx.CallAfter(_scroll)

    def run_cell(self, cell_id, scroll=True):
        if self.is_running:
            return
        self.is_running = True
        self._set_busy_visual(True)
        self._set_toolbar_enabled(False)
        self.browser._set_nav_enabled(False)
        panel = self.cell_panels[cell_id]
        cell = self.cells[cell_id]
        if scroll:
            self.scroll_cell_into_view(cell_id)
        panel.set_running(True)
        wx.YieldIfNeeded()
        try:
            output, error, ok, show_items = self.kernel.run_cell(cell_id, cell['body'], panel.get_input_values())
            panel.set_output(output, error, ok, show_items)
            panel.set_status('Done' if ok else 'Error')
            if scroll:
                self._scroll_cell_into_view_after_layout(cell_id)
        finally:
            panel.set_running(False)
            self.is_running = False
            self._set_busy_visual(False)
            self._set_toolbar_enabled(True)
            self.browser._set_nav_enabled(True)

    def _on_run_all(self, scroll=True):
        if self.is_running:
            return
        self.is_running = True
        self._set_busy_visual(True)
        self._set_toolbar_enabled(False)
        self.browser._set_nav_enabled(False)
        self.status_label.SetLabel('Running…')
        all_ok = True
        try:
            for cell_id in self.exec_cell_order:
                cell = self.cells[cell_id]
                panel = self.cell_panels[cell_id]
                if scroll:
                    self.scroll_cell_into_view(cell_id)
                panel.set_running(True)
                panel.set_status('Running…')
                wx.YieldIfNeeded()
                output, error, ok, show_items = self.kernel.run_cell(cell_id, cell['body'], panel.get_input_values())
                panel.set_output(output, error, ok, show_items)
                panel.set_status('Done' if ok else 'Error')
                if scroll:
                    self._scroll_cell_into_view_after_layout(cell_id)
                panel.set_running(False)
                wx.YieldIfNeeded()
                if not ok:
                    all_ok = False
                    break
        finally:
            self.is_running = False
            self._set_busy_visual(False)
            self._set_toolbar_enabled(True)
            self.browser._set_nav_enabled(True)
            self.status_label.SetLabel('Done' if all_ok else 'Stopped on error')

    def _on_clear_outputs(self):
        if self.is_running:
            return
        for panel in self.cell_panels.values():
            panel.clear_output()
            panel.set_status('')
        self.status_label.SetLabel('Outputs cleared (kernel kept).')

    def _on_restart_kernel(self):
        if self.is_running:
            return
        self.kernel.reset()
        for panel in self.cell_panels.values():
            if panel.output_host.IsShown() and panel.stdout_ctrl:
                panel.stdout_ctrl.SetBackgroundColour(wx.Colour(240, 240, 200))
                panel.stdout_ctrl.SetForegroundColour(wx.Colour(60, 60, 60))
                panel.stdout_ctrl.Refresh()
            panel.set_status('')
        self.status_label.SetLabel('Kernel restarted.')

    def _on_export(self):
        if self.is_running:
            return
        dlg = wx.FileDialog(
            self, 'Save Sheet Data',
            wildcard='Sheet data (*.sheetdata.json)|*.sheetdata.json',
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            self.export_data(path)
        dlg.Destroy()

    def _on_import(self):
        if self.is_running:
            return
        dlg = wx.FileDialog(
            self, 'Open Sheet Data',
            wildcard='Sheet data (*.sheetdata.json)|*.sheetdata.json|Sheet state (*.sheetstate.json)|*.sheetstate.json',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            try:
                summary = self.import_data(path)
                wx.MessageBox(
                    f'Updated cells: {summary["updated"]}\n'
                    f'Missing cells left unchanged: {summary["missing"]}\n'
                    f'Ignored unknown cells: {summary["superfluous"]}',
                    'Import Complete', wx.ICON_INFORMATION,
                )
            except Exception as e:
                wx.MessageBox(f'Failed to import: {e}', 'Import Error', wx.ICON_ERROR)
        dlg.Destroy()

    def _build_clean_document(self):
        doc = copy.deepcopy(self.data)
        if not isinstance(doc, dict):
            return doc
        content = doc.get('content', [])
        if not isinstance(content, list):
            return doc
        for block in content:
            if not isinstance(block, dict) or 'codeblock' not in block:
                continue
            cb = block['codeblock']
            if not isinstance(cb, dict):
                continue
            for k in ('output', 'show_items', 'inputs', 'state'):
                cb.pop(k, None)
        return doc

    def save_page(self):
        doc = self._build_clean_document()
        self.browser.data_source.write(self.key, doc)
        self.data = doc
        self.status_label.SetLabel('Saved page (without runtime data).')
        return {'key': self.key}

    def _on_save_page(self):
        if self.is_running:
            return
        self.save_page()

    # --- Fix as New Note -----------------------------------------------
    # "Fixing" a runnable note: save the *current* outputs (a matplotlib
    # plot, a table, printed text) as a new, ordinary non-runnable note,
    # so it can be read back later without ever re-running the code (which
    # may pull live/time-sensitive data — the whole point is a snapshot).
    # See notes-browser/fixed-notes and JSONHTL_SPEC ("image", "details").

    def _suggest_fixed_name(self):
        """Default new-note name: original key + when the data was last
        actually pulled (last_run_at), not when Fix is clicked — those can
        be hours apart (e.g. fixing this morning's commute plot tonight).
        """
        infos = self.kernel.list_cells()
        times = [i.last_run_at for i in infos if i.last_run_at]
        when = max(times) if times else datetime.datetime.now()
        return f"{self.key}-{when.strftime('%Y-%m-%d-%H%M')}"

    def _show_item_to_block(self, item):
        """Convert one show()-produced item into a persistent JSONHTL
        block, or None if there's no static representation for it yet.
        """
        if isinstance(item, str):
            return {'para': [item]}
        if isinstance(item, dict) and item.get('kind') == 'image':
            # What show(fig) actually produces (ImageOutput.__show__() in
            # sheet_kernel.py) — the live case, e.g. a speed plot.
            fmt = item.get('format', 'png') or 'png'
            data = item.get('data', '')
            if not data:
                return None
            return {'image': {'format': fmt, 'data': data}}
        if isinstance(item, list) and item and item[0] == 'img':
            # A data: URI JSONML img node — not currently produced by
            # anything in this codebase (show(fig) uses the dict kind
            # above), but the shape is a valid, documented alternative
            # (see JSONHTL_SPEC), so still worth converting if seen.
            attrs = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            src = attrs.get('src', '')
            if not src.startswith('data:image/'):
                return None
            header, _, payload = src.partition(',')
            fmt = 'png'
            if '/' in header:
                fmt = header.split('/', 1)[1].split(';', 1)[0] or 'png'
            return {'image': {'format': fmt, 'data': payload}}
        if isinstance(item, list) and item and item[0] == 'table':
            return self._jsonml_table_to_block(item)
        # dict kinds ('plot', 'html') and anything else: no persistent
        # block type for these yet — see notes-browser/todo.
        return None

    def _jsonml_table_to_block(self, node):
        """Best-effort conversion of the ndarray/DataFrame JSONML table
        shape (_make_jsonml_for_ndarray/_make_jsonml_for_dataframe in
        sheet_kernel.py) into a {"table": {...}} block. Handles a leading
        header row of <th> cells if present (the DataFrame case); a plain
        grid of <td> rows otherwise (the ndarray case). Drops any
        <caption> child — no equivalent field on the table block type.
        """
        rows_ml = [c for c in node[2:] if isinstance(c, list) and c and c[0] == 'tr']
        if not rows_ml:
            return None

        def cell_text(cell):
            return str(cell[2]) if len(cell) > 2 else ''

        first_cells = [c for c in rows_ml[0][2:] if isinstance(c, list)]
        is_header = bool(first_cells) and all(c and c[0] == 'th' for c in first_cells)
        columns = [cell_text(c) for c in first_cells] if is_header else []
        data_rows_ml = rows_ml[1:] if is_header else rows_ml
        rows = []
        for r in data_rows_ml:
            cells = [c for c in r[2:] if isinstance(c, list)]
            rows.append([cell_text(c) for c in cells])
        return {'table': {'columns': columns, 'rows': rows}}

    def _build_fixed_document(self):
        """Walk the current document, replacing each executed cell with a
        collapsed <details> section holding its code, followed by its
        outputs converted to persistent blocks. Prose blocks pass through
        unchanged. Result has no "runnable" key at all.
        """
        doc = copy.deepcopy(self.data)
        if not isinstance(doc, dict):
            doc = {}
        doc.pop('runnable', None)
        content = doc.get('content', [])
        if not isinstance(content, list):
            content = []

        seen_names = {}
        for block in content:
            if isinstance(block, dict) and 'codeblock' in block:
                cb = block['codeblock']
                if isinstance(cb, dict) and cb.get('exec') is True:
                    n = cb.get('name', '')
                    if n:
                        seen_names[n] = seen_names.get(n, 0) + 1

        new_content = []
        cb_idx = 0
        for block in content:
            is_exec = (
                isinstance(block, dict) and 'codeblock' in block
                and isinstance(block['codeblock'], dict)
                and block['codeblock'].get('exec') is True
            )
            if isinstance(block, dict) and 'codeblock' in block:
                cb_idx += 1

            if not is_exec:
                new_content.append(block)
                continue

            cb = block['codeblock']
            name = cb.get('name', '')
            canon = name if (name and seen_names.get(name, 0) == 1) else str(cb_idx - 1)
            panel = self.cell_panels.get(canon)

            code_block = {'codeblock': {'lang': cb.get('lang', ''), 'body': cb.get('body', '')}}
            if panel is None:
                new_content.append(code_block)
                continue

            new_content.append({
                'details': {'summary': f'Cell: {name or canon} (code)', 'content': [code_block]},
            })

            stdout_text = panel.get_stdout_text()
            if stdout_text:
                new_content.append({'codeblock': {'lang': 'text', 'body': stdout_text}})

            for item in panel.get_show_items():
                block_out = self._show_item_to_block(item)
                if block_out is not None:
                    new_content.append(block_out)

        doc['content'] = new_content
        return doc

    def fix_as_new_note(self, new_key=None):
        """Build and save the fixed document under new_key (default: the
        suggested name). Shared by the toolbar button and the
        sheet.fix_as_new_note control-API method.
        """
        new_key = (new_key or self._suggest_fixed_name()).strip()
        if not new_key:
            raise ValueError("new_key must not be empty")
        doc = self._build_fixed_document()
        self.browser.data_source.write(new_key, doc)
        self.status_label.SetLabel(f'Fixed copy saved as {new_key}')
        return {'key': new_key}

    def _on_fix_as_new_note(self):
        if self.is_running:
            return
        suggested = self._suggest_fixed_name()
        dlg = wx.TextEntryDialog(
            self, 'Save the current outputs as a new, non-runnable note:',
            'Fix as New Note', suggested,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            new_key = dlg.GetValue().strip()
        finally:
            dlg.Destroy()
        if not new_key:
            return
        try:
            self.fix_as_new_note(new_key)
        except Exception as e:
            wx.MessageBox(f'Failed to save fixed copy: {e}', 'Fix as New Note', wx.OK | wx.ICON_ERROR)

    def export_data(self, path):
        cells_out = {}
        for cell_id, panel in self.cell_panels.items():
            cells_out[cell_id] = {
                'inputs': {str(i): v for i, v in enumerate(panel.get_input_values())},
                'output': panel.get_stdout_text(),
                'show_items': panel.get_show_items(),
            }
        payload = {
            'format': 'runnable-sheet-data',
            'version': 2,
            'sheet': self.data.get('title', ''),
            'doc_key': self.key,
            'exported': datetime.datetime.now().isoformat(),
            'cells': cells_out,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        self.status_label.SetLabel(f'Exported to {os.path.basename(path)}')
        return {'path': path, 'cells': len(cells_out)}

    def import_data(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        version = int(state.get('version', 1))
        fmt = state.get('format', 'runnable-sheet-state')
        if fmt not in ('runnable-sheet-state', 'runnable-sheet-data'):
            raise ValueError(f'Unsupported sheet data format: {fmt}')
        if version not in (1, 2):
            raise ValueError(f'Unsupported sheet data version: {version}')
        cells = state.get('cells', {})
        updated = missing = superfluous = 0
        for cell_id, panel in self.cell_panels.items():
            if cell_id in cells:
                cs = cells[cell_id]
                inp = cs.get('inputs', {})
                for i, ctrl in enumerate(panel.input_ctrls):
                    ctrl.SetValue(inp.get(str(i), ''))
                out = cs.get('output', '')
                show_items = cs.get('show_items', []) if version >= 2 else []
                if not isinstance(show_items, list):
                    show_items = []
                if out or show_items:
                    panel.set_output(out, ok=True, show_items=show_items)
                    if panel.stdout_ctrl:
                        panel.stdout_ctrl.SetBackgroundColour(wx.Colour(240, 240, 200))
                        panel.stdout_ctrl.SetForegroundColour(wx.Colour(60, 60, 60))
                        panel.stdout_ctrl.Refresh()
                else:
                    panel.clear_output()
                updated += 1
            else:
                missing += 1
        superfluous = sum(1 for k in cells if k not in self.cell_panels)
        self.status_label.SetLabel(f'Imported from {os.path.basename(path)}')
        return {'updated': updated, 'missing': missing, 'superfluous': superfluous}

    # Backward-compatible aliases for control RPC method names.
    def export_state(self, path):
        return self.export_data(path)

    def import_state(self, path):
        return self.import_data(path)

    def _set_toolbar_enabled(self, enabled):
        for btn in (self.run_all_btn, self.save_btn, self.clear_btn, self.restart_btn,
                    self.export_btn, self.import_btn):
            btn.Enable(enabled)
        for panel in self.cell_panels.values():
            panel.run_button.Enable(enabled)
