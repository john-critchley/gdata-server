## 1. Decision: use the hybrid native sheet UI

Use **a custom `wx.ScrolledWindow` sheet view with `wx.Panel` per cell**. Keep the existing `wx.html.HtmlWindow` only for normal non-runnable documents.

Do **not** keep `wx.html.HtmlWindow` for runnable sheets. It cannot provide real buttons, input fields, focus handling, dynamic output panels, or reliable per-cell state.

Do **not** switch the whole browser to `wx.WebView` for this feature. `wx.WebView` would give HTML/CSS/JS, but then the app needs a JavaScript/native bridge for running Python, updating outputs, controlling inputs through RPC, and preserving the existing `navigate://` behavior. That adds complexity without solving execution, because Python still runs in the wxPython process.

Best implementation choice:

```text
Normal JSONHTL document      -> existing NotesHtmlWindow / wx.html.HtmlWindow
Runnable JSONHTL document    -> RunnableSheetPanel / wx.Panel
                                  containing toolbar + wx.ScrolledWindow
                                  containing native per-block/per-cell widgets
```

This gives:

- Native `wx.Button` Run buttons.
- Native `wx.TextCtrl` input fields.
- Native output areas.
- Easy enable/disable state while running.
- Easy JSON-RPC control because every input/output widget maps directly to a Python object.
- No JavaScript bridge.
- No server writes required.

---

## 2. Widget hierarchy for one executable sheet cell

Use one native panel per executable codeblock.

Class:

```python
class SheetCellPanel(wx.Panel):
    ...
```

Recommended hierarchy:

```text
SheetCellPanel(wx.Panel, style=wx.BORDER_SIMPLE)
└── wx.BoxSizer(wx.VERTICAL)

    ├── header_panel(wx.Panel)
    │   └── wx.BoxSizer(wx.HORIZONTAL)
    │       ├── name_label(wx.StaticText)
    │       │       Example: "cell_1" or "Cell 7"
    │       ├── language_label(wx.StaticText)
    │       │       Example: "python"
    │       ├── stretch spacer
    │       ├── status_label(wx.StaticText)
    │       │       Example: "", "Running…", "Done", "Error"
    │       └── run_button(wx.Button)
    │               Label: "Run"

    ├── code_ctrl(wx.stc.StyledTextCtrl)
    │       Read-only
    │       Monospace
    │       No editing
    │       Displays codeblock["body"]
    │
    │   Alternative if avoiding wx.stc:
    │       wx.TextCtrl(
    │           style=wx.TE_MULTILINE
    │               | wx.TE_READONLY
    │               | wx.TE_DONTWRAP
    │               | wx.BORDER_SIMPLE
    │       )

    ├── input_panel(wx.Panel)
    │   └── wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
    │       ├── wx.StaticText(label="Enter name: ")
    │       ├── wx.TextCtrl(value="")
    │       ├── wx.StaticText(label="Input")
    │       ├── wx.TextCtrl(value="")
    │       └── ...
    │
    │   Hidden when the cell has no detected input() calls.

    ├── output_label(wx.StaticText)
    │       Label: "Output"
    │       Hidden until output exists.

    └── output_ctrl(wx.TextCtrl)
            style = wx.TE_MULTILINE
                  | wx.TE_READONLY
                  | wx.TE_DONTWRAP
                  | wx.BORDER_SIMPLE

            Hidden until output exists.
            Shows captured stdout/stderr text.
```

Concrete construction sketch:

