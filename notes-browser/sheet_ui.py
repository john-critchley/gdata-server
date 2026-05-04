"""
sheet_ui.py — Runnable Sheet UI classes for notes_browser_runnable.py

Classes:
  ProsePanel         — JSONHTL prose blocks rendered as wx.html.HtmlWindow
  SheetCellPanel     — One executable cell (code, inputs, run button, output)
  RunnableSheetPanel — Full sheet: toolbar + scroller of ProsePanel/SheetCellPanel
"""

import datetime
import json
import os
import re

import wx
import wx.html

from urllib.parse import unquote
from sheet_kernel import SheetKernel, detect_inputs

ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def _clean_output(text):
    return ANSI_RE.sub('', text) if text else ''


# ---------------------------------------------------------------------------
# ProsePanel
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

        html_body = renderer._render_jsonhtl_blocks(blocks)
        html = f"<html><body style='margin:8px'>{html_body}</body></html>"

        self.htmlwin = _ProseHtmlWindow(self, browser)
        self.htmlwin.SetPage(html)

        ir = self.htmlwin.GetInternalRepresentation()
        h = (ir.GetHeight() + 10) if ir else 60
        self.htmlwin.SetMinSize((-1, h))

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.htmlwin, 1, wx.EXPAND)
        self.SetSizer(sizer)


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

        mono = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- Header ---
        header = wx.Panel(self)
        hs = wx.BoxSizer(wx.HORIZONTAL)
        name = cell_spec.get('name') or cell_id
        self.name_label = wx.StaticText(header, label=name)
        self.lang_label = wx.StaticText(header, label=cell_spec.get('lang', ''))
        self.status_label = wx.StaticText(header, label='')
        self.run_button = wx.Button(header, label='Run')
        hs.Add(self.name_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        hs.Add(self.lang_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        hs.AddStretchSpacer(1)
        hs.Add(self.status_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
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

        # --- Output ---
        self.output_label = wx.StaticText(self, label='Output')
        self.output_ctrl = wx.TextCtrl(
            self, value='',
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SIMPLE,
        )
        self.output_ctrl.SetFont(mono)
        self.output_ctrl.SetMinSize((-1, 80))
        self.output_label.Hide()
        self.output_ctrl.Hide()
        outer.Add(self.output_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        outer.Add(self.output_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.SetSizer(outer)
        self.run_button.Bind(wx.EVT_BUTTON, lambda e: self.sheet_panel.run_cell(self.cell_id))

    def set_running(self, running):
        self.run_button.Enable(not running)
        self.status_label.SetLabel('Running…' if running else '')

    def set_status(self, text):
        self.status_label.SetLabel(text)

    def set_output(self, output, error=None, ok=True):
        parts = [_clean_output(s) for s in [output, error] if s]
        text = '\n'.join(parts)
        if not ok:
            self.output_ctrl.SetBackgroundColour(wx.Colour(255, 224, 224))
            self.output_label.SetLabel('Output (error)')
        else:
            self.output_ctrl.SetBackgroundColour(wx.Colour(224, 255, 224))
            self.output_label.SetLabel('Output')
        self.output_ctrl.SetValue(text)
        self.output_label.Show()
        self.output_ctrl.Show()
        self.Layout()
        p = self.GetParent()
        if p:
            p.Layout()

    def clear_output(self):
        self.output_ctrl.SetValue('')
        self.output_label.Hide()
        self.output_ctrl.Hide()
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
        self.kernel = SheetKernel()

        outer = wx.BoxSizer(wx.VERTICAL)

        # --- Toolbar ---
        tb = wx.Panel(self)
        tbs = wx.BoxSizer(wx.HORIZONTAL)
        self.run_all_btn  = wx.Button(tb, label='Run All')
        self.clear_btn    = wx.Button(tb, label='Clear Outputs')
        self.restart_btn  = wx.Button(tb, label='Restart Kernel')
        self.export_btn   = wx.Button(tb, label='Export…')
        self.import_btn   = wx.Button(tb, label='Import…')
        self.status_label = wx.StaticText(tb, label='')
        for btn in (self.run_all_btn, self.clear_btn, self.restart_btn,
                    self.export_btn, self.import_btn):
            tbs.Add(btn, 0, wx.ALL, 4)
        tbs.AddStretchSpacer(1)
        tbs.Add(self.status_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        tb.SetSizer(tbs)
        outer.Add(tb, 0, wx.EXPAND)

        self.run_all_btn.Bind(wx.EVT_BUTTON,  lambda e: self._on_run_all())
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

    def _build_content(self, data, browser):
        content = data.get('content', [])
        renderer = browser.renderer
        codeblock_index = 0
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
                cp.output_label.SetLabel('Output (from doc)')
                cp.output_ctrl.SetBackgroundColour(wx.Colour(240, 240, 200))

            self.cell_panels[canon] = cp
            self.blocks_sizer.Add(cp, 0, wx.EXPAND | wx.ALL, 4)

        if prose_run:
            pp = ProsePanel(self.scroller, browser, prose_run, renderer)
            self.blocks_sizer.Add(pp, 0, wx.EXPAND | wx.BOTTOM, 2)

    # --- Execution ---

    def run_cell(self, cell_id):
        if self.is_running:
            return
        self.is_running = True
        self._set_toolbar_enabled(False)
        self.browser._set_nav_enabled(False)
        panel = self.cell_panels[cell_id]
        cell = self.cells[cell_id]
        panel.set_running(True)
        wx.YieldIfNeeded()
        try:
            output, error, ok = self.kernel.run_cell(cell_id, cell['body'], panel.get_input_values())
            panel.set_output(output, error, ok)
            panel.set_status('Done' if ok else 'Error')
        finally:
            panel.set_running(False)
            self.is_running = False
            self._set_toolbar_enabled(True)
            self.browser._set_nav_enabled(True)

    def _on_run_all(self):
        if self.is_running:
            return
        self.is_running = True
        self._set_toolbar_enabled(False)
        self.browser._set_nav_enabled(False)
        self.status_label.SetLabel('Running…')
        all_ok = True
        try:
            for cell_id in self.exec_cell_order:
                cell = self.cells[cell_id]
                panel = self.cell_panels[cell_id]
                panel.set_running(True)
                panel.set_status('Running…')
                wx.YieldIfNeeded()
                output, error, ok = self.kernel.run_cell(cell_id, cell['body'], panel.get_input_values())
                panel.set_output(output, error, ok)
                panel.set_status('Done' if ok else 'Error')
                panel.set_running(False)
                wx.YieldIfNeeded()
                if not ok:
                    all_ok = False
                    break
        finally:
            self.is_running = False
            self._set_toolbar_enabled(True)
            self.browser._set_nav_enabled(True)
            self.status_label.SetLabel('Done' if all_ok else 'Stopped on error')

    def _on_clear_outputs(self):
        if self.is_running:
            return
        self.kernel.reset()
        for panel in self.cell_panels.values():
            panel.clear_output()
            panel.set_status('')
        self.status_label.SetLabel('Outputs cleared.')

    def _on_restart_kernel(self):
        if self.is_running:
            return
        self.kernel.reset()
        for panel in self.cell_panels.values():
            if panel.output_ctrl.IsShown():
                panel.output_label.SetLabel('Output (stale)')
                panel.output_ctrl.SetBackgroundColour(wx.Colour(240, 240, 200))
                panel.output_ctrl.Refresh()
            panel.set_status('')
        self.status_label.SetLabel('Kernel restarted.')

    def _on_export(self):
        if self.is_running:
            return
        dlg = wx.FileDialog(
            self, 'Save Sheet State',
            wildcard='Sheet state (*.sheetstate.json)|*.sheetstate.json',
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            cells_out = {}
            for cell_id, panel in self.cell_panels.items():
                cells_out[cell_id] = {
                    'inputs': {str(i): v for i, v in enumerate(panel.get_input_values())},
                    'output': panel.output_ctrl.GetValue() if panel.output_ctrl.IsShown() else '',
                }
            payload = {
                'format': 'runnable-sheet-state',
                'version': 1,
                'sheet': self.data.get('title', ''),
                'doc_key': self.key,
                'exported': datetime.datetime.now().isoformat(),
                'cells': cells_out,
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            self.status_label.SetLabel(f'Exported to {os.path.basename(path)}')
        dlg.Destroy()

    def _on_import(self):
        if self.is_running:
            return
        dlg = wx.FileDialog(
            self, 'Open Sheet State',
            wildcard='Sheet state (*.sheetstate.json)|*.sheetstate.json',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                version = state.get('version', 1)
                if version != 1:
                    wx.MessageBox(
                        f'Unsupported sheet state version: {version}.\nThis browser supports version 1.',
                        'Import Error', wx.ICON_ERROR,
                    )
                    dlg.Destroy()
                    return
                cells = state.get('cells', {})
                updated = missing = superfluous = 0
                for cell_id, panel in self.cell_panels.items():
                    if cell_id in cells:
                        cs = cells[cell_id]
                        inp = cs.get('inputs', {})
                        for i, ctrl in enumerate(panel.input_ctrls):
                            ctrl.SetValue(inp.get(str(i), ''))
                        out = cs.get('output', '')
                        if out:
                            panel.set_output(out, ok=True)
                            panel.output_label.SetLabel('Output (imported)')
                            panel.output_ctrl.SetBackgroundColour(wx.Colour(240, 240, 200))
                        else:
                            panel.clear_output()
                        updated += 1
                    else:
                        missing += 1
                superfluous = sum(1 for k in cells if k not in self.cell_panels)
                wx.MessageBox(
                    f'Updated cells: {updated}\nMissing cells left unchanged: {missing}\nIgnored unknown cells: {superfluous}',
                    'Import Complete', wx.ICON_INFORMATION,
                )
                self.status_label.SetLabel(f'Imported from {os.path.basename(path)}')
            except Exception as e:
                wx.MessageBox(f'Failed to import: {e}', 'Import Error', wx.ICON_ERROR)
        dlg.Destroy()

    def _set_toolbar_enabled(self, enabled):
        for btn in (self.run_all_btn, self.clear_btn, self.restart_btn,
                    self.export_btn, self.import_btn):
            btn.Enable(enabled)
        for panel in self.cell_panels.values():
            panel.run_button.Enable(enabled)
