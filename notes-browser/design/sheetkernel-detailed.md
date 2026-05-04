## SheetKernel detailed design

### Core model

`SheetKernel` is a single in-process Python execution context for one open Runnable Sheet.

- One `SheetKernel` instance per sheet.
- All cells in that sheet share one Python namespace.
- A cell is executed with `exec()` against that shared namespace.
- stdout, stderr, exceptions, input state, timing, and status are tracked per cell.
- The kernel is not sandboxed.
- The kernel is not thread-safe.
- V1 execution happens on the wxPython main thread.

Although the kernel owns a `code.InteractiveInterpreter`, the design should use its `locals` namespace directly and execute compiled code with `exec()`. This gives better control over exception handling than `InteractiveInterpreter.runcode()`, which catches exceptions and writes formatted tracebacks itself.

---

# 1. Class interface

```python
class SheetKernel:
    def __init__(self, *, output_limit_chars: int = 64 * 1024):
        ...

    def run_cell(
        self,
        name: str,
        source: str,
        inputs: list[str] | tuple[str, ...] = (),
    ) -> tuple[str, str, bool]:
        ...

    def reset(self) -> None:
        ...

    def list_cells(self) -> list[CellInfo]:
        ...

    def get_output(self, name: str) -> tuple[str, str, bool]:
        ...
```

## `__init__`

Creates a fresh shared Python namespace and empty cell state table.

```python
def __init__(self, *, output_limit_chars: int = 64 * 1024):
    self.output_limit_chars = output_limit_chars
    self.interpreter = code.InteractiveInterpreter()
    self.namespace = self.interpreter.locals

    self.namespace.setdefault("__name__", "__sheet__")
    self.namespace.setdefault("__doc__", None)

    self._cells: dict[str, CellRecord] = {}
    self._execution_count = 0
    self._running = False
```

Semantics:

- The namespace persists across cell executions.
- Variables/functions/classes defined in one cell are visible to later cells.
- `output_limit_chars` applies independently to stdout and stderr.
- `_execution_count` is a global monotonically increasing count, similar to Jupyter execution numbers.

---

## `run_cell(name, source, inputs) -> (output_str, error_str, ok)`

Executes one cell.

```python
output_str, error_str, ok = kernel.run_cell(
    name="cell-1",
    source="x = input('Name: ')\nprint('Hello', x)",
    inputs=["Alice"],
)
```

Semantics:

- `name` is the stable sheet-local cell identifier.
- `source` is the Python source code for the cell.
- `inputs` is the ordered list of strings returned by calls to `input()`.
- Returns:
  - `output_str`: captured stdout.
  - `error_str`: captured stderr plus any exception traceback.
  - `ok`: `True` if the cell compiled and executed without exception, else `False`.

Behavior:

- Compilation errors do not execute any code.
- Runtime exceptions stop the current cell but leave previous namespace state intact.
- Partial side effects from the failing cell remain, as in normal Python execution.
- The cell’s stored state is updated whether execution succeeds or fails.
- Reentrant execution is rejected.

```python
if self._running:
    raise RuntimeError("SheetKernel is already running")
```

---

## `reset()`

```python
def reset(self) -> None:
    ...
```

Semantics:

- Discards the current namespace.
- Discards all tracked cell outputs/errors/status.
- Resets the global execution count to zero.
- Creates a new `InteractiveInterpreter`.

```python
def reset(self) -> None:
    if self._running:
        raise RuntimeError("Cannot reset SheetKernel while a cell is running")

    self.interpreter = code.InteractiveInterpreter()
    self.namespace = self.interpreter.locals
    self.namespace.setdefault("__name__", "__sheet__")
    self.namespace.setdefault("__doc__", None)

    self._cells.clear()
    self._execution_count = 0
```

Rationale: kernel reset should behave like restarting a notebook kernel and clearing its execution history.

---

## `list_cells()`

```python
def list_cells(self) -> list[CellInfo]:
    ...
```

Returns immutable metadata snapshots for cells that have been run at least once.

```python
@dataclass(frozen=True)
class CellInfo:
    name: str
    run_count: int
    run_time_ms: float
    status: CellStatus
    has_output: bool
    has_error: bool
```

Semantics:

- Returned in first-seen order.
- Does not return full output text, to avoid large UI refresh payloads.
- Use `get_output(name)` to retrieve actual output/error text.