```python
class SheetCellPanel(wx.Panel):
    def __init__(self, parent, sheet_panel, cell):
        super().__init__(parent, style=wx.BORDER_SIMPLE)

        self.sheet_panel = sheet_panel
        self.cell = cell
        self.cell_id = cell.cell_id
        self.input_ctrls = []

        outer = wx.BoxSizer(wx.VERTICAL)

        # Header row
        header = wx.Panel(self)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.name_label = wx.StaticText(header, label=cell.display_name)
        self.lang_label = wx.StaticText(header, label=cell.lang or "")
        self.status_label = wx.StaticText(header, label="")
        self.run_button = wx.Button(header, label="Run")

        header_sizer.Add(self.name_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        header_sizer.Add(self.lang_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        header_sizer.AddStretchSpacer(1)
        header_sizer.Add(self.status_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        header_sizer.Add(self.run_button, 0, wx.ALL, 4)
        header.SetSizer(header_sizer)

        outer.Add(header, 0, wx.EXPAND)

        # Code display
        self.code_ctrl = wx.TextCtrl(
            self,
            value=cell.body,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SIMPLE,
        )
        self.code_ctrl.SetFont(wx.Font(
            10,
            wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_NORMAL,
        ))
        self.code_ctrl.SetMinSize((-1, 120))
        outer.Add(self.code_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Inputs
        self.input_panel = wx.Panel(self)
        input_sizer = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        input_sizer.AddGrowableCol(1, 1)

        for i, prompt in enumerate(cell.input_prompts):
            label = prompt or f"Input {i + 1}"
            label_ctrl = wx.StaticText(self.input_panel, label=label)
            value_ctrl = wx.TextCtrl(self.input_panel, value=cell.inputs.get(str(i), ""))

            field_id = f"{self.cell_id}/{i}"
            value_ctrl.field_id = field_id

            value_ctrl.Bind(wx.EVT_TEXT, self._on_input_changed)

            self.input_ctrls.append(value_ctrl)

            input_sizer.Add(label_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
            input_sizer.Add(value_ctrl, 1, wx.EXPAND)

        self.input_panel.SetSizer(input_sizer)

        if not cell.input_prompts:
            self.input_panel.Hide()

        outer.Add(self.input_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Output
        self.output_label = wx.StaticText(self, label="Output")
        self.output_ctrl = wx.TextCtrl(
            self,
            value=cell.output or "",
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.BORDER_SIMPLE,
        )
        self.output_ctrl.SetFont(wx.Font(
            10,
            wx.FONTFAMILY_TELETYPE,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_NORMAL,
        ))
        self.output_ctrl.SetMinSize((-1, 80))

        if not cell.output:
            self.output_label.Hide()
            self.output_ctrl.Hide()

        outer.Add(self.output_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        outer.Add(self.output_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.SetSizer(outer)

        self.run_button.Bind(wx.EVT_BUTTON, self._on_run_clicked)

    def _on_run_clicked(self, event):
        self.sheet_panel.run_cell(self.cell_id)

    def _on_input_changed(self, event):
        ctrl = event.GetEventObject()
        self.sheet_panel.set_input_value(ctrl.field_id, ctrl.GetValue())

    def set_running(self, running):
        self.run_button.Enable(not running)
        self.status_label.SetLabel("Running…" if running else "")

    def set_output(self, text):
        self.output_ctrl.SetValue(text or "")
        if text:
            self.output_label.Show()
            self.output_ctrl.Show()
        else:
            self.output_label.Hide()
            self.output_ctrl.Hide()

        self.Layout()
        self.GetParent().Layout()
```

---

## 3. How the sheet panel slots into `NotesBrowser`

Currently the main layout is:

```text
wx.Frame
└── panel(wx.Panel)
    └── wx.BoxSizer(wx.VERTICAL)
        ├── nav_sizer
        ├── selection_info(wx.StaticText)
        └── html(NotesHtmlWindow / wx.html.HtmlWindow)
```

Change the bottom content area into a replaceable host panel.

New structure:

```text
wx.Frame
└── panel(wx.Panel)
    └── wx.BoxSizer(wx.VERTICAL)
        ├── nav_sizer
        ├── selection_info(wx.StaticText)
        └── content_host(wx.Panel)
            └── content_sizer(wx.BoxSizer(wx.VERTICAL))
                ├── html(NotesHtmlWindow)          shown for normal docs
                └── sheet_panel(RunnableSheetPanel) shown for runnable docs
```

Implementation pattern:

```python
self.content_host = wx.Panel(panel)
self.content_sizer = wx.BoxSizer(wx.VERTICAL)
self.content_host.SetSizer(self.content_sizer)

self.html = NotesHtmlWindow(self.content_host, self)
self.sheet_panel = None

self.content_sizer.Add(self.html, 1, wx.EXPAND)
sizer.Add(self.content_host, 1, wx.EXPAND)
```

On navigation/render:

```python
def _display_page(self, key, data):
    is_runnable = isinstance(data, dict) and data.get("runnable") is True

    if is_runnable:
        self._show_sheet(key, data)
    else:
        self._show_html(key, data)
```

Normal document:

```python
def _show_html(self, key, data):
    if self.sheet_panel is not None:
        self.content_sizer.Detach(self.sheet_panel)
        self.sheet_panel.Destroy()
        self.sheet_panel = None

    html = self.renderer.render(key, data)
    self.html.SetPage(html)
    self.html.Show()

    if self.html.GetContainingSizer() is None:
        self.content_sizer.Add(self.html, 1, wx.EXPAND)

    self.content_host.Layout()
```

Runnable document:

```python
def _show_sheet(self, key, data):
    self.html.Hide()

    if self.sheet_panel is not None:
        self.content_sizer.Detach(self.sheet_panel)
        self.sheet_panel.Destroy()
        self.sheet_panel = None

    self.sheet_panel = RunnableSheetPanel(
        self.content_host,
        browser=self,
        key=key,
        data=data,
    )

    self.content_sizer.Add(self.sheet_panel, 1, wx.EXPAND)
    self.sheet_panel.Show()

    self.content_host.Layout()
```

