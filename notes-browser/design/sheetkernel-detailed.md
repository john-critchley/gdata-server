# SheetKernel: Execution Reference

`SheetKernel` is the in-process Python execution engine for Runnable Sheets. One instance is created per open runnable document and shared across all its executable cells.

## What it does

- Maintains a single Python namespace shared by all cells in the sheet.
- Executes cell source code with `exec()` against that namespace.
- Captures stdout and stderr per execution (up to 64 KB each).
- Shims `input()` to read from supplied string values rather than blocking.
- Returns `(output_str, error_str, ok)` for each cell run.
- Resets by discarding the namespace and creating a new interpreter.

The kernel does not know about wxPython, widgets, or network sockets. It is pure execution logic.

## Interface

```python
class SheetKernel:
    def run_cell(self, name: str, source: str, inputs: list[str] = ()) -> tuple[str, str, bool]:
        ...

    def reset(self) -> None:
        ...

    def detect_inputs(self, source: str) -> list[str]:
        ...
```

### `run_cell(name, source, inputs)`

Runs one cell. Returns `(output, error, ok)`.

- `name`: stable cell identifier, used in tracebacks (`<cell name>`).
- `source`: Python source code.
- `inputs`: ordered string values for `input()` calls.
- `output`: captured stdout text.
- `error`: captured stderr + exception traceback (empty on success).
- `ok`: `True` if execution completed without exception.

Reentrant calls raise `RuntimeError`; the kernel is not thread-safe.

### `reset()`

Discards the current namespace and creates a fresh interpreter. Cannot be called while a cell is running.

### `detect_inputs(source)`

AST-scans `source` for `input(...)` calls and returns the prompt strings (or empty strings for calls without a literal prompt). Used by the UI to build input field labels at document-load time.

## Execution semantics

**Shared namespace.** Variables set in one cell are available to all later cells in the same session. This mirrors Jupyter notebook semantics.

**Compile then execute.** The kernel compiles `source` before executing it. `SyntaxError` is caught at compile time and returned as an error without running anything.

**Runtime exceptions.** Any exception during execution is caught (including `SystemExit` — it will not close the browser). The full traceback is appended to `error`, starting from the first frame inside the cell. `ok` is `False`.

**input() shim.** `builtins.input` is temporarily replaced during execution. The shim:
- Writes the prompt string to stdout (so prompts appear in captured output).
- Returns the next string from `inputs` in order.
- Raises `EOFError("SheetKernel input exhausted")` if more `input()` calls occur than supplied values, matching real Python EOF behaviour.

**Output cap.** stdout and stderr are each capped at 64 KB. If a stream exceeds this, the first 64 KB is kept and a truncation notice is appended.

**ANSI codes.** The browser strips ANSI escape sequences from output before displaying it in the output widget, since `wx.TextCtrl` is plain text.

**wx.YieldIfNeeded().** The kernel itself does not call any wx functions. The caller (the Run All loop in `RunnableSheetPanel`) calls `wx.YieldIfNeeded()` between cells to keep the UI responsive.

## Reset vs Clear Outputs

| Action | Kernel namespace | Visible outputs |
|---|---|---|
| Restart Kernel | **cleared** | preserved (but stale) |
| Clear Outputs | preserved | **cleared** |

After Restart, previously computed variables are gone. After Clear Outputs, they are still available for subsequent runs.

## Cell status values

| Label | Meaning |
|---|---|
| *(empty)* | Cell has not been run this session, or outputs were cleared |
| `Running…` | Execution in progress |
| `Done` | Last run completed without exception |
| `Error` | Last run raised an exception |

Status persists after a run until the next run or Clear Outputs.
