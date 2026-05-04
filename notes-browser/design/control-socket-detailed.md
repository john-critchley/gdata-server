# Runnable Sheets: Control Socket Integration and State Management Design

## 0. JSON-RPC conventions

All `sheet.*` methods use the existing control socket JSON-RPC envelope.

Request:

```json
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "method": "sheet.inputs.list",
  "params": {}
}
```

Success response:

```json
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "result": {}
}
```

Error response:

```json
{
  "jsonrpc": "2.0",
  "id": "req-123",
  "error": {
    "code": -32010,
    "message": "No runnable sheet is active",
    "data": {
      "symbol": "SHEET_NOT_ACTIVE"
    }
  }
}
```

Standard JSON-RPC errors remain available:

| Code | Meaning |
|---:|---|
| `-32700` | Parse error |
| `-32600` | Invalid request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32603` | Internal error |

Sheet-specific application errors:

| Code | Symbol | Meaning |
|---:|---|---|
| `-32010` | `SHEET_NOT_ACTIVE` | Current document is not a runnable sheet |
| `-32011` | `CELL_NOT_FOUND` | Cell reference does not resolve |
| `-32012` | `INPUT_NOT_FOUND` | Input field ID does not resolve |
| `-32013` | `CELL_NOT_EXECUTABLE` | Cell exists but is not executable |
| `-32014` | `SHEET_BUSY` | A cell or run-all job is already running |
| `-32015` | `UNSUPPORTED_LANGUAGE` | Executable cell language is not supported |
| `-32016` | `DUPLICATE_OR_AMBIGUOUS_CELL` | Cell reference is ambiguous |
| `-32017` | `INVALID_SHEET_STATE` | Sheet state object is corrupt or unavailable |

---

# 1. Cell and input identity

## 1.1 Canonical cell key

Every codeblock in the current document has a document codeblock index: `0`, `1`, `2`, etc., counting only codeblocks, not all content blocks.

Every executable cell receives a canonical cell key:

```text
canonical key = codeblock.name if present and unique
canonical key = string(document_codeblock_index) otherwise
```

Examples:

```json
{
  "name": "cell_1",
  "doc_index": 0,
  "key": "cell_1"
}
```

Unnamed cell at codeblock index `3`:

```json
{
  "name": null,
  "doc_index": 3,
  "key": "3"
}
```

If two cells share the same `name`, neither duplicate cell uses that name as its canonical key. Both fall back to their document codeblock index string. A warning is exposed through `sheet.get_state`.

## 1.2 Cell references accepted by RPC

Methods accepting a cell use this type:

```json
{
  "cell": "cell_1"
}
```

or:

```json
{
  "cell": 3
}
```

Rules:

- String values are resolved as canonical cell keys.
- Integer values are resolved as document codeblock indexes.
- Duplicate non-canonical names are not accepted.
- Missing cells return `CELL_NOT_FOUND`.

## 1.3 Input field IDs

Input field IDs are:

```text
cell_key/N
```

where:

- `cell_key` is the canonical cell key.
- `N` is the zero-based input occurrence inside that cell.

Example:

```text
cell_1/0
cell_1/1
3/0
```

---

# 2. Async execution model for control socket calls

## Decision

`sheet.cell.run` and `sheet.run_all` are asynchronous and non-blocking.

They return immediately with a job ID.

There are no callbacks and no event subscriptions in v1.

Clients poll with:

- `sheet.get_state`
- `sheet.cell.get_output`

This is the committed protocol because the control server supports TCP, UDP, and Unix sockets, and not all transports are suitable for long-lived callback streams.

## Job lifecycle

Job statuses:

```text
queued -> running -> completed
queued -> running -> failed
queued -> cancelled
```

Only one execution job may exist at a time.

When a run request is accepted:

1. The UI-thread handler validates the request.
2. A job object is created.
3. `SheetState.is_running` is set to `true` immediately.
4. The job is scheduled with `wx.CallLater(0, ...)`.
5. The RPC response is returned immediately with the job ID.
6. The actual Python execution happens later on the wx main thread.

While `SheetState.is_running == true`, additional `sheet.cell.run`, `sheet.run_all`, `sheet.restart`, and input mutation requests fail with `SHEET_BUSY`.

Completed jobs are retained in memory until one of:

- sheet restart,
- navigation away,
- page reload,
- job history exceeds `100` entries.

No job data is persisted to disk unless the user exports sheet state.

---

# 3. RPC method schemas

## 3.1 `sheet.inputs.list`

Lists all detected input fields in the current runnable sheet.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "sheet.inputs.list",
  "params": {}
}
```