`RunnableSheetPanel` hierarchy:

```text
RunnableSheetPanel(wx.Panel)
└── wx.BoxSizer(wx.VERTICAL)

    ├── toolbar_panel(wx.Panel)
    │   └── wx.BoxSizer(wx.HORIZONTAL)
    │       ├── run_all_button(wx.Button, "Run All")
    │       ├── clear_outputs_button(wx.Button, "Clear All Outputs")
    │       ├── restart_button(wx.Button, "Restart Kernel")
    │       ├── export_button(wx.Button, "Export")
    │       ├── import_button(wx.Button, "Import")
    │       ├── stretch spacer
    │       └── status_label(wx.StaticText)

    └── scroller(wx.ScrolledWindow)
        └── blocks_sizer(wx.BoxSizer(wx.VERTICAL))
            ├── heading/prose/list/table widgets
            ├── SheetCellPanel
            ├── SheetCellPanel
            └── ...
```

Use:

```python
self.scroller.SetScrollRate(10, 10)
```

The sheet panel owns all session-local state:

```python
self.cells_by_id = {}
self.cell_panels_by_id = {}
self.input_values = {}
self.outputs = {}
self.kernel = ...
```

No outputs or inputs are written back to `data_source`.

---

## 4. Run All execution model

Use a **single background worker thread** for execution and `wx.CallAfter` for all UI updates.

Do not run arbitrary Python cells directly on the wx main thread. `wx.Yield()` only repaints between cells; it cannot prevent freezing during a long-running or CPU-bound cell. It also risks re-entrancy bugs if used heavily.

The UI main thread should:

1. Collect the ordered list of executable cells.
2. Snapshot current input values from the widgets.
3. Disable Run buttons and toolbar buttons.
4. Start or enqueue work on a single runner thread.
5. Receive output/status updates through `wx.CallAfter`.

Execution remains sequential because the worker uses one queue and one shared interpreter.

Sketch:

```python
def run_all(self):
    if self.running:
        return

    jobs = []
    for cell_id in self.exec_cell_order:
        cell = self.cells_by_id[cell_id]
        inputs = self.get_inputs_for_cell(cell_id)
        jobs.append((cell_id, cell.body, inputs))

    self.running = True
    self.set_all_running_ui(True)

    thread = threading.Thread(
        target=self._run_all_worker,
        args=(jobs,),
        daemon=True,
    )
    thread.start()
```

Worker:

```python
def _run_all_worker(self, jobs):
    try:
        for cell_id, body, inputs in jobs:
            wx.CallAfter(self._mark_cell_running, cell_id, True)

            try:
                output = self.kernel.run_cell(body, inputs)
                wx.CallAfter(self._set_cell_output, cell_id, output)
                wx.CallAfter(self._mark_cell_status, cell_id, "Done")
            except Exception:
                output = traceback.format_exc()
                wx.CallAfter(self._set_cell_output, cell_id, output)
                wx.CallAfter(self._mark_cell_status, cell_id, "Error")

            wx.CallAfter(self._mark_cell_running, cell_id, False)

    finally:
        wx.CallAfter(self._run_all_finished)
```

Finish on main thread:

```python
def _run_all_finished(self):
    self.running = False
    self.set_all_running_ui(False)
    self.status_label.SetLabel("Ready")
```

Important rules:

- Never create, destroy, read, or mutate wx widgets from the worker thread.
- All widget updates go through `wx.CallAfter`.
- Only one execution thread may run at a time.
- The shared Python namespace belongs to the sheet kernel and is reused across cells.
- Restart Kernel clears/replaces that interpreter and clears execution state as needed.

For input handling:

```python
def get_inputs_for_cell(self, cell_id):
    panel = self.cell_panels_by_id[cell_id]
    return [ctrl.GetValue() for ctrl in panel.input_ctrls]
```

For the kernel, avoid making UI calls from executed code. Capture stdout/stderr into strings and return them.

---

## 5. ANSI colour handling in output

For v1: **strip ANSI escape codes before displaying output**.

Reason:

- The chosen output widget is `wx.TextCtrl`, which is plain text.
- Showing raw ANSI codes looks broken.
- Rendering ANSI colours properly would require `wx.richtext.RichTextCtrl` or a custom renderer.
- Colour rendering can be added later without changing the sheet state model.

Use a small sanitizer:

```python
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def clean_output_for_display(text):
    if not text:
        return ""
    return ANSI_RE.sub("", text)
```

Apply before storing/displaying session output:

```python
output = clean_output_for_display(output)
self.outputs[cell_id] = output
panel.set_output(output)
```

Decision: **strip ANSI codes, do not render them, do not leave them visible.**