Example:

```python
[
    CellInfo(
        name="cell-1",
        run_count=1,
        run_time_ms=3.42,
        status=CellStatus.OK,
        has_output=True,
        has_error=False,
    )
]
```

---

## `get_output(name)`

```python
def get_output(self, name: str) -> tuple[str, str, bool]:
    ...
```

Returns the last stored result for a cell.

```python
output_str, error_str, ok = kernel.get_output("cell-1")
```

Semantics:

- Raises `KeyError` if the cell has never been run.
- Returns the most recent captured stdout, stderr/error text, and success flag.
- `ok` is derived from the stored status.

```python
def get_output(self, name: str) -> tuple[str, str, bool]:
    record = self._cells[name]
    return (
        record.last_output,
        record.last_error,
        record.status is CellStatus.OK,
    )
```

---

# 2. stdout/stderr capture

Use `contextlib.redirect_stdout` and `contextlib.redirect_stderr`, backed by capped text buffers.

```python
with contextlib.redirect_stdout(stdout_buf), \
     contextlib.redirect_stderr(stderr_buf), \
     patched_input(input_shim):
    exec(code_obj, self.namespace, self.namespace)
```

## Why `contextlib.redirect_stdout` instead of direct `sys.stdout` swap?

`contextlib.redirect_stdout` is preferred because:

- It is clearer and less error-prone.
- It restores `sys.stdout` using a context manager even if execution raises.
- It composes cleanly with `redirect_stderr` and the `input()` patch.
- Internally it still swaps `sys.stdout`, which is fine for this V1 design.

Direct assignment is equivalent in mechanism:

```python
old_stdout = sys.stdout
sys.stdout = buf
try:
    ...
finally:
    sys.stdout = old_stdout
```

But `contextlib.redirect_stdout` is the standard library abstraction for this exact pattern.

## Important limitation

`sys.stdout` and `sys.stderr` are process-global.

That means this design is only safe because V1 executes cells on the main thread and does not run multiple cells concurrently.

This capture does not reliably capture:

- C-level writes to file descriptor 1/2.
- Output from subprocesses.
- Output from other threads.

That is acceptable for V1.

---

# 3. Exception handling

The kernel should distinguish:

1. Compile-time `SyntaxError`.
2. Other compile-time source errors, such as null bytes.
3. Runtime exceptions.

## Compile-time `SyntaxError`

Compilation happens before execution:

```python
try:
    code_obj = compile(source, filename, "exec")
except SyntaxError as exc:
    error_buf.write(format_syntax_error(exc))
    ok = False
```

Use a synthetic filename containing the cell name:

```python
filename = f"<cell {name}>"
```

Example returned error:

```text
  File "<cell intro>", line 2
    if x =
         ^
SyntaxError: invalid syntax
```

For `SyntaxError`, do not include the internal `SheetKernel.run_cell()` stack. The useful information is the file name, line, caret, and message.

Implementation:

```python
def format_syntax_error(exc: SyntaxError) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc))
```

## Runtime exceptions

Runtime exceptions should return a full traceback string, not just the last line.

Example:

```text
Traceback (most recent call last):
  File "<cell compute>", line 3, in <module>
  File "<cell helpers>", line 2, in divide
ZeroDivisionError: division by zero
```

Rationale:

- Full tracebacks are much more useful when functions/classes are defined in earlier cells.
- The last line alone loses call-stack context.
- Runnable Sheets are code notes; debugging should feel like normal Python.

The traceback should be appended to stderr/error output.

Implementation should try to remove internal `SheetKernel` frames and start at the first frame whose filename looks like a cell filename.

```python
def format_runtime_exception(exc: BaseException) -> str:
    tb = exc.__traceback__
    original_tb = tb

    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if filename.startswith("<cell "):
            break
        tb = tb.tb_next

    if tb is None:
        tb = original_tb

    return "".join(traceback.format_exception(type(exc), exc, tb))
```

## Catching exceptions

Catch `BaseException`, not just `Exception`, so that `SystemExit` from user code does not terminate the notes browser.

```python
try:
    exec(code_obj, self.namespace, self.namespace)
except BaseException as exc:
    stderr_buf.write(format_runtime_exception(exc))
    ok = False
else:
    ok = True
```