`params` must be an object. No parameters are currently defined.

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "result": {
    "doc_key": "gdata-browser/runnable-sheets/example",
    "sheet": "My Sheet",
    "inputs": [
      {
        "id": "cell_1/0",
        "cell": "cell_1",
        "cell_index": 0,
        "input_index": 0,
        "label": "Enter name: ",
        "value": ""
      },
      {
        "id": "cell_1/1",
        "cell": "cell_1",
        "cell_index": 0,
        "input_index": 1,
        "label": "Enter age: ",
        "value": "30"
      }
    ]
  }
}
```

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `INVALID_SHEET_STATE` | Sheet state object is missing or inconsistent |
| `-32602` | `params` is not an object |

---

## 3.2 `sheet.inputs.set`

Sets one input field value.

Input values are always strings. Empty string is valid.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-2",
  "method": "sheet.inputs.set",
  "params": {
    "id": "cell_1/0",
    "value": "Alice"
  }
}
```

### Params

```json
{
  "id": "string, required",
  "value": "string, required"
}
```

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-2",
  "result": {
    "id": "cell_1/0",
    "cell": "cell_1",
    "input_index": 0,
    "label": "Enter name: ",
    "value": "Alice"
  }
}
```

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `INPUT_NOT_FOUND` | Input ID does not exist |
| `SHEET_BUSY` | A job is running or queued |
| `-32602` | Missing `id`, missing `value`, or non-string value |

---

## 3.3 `sheet.inputs.set_many`

Atomically sets multiple input values.

Either all values are applied, or none are applied.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-3",
  "method": "sheet.inputs.set_many",
  "params": {
    "values": {
      "cell_1/0": "Alice",
      "cell_1/1": "30"
    }
  }
}
```

### Params

```json
{
  "values": {
    "input_id": "string value"
  }
}
```

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-3",
  "result": {
    "updated": [
      {
        "id": "cell_1/0",
        "cell": "cell_1",
        "input_index": 0,
        "value": "Alice"
      },
      {
        "id": "cell_1/1",
        "cell": "cell_1",
        "input_index": 1,
        "value": "30"
      }
    ],
    "count": 2
  }
}
```

### Error response for invalid input IDs

No values are changed.

```json
{
  "jsonrpc": "2.0",
  "id": "req-3",
  "error": {
    "code": -32012,
    "message": "One or more input IDs do not exist",
    "data": {
      "symbol": "INPUT_NOT_FOUND",
      "invalid_ids": ["cell_9/0"]
    }
  }
}
```

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `INPUT_NOT_FOUND` | At least one input ID does not exist |
| `SHEET_BUSY` | A job is running or queued |
| `-32602` | `values` is missing, not an object, or contains non-string values |

---

## 3.4 `sheet.cell.run`

Runs one executable Python cell.

The call returns immediately with a job ID.

The actual execution result is later visible through `sheet.get_state` and `sheet.cell.get_output`.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-4",
  "method": "sheet.cell.run",
  "params": {
    "cell": "cell_1"
  }
}
```

or:

```json
{
  "jsonrpc": "2.0",
  "id": "req-4",
  "method": "sheet.cell.run",
  "params": {
    "cell": 0
  }
}
```

### Params

```json
{
  "cell": "string or integer, required"
}
```

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-4",
  "result": {
    "job": {
      "id": "job-20260504-000001",
      "type": "cell.run",
      "status": "queued",
      "cell": "cell_1",
      "created_at": "2026-05-04T12:00:00Z"
    }
  }
}
```

### Execution semantics

- The cell runs in the shared sheet kernel.
- The cell receives current input field values.
- stdout and stderr are captured.
- Previous output for that cell is replaced when execution completes.
- If Python raises an exception, the traceback is captured in output and the job status becomes `failed`.
- The RPC call itself still succeeds if the job was accepted.

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `CELL_NOT_FOUND` | Cell reference does not resolve |
| `CELL_NOT_EXECUTABLE` | Cell exists but has no `"exec": true` |
| `UNSUPPORTED_LANGUAGE` | Cell language is not `python`, `py`, or empty |
| `SHEET_BUSY` | Another job is already queued or running |
| `-32602` | Missing or invalid `cell` param |

---

## 3.5 `sheet.run_all`

Runs all executable Python cells in document order.

The call returns immediately with a job ID.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-5",
  "method": "sheet.run_all",
  "params": {}
}
```

