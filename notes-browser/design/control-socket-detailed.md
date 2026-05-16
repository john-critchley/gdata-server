# Notes Browser: Control Socket API

The notes browser exposes a JSON-RPC 2.0 control interface that lets scripts, automation tools, and agents drive the browser programmatically. The control socket supports TCP, UDP, and Unix domain stream sockets simultaneously.

## Starting with control enabled

```bash
python notes_browser_runnable.py \
    --url http://127.0.0.1:8021 \
    --control-tcp-enabled \
    --control-tcp-port 18716 \
    --control-token mytoken
```

| Flag | Description |
|---|---|
| `--control-tcp-enabled` / `--control-tcp-port PORT` | Listen on TCP |
| `--control-udp-enabled` / `--control-udp-port PORT` | Listen on UDP |
| `--control-unix-enabled` / `--control-unix-socket PATH` | Listen on Unix socket |
| `--control-token TOKEN` | Shared secret required in every request |
| `--control-host HOST` | Bind address (default `127.0.0.1`) |


## Request format

Every request is a JSON-RPC 2.0 object sent as a single newline-terminated JSON line. If a token was configured, include it in `params`:

```json
{"jsonrpc": "2.0", "id": 1, "method": "status.ping", "params": {"token": "mytoken"}}
```

The server replies with a single JSON line:

```json
{"jsonrpc": "2.0", "id": 1, "result": {"ok": true, "page": "home"}}
```

On error:

```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found: foo"}}
```

### Minimal Python client

```python
import json, socket

def rpc(method, params=None, host='127.0.0.1', port=18716, token='mytoken'):
    p = dict(params or {})
    p['token'] = token
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall((json.dumps(req) + '\n').encode())
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                pass
    return {"error": {"message": "no response"}}
```


## Standard error codes