This means user code like:

```python
exit()
```

does not close the application. It becomes a captured error.

---

# 4. `input()` shim

`input()` is implemented by temporarily replacing `builtins.input`.

Use the iterator pattern:

```python
input_iter = iter(inputs)

def input_shim(prompt: str = "") -> str:
    if prompt:
        print(prompt, end="")

    try:
        return str(next(input_iter))
    except StopIteration:
        raise EOFError("SheetKernel input exhausted")
```

## Prompt handling

Real Python `input(prompt)` writes the prompt to stdout before reading.

The shim should mimic that:

```python
if prompt:
    print(prompt, end="")
```

Because stdout is already redirected, the prompt appears in `output_str`.

Example:

```python
source = "name = input('Name: ')\nprint(name)"
inputs = ["Alice"]
```

Output:

```text
Name: Alice
```

Strictly speaking, the user did not type `Alice` into stdout, but showing prompts in output is useful and consistent with command-line expectations. If echoing input is desired later, that should be a separate UI decision. The V1 shim only captures the prompt, not the input value.

## Too many `input()` calls

Do not allow raw `StopIteration` to escape.

Do not silently return `""`.

Instead, convert exhausted input to `EOFError`.

```python
raise EOFError("SheetKernel input exhausted")
```

Rationale:

- Python’s real `input()` raises `EOFError` on end-of-input.
- Returning `""` hides bugs and can silently change program behavior.
- `StopIteration` is an implementation detail of the shim and should not leak.

Example:

```python
source = "a = input('a: ')\nb = input('b: ')"
inputs = ["one"]
```

Result:

```text
a:
Traceback (most recent call last):
  File "<cell example>", line 2, in <module>
EOFError: SheetKernel input exhausted
```

`ok` is `False`.

---

# 5. Output size cap

Use a per-stream cap of:

```python
64 * 1024
```

That is 65,536 characters for stdout and 65,536 characters for stderr independently.

```python
DEFAULT_OUTPUT_LIMIT_CHARS = 64 * 1024
```

Rationale:

- Enough for normal notes/code examples.
- Prevents accidentally rendering megabytes of text in wxPython controls.
- Keeps the browser responsive.

## Truncation behavior

When a stream exceeds the limit:

- Keep the first `output_limit_chars` characters.
- Discard the rest.
- Append a truncation notice when `getvalue()` is called.

stdout notice:

```text
[SheetKernel: stdout truncated after 65536 characters]
```

stderr notice:

```text
[SheetKernel: stderr truncated after 65536 characters]
```

The notice is appended after the retained text and may make the final returned string slightly larger than the cap.

Example:

```text
aaaaaaaaaa...
[SheetKernel: stdout truncated after 65536 characters]
```

Implementation:

```python
class CappedTextBuffer(io.TextIOBase):
    def __init__(self, *, limit: int, label: str):
        self.limit = limit
        self.label = label
        self._parts: list[str] = []
        self._length = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)

        original_len = len(s)
        remaining = self.limit - self._length

        if remaining > 0:
            kept = s[:remaining]
            self._parts.append(kept)
            self._length += len(kept)

        if original_len > remaining:
            self.truncated = True

        return original_len

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        text = "".join(self._parts)
        if self.truncated:
            text += (
                f"\n[SheetKernel: {self.label} truncated after "
                f"{self.limit} characters]\n"
            )
        return text
```

---

# 6. wxPython thread safety and `wx.Yield()`

The kernel itself should not call `wx.Yield()`.

`SheetKernel.run_cell()` should be pure execution logic. It should not know about wxPython widgets or event loops.

The Run All controller should call `wx.YieldIfNeeded()` between cells, not during cell execution.

## Recommended Run All loop

```python
def run_all(sheet, kernel):
    disable_run_buttons()
    set_run_all_active(True)

    try:
        for cell in sheet.cells:
            if run_cancelled() or sheet_window_destroyed():
                break

            mark_cell_running(cell)
            wx.YieldIfNeeded()

            if run_cancelled() or sheet_window_destroyed():
                break

            output, error, ok = kernel.run_cell(
                name=cell.name,
                source=cell.source,
                inputs=cell.inputs,
            )

            apply_cell_result(cell, output, error, ok)
            wx.YieldIfNeeded()

    finally:
        set_run_all_active(False)
        enable_run_buttons()
```