### Params

No parameters.

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-5",
  "result": {
    "job": {
      "id": "job-20260504-000002",
      "type": "sheet.run_all",
      "status": "queued",
      "cells": ["cell_1", "cell_2", "3"],
      "created_at": "2026-05-04T12:01:00Z"
    }
  }
}
```

### Execution semantics

- Cells execute sequentially in document order.
- All cells share the same kernel namespace.
- Inputs are read from current field values at the moment each cell starts.
- If a cell fails, `run_all` stops immediately.
- The failed cell’s output contains the traceback.
- The job status becomes `failed`.
- Later cells are not executed.

### Empty runnable sheet

If the sheet has no executable Python cells, the job is accepted and immediately completes with an empty cell list.

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `SHEET_BUSY` | Another job is already queued or running |
| `UNSUPPORTED_LANGUAGE` | At least one executable cell has unsupported language |
| `-32602` | `params` is not an object |

---

## 3.6 `sheet.cell.get_output`

Gets the current stored output for one cell.

This method does not run code.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-6",
  "method": "sheet.cell.get_output",
  "params": {
    "cell": "cell_1"
  }
}
```

### Params

```json
{
  "cell": "string or integer, required"
}
```

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-6",
  "result": {
    "cell": "cell_1",
    "cell_index": 0,
    "status": "ok",
    "output": "Hello, Alice!\n",
    "output_truncated": false,
    "started_at": "2026-05-04T12:00:05Z",
    "finished_at": "2026-05-04T12:00:05Z",
    "execution_count": 1,
    "kernel_generation": 1,
    "stale": false
  }
}
```

Before a cell has ever run:

```json
{
  "jsonrpc": "2.0",
  "id": "req-6",
  "result": {
    "cell": "cell_1",
    "cell_index": 0,
    "status": "idle",
    "output": "",
    "output_truncated": false,
    "started_at": null,
    "finished_at": null,
    "execution_count": 0,
    "kernel_generation": null,
    "stale": false
  }
}
```

### Status values

| Status | Meaning |
|---|---|
| `idle` | Cell has not run in this session |
| `running` | Cell is currently executing |
| `ok` | Last execution completed successfully |
| `error` | Last execution raised an exception |
| `stale` | Output exists but belongs to a previous kernel generation |

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `CELL_NOT_FOUND` | Cell reference does not resolve |
| `-32602` | Missing or invalid `cell` param |

---

## 3.7 `sheet.get_state`

Returns the current sheet state.

This is the primary polling method for asynchronous jobs.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-7",
  "method": "sheet.get_state",
  "params": {
    "include_outputs": false,
    "include_jobs": true
  }
}
```

### Params

