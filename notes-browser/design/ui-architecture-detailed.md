# Notes Browser: UI Architecture

The notes browser is a wxPython desktop application. It connects to the gdata HTTP server (or a local GDBM file), renders JSONHTL documents, and provides navigation, zooming, text selection, and — when a document is marked `"runnable": true` — a live code execution view.

## Launching the browser

```bash
# Connect to HTTP server (most common)
python notes_browser_runnable.py --url http://127.0.0.1:8021

# Connect directly to a GDBM file (no server needed)
python notes_browser_runnable.py --gdbm-file /path/to/notes.gdbm

# Open a specific page on startup
python notes_browser_runnable.py --url http://127.0.0.1:8021 --page my/doc/key

# With control socket enabled
python notes_browser_runnable.py \
    --url http://127.0.0.1:8021 \
    --control-tcp-enabled --control-tcp-port 18716 \
    --control-token mytoken

# Font overrides
python notes_browser_runnable.py --render-font-size-pt 12 --render-font-family "Helvetica"
```

Settings can also be stored in `~/.notes_browser.yaml` (or a file specified with `--config`). Command-line flags override config file values.

## Window layout

```
wx.Frame (NotesBrowser)
└── panel (wx.Panel)
    └── wx.BoxSizer (VERTICAL)
        ├── nav_sizer (wx.BoxSizer HORIZONTAL)
        │   ├── back_btn
        │   ├── forward_btn
        │   ├── home_btn
        │   ├── refresh_btn
        │   └── page_text (wx.TextCtrl, address bar)
        ├── selection_info (wx.StaticText)
        └── content_host (wx.Panel)
            └── content_sizer (wx.BoxSizer VERTICAL)
                ├── html (NotesHtmlWindow)     — shown for normal documents
                └── sheet_panel (RunnableSheetPanel) — shown for runnable documents
```

At any moment only one of `html` or `sheet_panel` is visible. Switching between them happens automatically when navigating to a different document type.

## Normal documents

For ordinary JSONHTL documents, `NotesHTMLRenderer` converts the document to an HTML string and `NotesHtmlWindow` (a subclass of `wx.html.HtmlWindow`) displays it. Clicking a link calls `OnLinkClicked`, which resolves the `href` value and navigates via `_navigate_to()`.

Zoom is handled by adjusting the renderer's font size and re-rendering. The zoom level is preserved across navigations.

## Runnable documents

When a document has `"runnable": true` at the top level, the browser replaces the HTML view with a `RunnableSheetPanel`. The HTML window is hidden; the sheet panel is created fresh each time a runnable document is loaded.

### RunnableSheetPanel layout

```
RunnableSheetPanel (wx.Panel)
└── wx.BoxSizer (VERTICAL)
    ├── toolbar_panel (wx.Panel)
    │   └── wx.BoxSizer (HORIZONTAL)
    │       ├── run_all_button    ("Run All")
    │       ├── clear_btn         ("Clear Outputs")
    │       ├── restart_btn       ("Restart Kernel")
    │       ├── export_btn        ("Export")
    │       ├── import_btn        ("Import")
    │       ├── stretch spacer
    │       └── status_label      (wx.StaticText)
    └── scroller (wx.ScrolledWindow)
        └── blocks_sizer (wx.BoxSizer VERTICAL)
            ├── ProsePanel        (headings, paragraphs, lists)
            ├── SheetCellPanel    (executable codeblock)
            ├── ProsePanel
            ├── SheetCellPanel
            └── ...
```

Content blocks from the JSONHTL document are rendered in document order. Non-executable blocks (headings, paragraphs, lists) become `ProsePanel` widgets. Executable codeblocks become `SheetCellPanel` widgets.

### SheetCellPanel layout

```
SheetCellPanel (wx.Panel, styled background)
└── wx.BoxSizer (VERTICAL)
    ├── header_panel (wx.Panel, tinted background)
    │   └── wx.BoxSizer (HORIZONTAL)
    │       ├── name_label   (cell name, bold)
    │       ├── lang_label   (language tag, accent colour)
    │       ├── stretch spacer
    │       ├── status_label ("Done" / "Error" / "Running…")
    │       └── run_button   ("Run")
    ├── code_ctrl    (wx.TextCtrl, read-only, monospace, dark theme)
    ├── input_panel  (wx.Panel with label + TextCtrl per input() call)
    │                — hidden if no input() calls in the cell
    ├── output_label (wx.StaticText "Output", bold)
    │                — hidden until output exists
    └── output_ctrl  (wx.TextCtrl, read-only, monospace)
                     — hidden until output exists
```

### Execution

All cells share a single `SheetKernel` instance (one Python namespace) for the lifetime of the sheet. Execution runs on the wx main thread with `wx.YieldIfNeeded()` called between cells during run-all so the UI stays responsive.

**Run one cell:** clicking Run (or calling `sheet.cell.run` via RPC) executes that cell synchronously. Output replaces any previous output for that cell. Status shows `"Done"` or `"Error"` and persists until the next run or until outputs are cleared.

**Run All:** executes all executable cells in document order. Stops at the first error.

**Clear Outputs:** removes all visible output text and status labels. The kernel namespace is untouched — variables defined previously are still available.

**Restart Kernel:** creates a new Python interpreter. All variables are gone. Existing input values and visible outputs are preserved in the UI.

### Input fields

If a cell's source code contains `input()` calls, the browser detects them via AST analysis at load time and creates a labelled text field per call. The user fills in values before running the cell. The `input()` shim inside `SheetKernel` reads these values in order and echoes the prompt to stdout.

### Export / Import

The toolbar Export and Import buttons open file dialogs to save and restore sheet state as a JSON file. The same operations are available programmatically via `sheet.export_state` and `sheet.import_state` RPC calls (which take a path directly, with no dialog).

The state file contains input field values and output text for all cells. It does not contain kernel variable state; re-running cells after import is necessary to restore that.

## Data source

`NotesDataSource` abstracts over HTTP and local GDBM access. It provides:

- `read_doc(key)` → dict
- `write(key, doc)` → None

Navigation calls `read_doc`, renders/displays the result, and pushes the key to the history stack.

## Settings

`~/.notes_browser.yaml` (or `--config PATH`) accepts:

```yaml
notes_url: http://127.0.0.1:8021
render_font_size_pt: 11
render_font_family: "'Helvetica Neue', Helvetica, sans-serif"
control_tcp_enabled: true
control_tcp_port: 18716
control_token: mytoken
```

Command-line flags always override settings file values.