| Code | Meaning |
|---|---|
| `-32700` | Parse error |
| `-32600` | Invalid request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32603` | Internal / application error |

---

## Status methods

### `status.ping`

Check that the browser is alive.

**Result:** `{"ok": true, "page": "current/page/key"}`

---

### `status.capabilities`

List all supported methods.

**Result:** `{"version": 1, "read_only": true, "methods": ["status.ping", ...]}`

---

## Navigation methods

### `navigate.go_to_page`

**Params:** `{"key": "my/doc/key"}`  **Result:** `{"page": "my/doc/key"}`

### `navigate.back`

Go back in history.  **Result:** `{"page": "previous/key"}`

### `navigate.forward`

Go forward in history.  **Result:** `{"page": "next/key"}`

### `navigate.home`

Navigate to the root document (key `""`).  **Result:** `{"page": ""}`

### `navigate.refresh`

Reload the current document from the data source.  **Result:** `{"page": "current/key"}`

---

## Page methods

### `page.get_current`

**Result:** `{"key": "my/doc/key", "url": "http://..."}`

### `page.get_document_json`

Get the raw JSON document currently displayed.

**Result:** `{"document": {"title": "...", "content": [...]}}`

### `page.put_document_json`

Write a document to the data source. Reloads the page if the key matches the current page.

**Params:** `{"key": "my/doc/key", "document": {...}}`  **Result:** `{"status": "ok", "key": "my/doc/key"}`

### `page.get_rendered_text`

Get the plain text of the HTML view (non-sheet documents only).

**Result:** `{"text": "..."}`

---

## View methods

### `view.get_zoom`

**Result:** `{"zoom_percent": 100}`

### `view.zoom_in` / `view.zoom_out` / `view.zoom_reset`

No params.  **Result:** `{"zoom_percent": N}`

### `view.zoom_set`

**Params:** `{"zoom_percent": 150}`  **Result:** `{"zoom_percent": 150}`

### `view.scroll_pages`

Scroll the HTML view. Positive = down, negative = up.

**Params:** `{"pages": 1}`  **Result:** `{"scrolled": true, "pages": 1}`

---

## Selection methods

These work on the HTML view text selection. Not applicable when a runnable sheet is active.

### `selection.get`

**Result:** `{"start": ..., "end": ..., "text": "..."}`

### `selection.set_by_block_offsets`

**Params:** `{"start_block": 0, "start_offset": 5, "end_block": 1, "end_offset": 10}`  **Result:** `{"text": "..."}`

### `selection.native_select_word_by_text`

Find and select the first matching word.

**Params:** `{"text": "hello"}`  **Result:** `{"found": true, "text": "hello"}`

### `selection.native_select_line_by_text`

Find and select the line containing a string.

**Params:** `{"text": "hello"}`  **Result:** `{"found": true, "text": "hello world"}`

### `selection.read_text`

**Result:** `{"text": "..."}`

### `selection.write_text`

Replace the current selection.

**Params:** `{"text": "replacement"}`  **Result:** `{"ok": true}`

### `selection.set_and_capture`

**Params:** `{"start_block": 0, "start_offset": 0, "end_block": 0, "end_offset": 5}`  **Result:** `{"text": "...", "context": "..."}`

---

## Links methods

### `links.get_current`

Get all hyperlinks visible in the current page.

**Result:** `{"links": [{"text": "...", "href": "..."}]}`

---

## UI methods

### `ui.capture_screenshot`

**Params:** `{"path": "/absolute/path/to/file.png"}`  **Result:** `{"path": "..."}`

### `ui.capture_sixel`

**Params:** `{"path": "/absolute/path/to/file.sixel"}`  **Result:** `{"path": "..."}`

### `ui.set_window_size`

**Params:** `{"width": 1280, "height": 800}`  **Result:** `{"width": 1280, "height": 800}`

### `ui.quit`

Close the browser.  **Result:** `{"status": "quitting"}`

---

## Sheet methods

Sheet methods operate on the runnable sheet currently displayed. They return an error if no runnable document is active, or if a cell is currently executing.

Execution is **synchronous**: `sheet.cell.run` and `sheet.run_all` block until execution completes and return results directly.

### Cell identity

Every executable cell has a **cell key**: its `name` field if unique within the document, otherwise its zero-based codeblock index as a string (`"0"`, `"1"`, …).

Input fields are identified as `cell_key/N` where N is the zero-based `input()` occurrence index within that cell.

---

### `sheet.inputs.list`

List all detected input fields and their current values.

**Params:** `{}`

**Result:**
```json
{
  "doc_key": "my/sheet",
  "inputs": [
    {"id": "setup/0", "cell": "setup", "input_index": 0, "label": "Name: ", "value": ""}
  ]
}
```

### `sheet.inputs.set`

**Params:** `{"id": "setup/0", "value": "Alice"}`  **Result:** `{"id": "setup/0", "value": "Alice"}`

### `sheet.inputs.set_many`

Atomically set multiple fields. All IDs must be valid or none are changed.

**Params:** `{"values": {"setup/0": "Alice", "setup/1": "30"}}`  **Result:** `{"updated": [...], "count": 2}`

---

### `sheet.cell.run`

Run one cell synchronously.

**Params:** `{"cell": "setup"}` or `{"cell": 0}` (integer = codeblock index)

**Result:**
```json
{"cell": "setup", "status": "Done", "output": "value set to 42\n"}
```

`status` is `"Done"` on success, `"Error"` on exception. `output` contains captured stdout; on error it contains the Python traceback.

### `sheet.run_all`

Run all executable cells in document order. Stops at the first error.

**Params:** `{}`

**Result:**
```json
{"cells": [
  {"cell": "setup",      "status": "Done",  "output": "value set to 42\n"},
  {"cell": "compute",    "status": "Done",  "output": "double: 84\n"},
  {"cell": "error_cell", "status": "Error", "output": "Traceback ...\nZeroDivisionError: ...\n"}
]}
```

### `sheet.cell.get_output`

Get stored output for a cell without running it.

**Params:** `{"cell": "setup"}`  **Result:** `{"cell": "setup", "status": "Done", "output": "..."}`

### `sheet.get_state`

Get full sheet state.

**Params:** `{"include_outputs": false}`

**Result:**
```json
{
  "doc_key": "my/sheet",
  "is_running": false,
  "cells": [
    {"cell": "setup", "lang": "python", "input_count": 0, "inputs": [], "status": "Done"}
  ]
}
```

With `"include_outputs": true` each cell additionally contains `"output": "..."`.

### `sheet.clear_outputs`

Clear all cell outputs and status labels. **Kernel namespace is preserved.**

**Params:** `{}`  **Result:** `{"cleared": true}`

### `sheet.restart`

Reset the Python kernel. All variables cleared. Input values and outputs preserved in UI.

**Params:** `{}`  **Result:** `{"restarted": true}`

### `sheet.export_state`

Write current sheet state (inputs + output text) to a JSON file.

**Params:** `{"path": "/absolute/path/state.json"}`  **Result:** `{"path": "...", "cells": N}`

### `sheet.import_state`

Restore state from a previously exported JSON file. Does not re-execute any code.

**Params:** `{"path": "/absolute/path/state.json"}`  **Result:** `{"updated": N}`

---

## Example automation script

```python
import json, socket

TOKEN = 'mytoken'
PORT = 18716

def rpc(method, params=None):
    p = dict(params or {})
    p['token'] = TOKEN
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}
    with socket.create_connection(('127.0.0.1', PORT), timeout=10) as s:
        s.sendall((json.dumps(req) + '\n').encode())
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode())['result']
            except (json.JSONDecodeError, KeyError):
                pass

rpc('navigate.go_to_page', {'key': 'my/runnable-sheet'})
rpc('sheet.restart')
rpc('sheet.inputs.set', {'id': 'setup/0', 'value': 'Alice'})
result = rpc('sheet.cell.run', {'cell': 'setup'})
print(result['status'], result['output'])

results = rpc('sheet.run_all')
for cell in results['cells']:
    print(cell['cell'], cell['status'])
```