```json
{
  "include_outputs": "boolean, optional, default false",
  "include_jobs": "boolean, optional, default true"
}
```

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-7",
  "result": {
    "doc_key": "gdata-browser/runnable-sheets/example",
    "sheet": "My Sheet",
    "runnable": true,
    "kernel_generation": 1,
    "is_running": false,
    "active_job_id": null,
    "warnings": [],
    "cells": [
      {
        "cell": "cell_1",
        "name": "cell_1",
        "cell_index": 0,
        "lang": "python",
        "exec": true,
        "input_count": 2,
        "inputs": [
          {
            "id": "cell_1/0",
            "input_index": 0,
            "label": "Enter name: ",
            "value": "Alice"
          },
          {
            "id": "cell_1/1",
            "input_index": 1,
            "label": "Enter age: ",
            "value": "30"
          }
        ],
        "status": "ok",
        "execution_count": 1,
        "output_truncated": false,
        "started_at": "2026-05-04T12:00:05Z",
        "finished_at": "2026-05-04T12:00:05Z",
        "stale": false
      }
    ],
    "jobs": [
      {
        "id": "job-20260504-000001",
        "type": "cell.run",
        "status": "completed",
        "created_at": "2026-05-04T12:00:00Z",
        "started_at": "2026-05-04T12:00:05Z",
        "finished_at": "2026-05-04T12:00:05Z",
        "requested_cells": ["cell_1"],
        "current_cell": null,
        "completed_cells": ["cell_1"],
        "failed_cell": null,
        "error": null
      }
    ]
  }
}
```

If `include_outputs` is `true`, each cell object additionally includes:

```json
{
  "output": "Hello, Alice!\n"
}
```

### Running job example

```json
{
  "is_running": true,
  "active_job_id": "job-20260504-000002",
  "jobs": [
    {
      "id": "job-20260504-000002",
      "type": "sheet.run_all",
      "status": "running",
      "requested_cells": ["cell_1", "cell_2"],
      "current_cell": "cell_2",
      "completed_cells": ["cell_1"],
      "failed_cell": null,
      "error": null
    }
  ]
}
```

### Failed job example

```json
{
  "id": "job-20260504-000002",
  "type": "sheet.run_all",
  "status": "failed",
  "requested_cells": ["cell_1", "cell_2", "cell_3"],
  "current_cell": null,
  "completed_cells": ["cell_1"],
  "failed_cell": "cell_2",
  "error": {
    "type": "ZeroDivisionError",
    "message": "division by zero"
  }
}
```

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `-32602` | Params are not an object or booleans are invalid |

---

## 3.8 `sheet.restart`

Restarts the sheet kernel.

This resets the Python namespace but keeps current inputs and outputs.

### Request

```json
{
  "jsonrpc": "2.0",
  "id": "req-8",
  "method": "sheet.restart",
  "params": {}
}
```

### Params

No parameters.

### Success response

```json
{
  "jsonrpc": "2.0",
  "id": "req-8",
  "result": {
    "doc_key": "gdata-browser/runnable-sheets/example",
    "sheet": "My Sheet",
    "restarted": true,
    "kernel_generation": 2,
    "jobs_cleared": true,
    "outputs_preserved": true,
    "inputs_preserved": true
  }
}
```

### Restart semantics

On restart:

- A new `code.InteractiveInterpreter` is created.
- `kernel_generation` increments.
- All variables from previous cells are gone.
- Inputs are preserved.
- Outputs are preserved.
- Preserved outputs are marked stale because they belong to an older kernel generation.
- Job history is cleared.
- `execution_count` for cells is reset to `0`.

### Error cases

| Error | Condition |
|---|---|
| `SHEET_NOT_ACTIVE` | Current document is not runnable |
| `SHEET_BUSY` | A job is queued or running |
| `-32602` | Params are not an object |

---

# 4. SheetState class

## Decision

`SheetState` is a standalone model object owned by `NotesBrowser`.

It does not live on the wx panel.

The current runnable sheet panel receives a reference to the active `SheetState` and renders from it.

Reason:

- The control socket needs access to sheet state independently of widgets.
- The wx panel may be destroyed during navigation.
- Kernel lifetime should be controlled by the browser navigation layer, not by a child widget.

## Ownership

```python
class NotesBrowser(wx.Frame):
    def __init__(self, ...):
        self.sheet_state: SheetState | None = None
        self.current_doc_key: str | None = None
```

When a runnable document is loaded:

```python
self.sheet_state = SheetState.from_document(doc_key, document)
panel = RunnableSheetPanel(parent, self.sheet_state)
```

When navigating away:

```python
self.sheet_state.close()
self.sheet_state = None
```

## Class outline

```python
@dataclass
class InputSpec:
    id: str
    cell_key: str
    input_index: int
    label: str


@dataclass
class CellSpec:
    key: str
    name: str | None
    doc_index: int
    lang: str
    body: str
    body_hash: str
    exec: bool
    inputs: list[InputSpec]


@dataclass
class CellState:
    inputs: dict[int, str]
    output: str
    output_truncated: bool
    status: Literal["idle", "running", "ok", "error", "stale"]
    execution_count: int
    started_at: str | None
    finished_at: str | None
    last_error_type: str | None
    last_error_message: str | None
    last_run_kernel_generation: int | None


@dataclass
class JobState:
    id: str
    type: Literal["cell.run", "sheet.run_all"]
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    created_at: str
    started_at: str | None
    finished_at: str | None
    requested_cells: list[str]
    current_cell: str | None
    completed_cells: list[str]
    failed_cell: str | None
    error_type: str | None
    error_message: str | None