## Exactly where to yield

Call `wx.YieldIfNeeded()`:

1. After marking a cell as running, before executing it.
2. After applying that cell’s output/error/status, before moving to the next cell.

Do not call it inside `SheetKernel.run_cell()`.

Do not call it from stdout/stderr capture.

Do not call it from the `input()` shim.

## Between cells vs during execution

For V1:

- Yield between cells: yes.
- Yield during cell execution: no.

Long-running cells will still freeze the UI. That is accepted for V1.

## Why not yield during execution?

Calling `wx.Yield()` during execution creates a nested event loop while the Python namespace is mid-mutation and while process-global state is patched:

- `sys.stdout` is redirected.
- `sys.stderr` is redirected.
- `builtins.input` is patched.
- The cell may have partially modified globals.
- The user could click Restart, Run All, close the sheet, edit cells, or trigger another execution.

Risks:

- Reentrant `run_cell()` calls.
- Resetting the kernel while a cell is executing.
- Closing UI objects that the running controller expects to exist.
- Capturing unrelated UI/plugin output into the cell’s stdout.
- Hard-to-debug inconsistent namespace state.

If responsiveness during long-running cells becomes necessary, move execution to a worker thread or subprocess. Do not solve it with unrestricted `wx.Yield()` inside `exec()`.

---

# 7. Per-cell state tracking

Use an enum and record object.

```python
from enum import Enum
from dataclasses import dataclass, field


class CellStatus(Enum):
    NEVER_RUN = "never_run"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


@dataclass
class CellRecord:
    name: str
    last_output: str = ""
    last_error: str = ""
    run_count: int = 0
    run_time_ms: float = 0.0
    status: CellStatus = CellStatus.NEVER_RUN
```

Fields:

## `last_output`

Captured stdout from the last run.

Includes:

- `print()` output.
- Prompts written by `input(prompt)`.
- Anything written to `sys.stdout`.

Does not include stderr or tracebacks.

---

## `last_error`

Captured stderr from the last run.

Includes:

- Writes to `sys.stderr`.
- Syntax error messages.
- Runtime traceback strings.
- Input exhaustion traceback.

---

## `run_count`

The global execution count assigned to this cell’s most recent run.

Example:

```text
cell A run_count = 1
cell B run_count = 2
cell A run_count = 3
```

This is more useful than “number of times this specific cell has run”, because it gives a notebook-like execution ordering.

If per-cell attempt count is later needed, add another field such as `attempt_count`.

---

## `run_time_ms`

Elapsed wall-clock time for the most recent run, measured with `time.perf_counter()`.

Includes:

- Compilation time.
- Execution time.
- Exception formatting time.

Does not include UI rendering after `run_cell()` returns.

---

## `status`

One of:

```python
CellStatus.NEVER_RUN
CellStatus.RUNNING
CellStatus.OK
CellStatus.ERROR
```

`RUNNING` is mostly useful internally or if the UI observes state immediately before execution. Since V1 runs on the main thread, the UI generally updates status itself around `run_cell()`.

---

# Implementation sketch

