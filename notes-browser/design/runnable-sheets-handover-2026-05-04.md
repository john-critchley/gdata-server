# Runnable Sheets: Usage Guide

Runnable Sheets are a notebook-style feature in the notes browser. A document marked `"runnable": true` is displayed as an interactive sheet with executable code cells, shared Python state, and per-cell output panels.

## Creating a runnable document

Add `"runnable": true` to the top level of a JSONHTL document. Executable code cells are `codeblock` blocks with `"exec": true`.

```json
{
  "title": "My Sheet",
  "runnable": true,
  "content": [
    {"heading": {"level": 1, "text": "My Sheet"}},
    {"para": ["Define a value, then compute with it."]},
    {"codeblock": {"lang": "python", "exec": true, "name": "setup",
      "body": "value = 42\nprint('value =', value)"}},
    {"para": ["Compute something."]},
    {"codeblock": {"lang": "python", "exec": true, "name": "compute",
      "body": "print('double:', value * 2)"}}
  ]
}
```

Non-executable codeblocks (no `"exec": true`) are displayed as read-only reference code.

### Cell naming

Give each executable cell a unique `name`. The name is used as the cell key in RPC calls and in error tracebacks. If `name` is absent or duplicated, the cell falls back to its zero-based position index string (`"0"`, `"1"`, …).

### input() fields

If a cell contains `input()` calls, the browser detects them via AST analysis at load time and creates a labelled input field per call. Fill in the values before running the cell.

```python
# This cell will show a text field labelled "Your name: "
name = input("Your name: ")
print("Hello,", name)
```

Input field IDs are `cell_key/N` (e.g. `greet/0`).

## Loading the sheet

Navigate to the document key in the browser. If the document has `"runnable": true`, the HTML view is replaced automatically by the sheet panel. Navigation away destroys the sheet and its kernel state.

## Running cells

### From the UI

Each cell has a **Run** button in its header. Click it to execute that cell and see its output below the code.

The toolbar provides:

| Button | Action |
|---|---|
| Run All | Execute all executable cells in document order |
| Clear Outputs | Remove all output text and status labels (kernel state preserved) |
| Restart Kernel | Reset Python namespace (input values and visible outputs preserved) |
| Export | Save sheet state (inputs + outputs) to a JSON file |
| Import | Restore a previously saved sheet state |

### From the control socket

All sheet operations are also available via JSON-RPC. See [control-socket-detailed.md](control-socket-detailed.md) for the full API reference. Quick example:

```python
rpc('sheet.restart')
rpc('sheet.inputs.set', {'id': 'greet/0', 'value': 'Alice'})
result = rpc('sheet.cell.run', {'cell': 'greet'})
print(result['status'], result['output'])
# Done   Hello, Alice
```

## Execution model

- All cells share **one Python namespace** for the lifetime of the sheet.
- Variables defined in one cell are visible to all later cells.
- Run order matters: running `compute` before `setup` will fail if `setup` defines `value`.
- Exceptions are caught and shown as `Error` status with a full traceback. The browser does not close; subsequent cells can still be run.

## Clear vs Restart

| Action | Kernel state | Output display |
|---|---|---|
| Clear Outputs | **unchanged** | cleared |
| Restart Kernel | **reset** | preserved |

After **Clear Outputs**, you can re-run cells and they will use variables from the previous run.

After **Restart**, all variables are gone and all cells must be re-run from scratch.

## Export / Import

**Export** writes a JSON file containing:
- current input field values for all cells
- current output text for all cells

**Import** reads that file and restores input values and output display. It does not re-execute any code, so kernel variables are not restored — import only resets the display.

Typical workflow for saving and restoring a session:

```
1. Run the sheet to completion.
2. Export state to a file.
3. Later: navigate back to the sheet, import state.
4. Re-run any cells whose kernel values you need.
```

## Automation example

Start the browser with a control socket:

```bash
cd notes-browser
python notes_browser_runnable.py \
    --url http://127.0.0.1:8021 \
    --control-tcp-enabled --control-tcp-port 18736 \
    --control-token test-token \
    --page my/runnable-sheet
```

Drive it from a script:

```python
import json, socket

def rpc(method, params=None):
    p = dict(params or {'token': 'test-token'})
    p.setdefault('token', 'test-token')
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}
    with socket.create_connection(('127.0.0.1', 18736), timeout=10) as s:
        s.sendall((json.dumps(req) + '\n').encode())
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk: break
            buf += chunk
            try: return json.loads(buf.decode())['result']
            except (json.JSONDecodeError, KeyError): pass

rpc('sheet.restart')
rpc('sheet.inputs.set', {'id': 'greet/0', 'value': 'Alice'})

r = rpc('sheet.cell.run', {'cell': 'setup'})
assert r['status'] == 'Done', r

r = rpc('sheet.run_all')
for cell in r['cells']:
    print(cell['cell'], cell['status'], cell['output'].strip())

rpc('sheet.export_state', {'path': '/tmp/my-sheet.json'})
rpc('ui.capture_screenshot', {'path': '/tmp/my-sheet.png'})
```

## Files

| File | Purpose |
|---|---|
| `notes-browser/notes_browser_runnable.py` | Browser application with sheet support and RPC dispatch |
| `notes-browser/sheet_ui.py` | `ProsePanel`, `SheetCellPanel`, `RunnableSheetPanel` widgets |
| `notes-browser/sheet_kernel.py` | `SheetKernel` execution engine |

## Known issues

- A GTK warning (`gtk_box_gadget_distribute assertion 'size >= 0' failed`) may appear during heavy screenshot automation runs. It does not block execution.
- Refreshing a runnable page recreates the sheet panel and kernel — input values and outputs are lost. Use Export before navigating away if you want to restore them.
- Output height is fixed-minimum; very long output requires scrolling within the output control.