```

Main state object:

```python
class SheetState:
    current_doc_key: str
    sheet_title: str
    loaded_at: str

    kernel: code.InteractiveInterpreter
    kernel_generation: int

    cells: list[CellSpec]
    cell_by_key: dict[str, CellSpec]
    cell_by_doc_index: dict[int, CellSpec]

    cell_states: dict[str, CellState]

    is_running: bool
    active_job_id: str | None
    jobs: OrderedDict[str, JobState]

    warnings: list[str]

    max_output_chars: int
```

## Required methods

```python
class SheetState:
    @classmethod
    def from_document(cls, doc_key: str, document: dict) -> "SheetState":
        ...

    def close(self) -> None:
        ...

    def restart(self) -> None:
        ...

    def resolve_cell(self, ref: str | int) -> CellSpec:
        ...

    def list_inputs(self) -> list[dict]:
        ...

    def set_input(self, input_id: str, value: str) -> dict:
        ...

    def set_inputs_many(self, values: dict[str, str]) -> list[dict]:
        ...

    def create_cell_run_job(self, cell_ref: str | int) -> JobState:
        ...

    def create_run_all_job(self) -> JobState:
        ...

    def execute_job(self, job_id: str) -> None:
        ...

    def get_output(self, cell_ref: str | int) -> dict:
        ...

    def to_rpc_state(
        self,
        include_outputs: bool = False,
        include_jobs: bool = True,
    ) -> dict:
        ...

    def to_export_json(self) -> dict:
        ...

    def import_export_json(self, data: dict) -> ImportSummary:
        ...
```

## Kernel details

`SheetState.kernel` is:

```python
code.InteractiveInterpreter(locals={})
```

Execution uses one shared namespace for the whole sheet.

Inputs are shimmed by temporarily replacing `builtins.input`.

stdout and stderr are captured with `io.StringIO`.

The shim receives the precomputed input values for the cell:

```python
def input_shim(prompt=""):
    try:
        return values[next_index]
    except IndexError:
        return ""
```

If the code calls `input()` more times than statically detected, the shim returns `""`.

## Output truncation

`SheetState.max_output_chars` is fixed at:

```python
MAX_OUTPUT_CHARS = 200_000
```

If captured output exceeds this size:

- output is truncated,
- `output_truncated` is set to `true`,
- the following marker is appended:

```text
\n[output truncated at 200000 characters]\n
```

---

# 5. wx control socket dispatch integration

The existing server dispatches onto the wx main thread using `wx.CallAfter`.

For non-running methods:

```python
def handle_sheet_inputs_set(params):
    state = browser.require_sheet_state()
    return state.set_input(params["id"], params["value"])
```

For run methods, the handler must not execute the cell inline.

Correct flow:

```python
def handle_sheet_cell_run(params):
    state = browser.require_sheet_state()

    if state.is_running:
        raise SheetBusyError()

    job = state.create_cell_run_job(params["cell"])

    # create_cell_run_job sets state.is_running = True immediately

    wx.CallLater(0, state.execute_job, job.id)

    browser.refresh_sheet_panel()

    return {
        "job": job.to_rpc()
    }