```python
import builtins
import code
import contextlib
import io
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


DEFAULT_OUTPUT_LIMIT_CHARS = 64 * 1024


class CellStatus(Enum):
    NEVER_RUN = "never_run"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


@dataclass
class CellRecord:
    name: str
    last_output: str = ""
    last_error: str = ""
    run_count: int = 0
    run_time_ms: float = 0.0
    status: CellStatus = CellStatus.NEVER_RUN


@dataclass(frozen=True)
class CellInfo:
    name: str
    run_count: int
    run_time_ms: float
    status: CellStatus
    has_output: bool
    has_error: bool


class CappedTextBuffer(io.TextIOBase):
    def __init__(self, *, limit: int, label: str):
        self.limit = limit
        self.label = label
        self._parts: list[str] = []
        self._length = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)

        original_len = len(s)
        remaining = self.limit - self._length

        if remaining > 0:
            kept = s[:remaining]
            self._parts.append(kept)
            self._length += len(kept)

        if original_len > remaining:
            self.truncated = True

        return original_len

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        text = "".join(self._parts)
        if self.truncated:
            text += (
                f"\n[SheetKernel: {self.label} truncated after "
                f"{self.limit} characters]\n"
            )
        return text


@contextlib.contextmanager
def patched_input(func):
    old_input = builtins.input
    builtins.input = func
    try:
        yield
    finally:
        builtins.input = old_input


def format_syntax_error(exc: SyntaxError) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc))


def format_runtime_exception(exc: BaseException) -> str:
    tb = exc.__traceback__
    original_tb = tb

    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if filename.startswith("<cell "):
            break
        tb = tb.tb_next

    if tb is None:
        tb = original_tb

    return "".join(traceback.format_exception(type(exc), exc, tb))


class SheetKernel:
    def __init__(self, *, output_limit_chars: int = DEFAULT_OUTPUT_LIMIT_CHARS):
        self.output_limit_chars = output_limit_chars
        self.interpreter = code.InteractiveInterpreter()
        self.namespace = self.interpreter.locals
        self.namespace.setdefault("__name__", "__sheet__")
        self.namespace.setdefault("__doc__", None)

        self._cells: dict[str, CellRecord] = {}
        self._execution_count = 0
        self._running = False

    def reset(self) -> None:
        if self._running:
            raise RuntimeError("Cannot reset SheetKernel while a cell is running")

        self.interpreter = code.InteractiveInterpreter()
        self.namespace = self.interpreter.locals
        self.namespace.setdefault("__name__", "__sheet__")
        self.namespace.setdefault("__doc__", None)

        self._cells.clear()
        self._execution_count = 0

    def list_cells(self) -> list[CellInfo]:
        return [
            CellInfo(
                name=record.name,
                run_count=record.run_count,
                run_time_ms=record.run_time_ms,
                status=record.status,
                has_output=bool(record.last_output),
                has_error=bool(record.last_error),
            )
            for record in self._cells.values()
        ]

    def get_output(self, name: str) -> tuple[str, str, bool]:
        record = self._cells[name]
        return (
            record.last_output,
            record.last_error,
            record.status is CellStatus.OK,
        )

    def run_cell(
        self,
        name: str,
        source: str,
        inputs: Sequence[str] = (),
    ) -> tuple[str, str, bool]:
        if self._running:
            raise RuntimeError("SheetKernel is already running")

        record = self._cells.get(name)
        if record is None:
            record = CellRecord(name=name)
            self._cells[name] = record

        self._execution_count += 1
        record.run_count = self._execution_count
        record.status = CellStatus.RUNNING
        record.last_output = ""
        record.last_error = ""
        record.run_time_ms = 0.0

        stdout_buf = CappedTextBuffer(
            limit=self.output_limit_chars,
            label="stdout",
        )
        stderr_buf = CappedTextBuffer(
            limit=self.output_limit_chars,
            label="stderr",
        )

        filename = f"<cell {name}>"
        ok = False
        start = time.perf_counter()

        self._running = True

        try:
            try:
                code_obj = compile(source, filename, "exec")
            except SyntaxError as exc:
                stderr_buf.write(format_syntax_error(exc))
                ok = False
            except (ValueError, TypeError) as exc:
                stderr_buf.write("".join(traceback.format_exception_only(type(exc), exc)))
                ok = False
            else:
                input_iter = iter(inputs)

                def input_shim(prompt: str = "") -> str:
                    if prompt:
                        print(prompt, end="")

                    try:
                        return str(next(input_iter))
                    except StopIteration:
                        raise EOFError("SheetKernel input exhausted")

                with contextlib.redirect_stdout(stdout_buf), \
                     contextlib.redirect_stderr(stderr_buf), \
                     patched_input(input_shim):
                    try:
                        exec(code_obj, self.namespace, self.namespace)
                    except BaseException as exc:
                        stderr_buf.write(format_runtime_exception(exc))
                        ok = False
                    else:
                        ok = True

        finally:
            elapsed = time.perf_counter() - start

            record.last_output = stdout_buf.getvalue()
            record.last_error = stderr_buf.getvalue()
            record.run_time_ms = elapsed * 1000.0
            record.status = CellStatus.OK if ok else CellStatus.ERROR

            self._running = False

        return record.last_output, record.last_error, ok
```

This is the V1 complete implementation shape: simple, deterministic, in-process, shared-namespace, and easy to replace later with a subprocess-backed kernel while keeping the public interface stable.