```

`state.execute_job()` runs on the wx main thread.

After each cell finishes, it updates:

- `CellState`
- `JobState`
- `SheetState.is_running`
- `SheetState.active_job_id`
- the rendered sheet panel

Because execution is synchronous on the main thread, the UI may be unresponsive while a long cell runs. This is accepted for v1.

---

# 6. Export/import UI

## 6.1 Export file format

The export format is JSON.

The file extension is:

```text
.sheetstate.json
```

The default filename is:

```text
<sanitized-sheet-title>-<YYYYMMDD-HHMMSS>.sheetstate.json
```

Example:

```text
My-Sheet-20260504-120000.sheetstate.json
```

Export writes this structure:

```json
{
  "format": "runnable-sheet-state",
  "version": 1,
  "sheet": "My Sheet",
  "doc_key": "gdata-browser/runnable-sheets/example",
  "exported": "2026-05-04T12:00:00Z",
  "cells": {
    "cell_1": {
      "inputs": {
        "0": "Alice",
        "1": "30"
      },
      "output": "Hello, Alice!\n"
    },
    "cell_2": {
      "inputs": {},
      "output": "Result: 42\n"
    }
  }
}
```

The high-level required keys remain:

```json
{
  "sheet": "...",
  "exported": "...",
  "cells": {}
}
```

`format`, `version`, and `doc_key` are additional metadata.

## 6.2 Export UI flow

Toolbar button:

```text
Export State…
```

Enabled only when a runnable sheet is active.

Flow:

1. User clicks `Export State…`.
2. Browser opens `wx.FileDialog` with `FD_SAVE | FD_OVERWRITE_PROMPT`.
3. Wildcard:

   ```text
   Sheet state (*.sheetstate.json)|*.sheetstate.json|JSON files (*.json)|*.json|All files (*.*)|*.*
   ```

4. Default directory is the last used export directory.
5. Default filename is generated from title and timestamp.
6. On confirmation, write UTF-8 pretty-printed JSON with two-space indentation.
7. Show status bar message:

   ```text
   Exported sheet state to /path/file.sheetstate.json
   ```

Export does not write to the notes server.

Export is disabled while a job is running.

## 6.3 Import UI flow

Toolbar button:

```text
Import State…
```

Enabled only when a runnable sheet is active and no job is running.

Flow:

1. User clicks `Import State…`.
2. Browser opens `wx.FileDialog` with `FD_OPEN | FD_FILE_MUST_EXIST`.
3. Wildcard:

   ```text
   Sheet state (*.sheetstate.json)|*.sheetstate.json|JSON files (*.json)|*.json|All files (*.*)|*.*
   ```

4. Browser reads UTF-8 JSON.
5. Browser validates format and version.
6. Browser applies matching cell state.
7. Browser refreshes the sheet panel.
8. Browser displays an import summary dialog.

## 6.4 Import version handling

Supported version:

```json
"version": 1
```

Rules:

| File condition | Behavior |
|---|---|
| Missing `version` | Treat as legacy v1 |
| `"version": 1` | Import |
| `"version"` not integer | Reject |
| `"version" < 1` | Reject |
| `"version" > 1` | Reject |
| Missing `format` | Treat as legacy v1 |
| `"format": "runnable-sheet-state"` | Import |
| Other `format` value | Reject |

Future versions are rejected, not partially imported.

Rejection dialog:

```text
Cannot import sheet state.

Unsupported sheet state version: 2.
This browser supports version 1.
```

## 6.5 Import cell matching

Cells are matched by canonical cell key.

For each imported cell key:

- If the key exists in the current sheet:
  - matching input indexes are applied,
  - output is applied if present and string.
- If the key does not exist:
  - it is ignored,
  - it is listed in the summary as superfluous.

For each current sheet cell missing from the file:

- existing input values remain unchanged,
- existing output remains unchanged,
- it is listed in the summary as missing.

For imported input indexes:

- If input index exists in current cell, value is applied.
- If input index does not exist, it is ignored and listed in the summary.
- Non-string input values reject the whole import.

Import does not alter the Python kernel namespace.

Imported outputs are marked stale:

```json
{
  "status": "stale",
  "last_run_kernel_generation": null
}
```

## 6.6 Import summary dialog

After successful import:

```text
Imported sheet state.

Updated cells: 2
Missing cells left unchanged: 1
Ignored unknown cells: 1
Ignored unknown inputs: 2
```

If the file’s `sheet` title differs from the current sheet title, import still proceeds, and the summary includes:

```text
Warning: file was exported from sheet "Old Title".
Current sheet is "New Title".
```

---

# 7. Navigation away from a sheet

## Decision

Navigation away from a runnable sheet resets immediately.

There is no prompt.

The kernel and session state are destroyed when the current document changes away from the runnable sheet.

This applies when navigating to:

- a non-runnable document,
- another runnable document,
- browser home,
- search results,
- reload.

## Behavior

On navigation away:

```python
if self.sheet_state is not None:
    self.sheet_state.close()
    self.sheet_state = None
```

Destroyed state includes:

- kernel namespace,
- input values,
- captured outputs,
- job history,
- stale output markers,
- imported state.

Nothing is written to the server.

If the user wants to preserve inputs or outputs, they must use `Export State…` before navigating.

## Back navigation

If the user navigates back to the same runnable document, a fresh `SheetState` is created.

The kernel is empty.

Inputs are empty.

Outputs are initialized only from the JSONHTL document’s own `codeblock.output` values, if present.

Previous session-only outputs are gone unless the user imports a `.sheetstate.json` file.

## Navigation while running

Because execution runs synchronously on the wx main thread, ordinary UI navigation cannot occur during execution.

If a control-socket navigation command is received while `SheetState.is_running == true`, it must fail with `SHEET_BUSY`.

Queued-but-not-started jobs are also treated as running for this purpose.