import ast
import builtins
import code
import contextlib
import io
import json
import math
import queue as _queue_mod
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence, List, Tuple, Dict, Optional, Any

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
        self._parts: List[str] = []
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
        else:
            if original_len > 0:
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


_BUILTIN_MISSING = object()


@contextlib.contextmanager
def patched_builtin(name, func):
    """Temporarily set builtins.<name> = func; restore (or delete) on exit."""
    old = getattr(builtins, name, _BUILTIN_MISSING)
    setattr(builtins, name, func)
    try:
        yield
    finally:
        if old is _BUILTIN_MISSING:
            try:
                delattr(builtins, name)
            except AttributeError:
                pass
        else:
            setattr(builtins, name, old)


# ---------------------------------------------------------------------------
# show() support
# ---------------------------------------------------------------------------

class HtmlOutput:
    """Wrap an HTML string for display in a cell output panel.

    Usage in a cell::

        show(HtmlOutput('<b>hello</b>'))
        show(HtmlOutput(html_heatmap(data, ...)))
    """
    def __init__(self, html: str):
        self._html = html

    def __show__(self):
        return {'kind': 'html', 'content': self._html}


class _ShowCollector:
    """Accumulates items produced by show() calls during cell execution."""
    def __init__(self):
        self.items: List[Any] = []

    def append(self, item: Any) -> None:
        self.items.append(item)


def _coerce_numeric_sequence(values: Any, label: str) -> List[float]:
    """Convert values into a flat numeric list suitable for plotting."""
    if isinstance(values, (str, bytes, dict)):
        raise TypeError(f"plot({label}) expects a numeric sequence, got {type(values).__name__}")
    if isinstance(values, (int, float)):
        values = [values]
    else:
        try:
            values = list(values)
        except TypeError as exc:
            raise TypeError(
                f"plot({label}) expects an iterable numeric sequence"
            ) from exc

    out: List[float] = []
    for i, v in enumerate(values):
        try:
            fv = float(v)
        except Exception as exc:
            raise TypeError(
                f"plot({label}) value at index {i} is not numeric: {v!r}"
            ) from exc
        if not math.isfinite(fv):
            raise ValueError(f"plot({label}) value at index {i} is not finite: {v!r}")
        out.append(fv)
    return out


def _make_plot_func(collector: "_ShowCollector"):
    """Build plot() builtin that records numeric series data for UI rendering.

    Signatures:
        plot(y)               – single series, x = 0,1,2,...
        plot(x, y)            – single series
        plot(x, y1, y2, ...)  – multiple series, shared x
        plot(y1, y2, ...)     – multiple series, x = 0,1,2,... (all y must be same length)
    """
    def _is_sequence(v: Any) -> bool:
        return not isinstance(v, (str, bytes, dict)) and hasattr(v, '__iter__')

    def plot(*args: Any, title: str = "", x_label: str = "x", y_label: str = "y") -> Dict[str, Any]:
        if len(args) == 0:
            raise TypeError("plot() requires at least one argument")

        # Determine whether first arg is x or y by checking if second arg is also a sequence
        series_list = []
        if len(args) == 1:
            y_vals = _coerce_numeric_sequence(args[0], "y")
            x_vals = [float(i) for i in range(len(y_vals))]
            series_list = [{"name": "series0", "x": x_vals, "y": y_vals}]
        elif len(args) == 2:
            x_vals = _coerce_numeric_sequence(args[0], "x")
            y_vals = _coerce_numeric_sequence(args[1], "y")
            if len(x_vals) != len(y_vals):
                raise ValueError(
                    f"plot(x, y) requires equal lengths, got {len(x_vals)} and {len(y_vals)}"
                )
            series_list = [{"name": "series0", "x": x_vals, "y": y_vals}]
        else:
            # 3+ args: either plot(x, y1, y2, ...) or plot(y1, y2, y3, ...)
            # Heuristic: if first arg length equals second arg length, treat as plot(x, y1, y2, ...)
            first = _coerce_numeric_sequence(args[0], "arg0")
            second = _coerce_numeric_sequence(args[1], "arg1")
            if len(first) == len(second):
                # treat first as x, rest as y-series
                x_vals = first
                y_arrays = [second] + [_coerce_numeric_sequence(a, f"y{i+1}") for i, a in enumerate(args[2:])]
            else:
                # treat all as y-series, auto-generate x
                y_arrays = [first, second] + [_coerce_numeric_sequence(a, f"y{i+2}") for i, a in enumerate(args[2:])]
                n = max(len(y) for y in y_arrays)
                x_vals = [float(i) for i in range(n)]
            for idx, y_vals in enumerate(y_arrays):
                if len(y_vals) != len(x_vals):
                    raise ValueError(
                        f"plot series {idx} length {len(y_vals)} != x length {len(x_vals)}"
                    )
                series_list.append({"name": f"series{idx}", "x": x_vals, "y": y_vals})

        spec = {
            "kind": "plot",
            "title": str(title or "Plot"),
            "x_label": str(x_label),
            "y_label": str(y_label),
            "series": series_list,
        }
        collector.append(spec)
        return spec

    return plot


def _make_jsonml_for_ndarray(arr: Any) -> Any:
    """Convert a numpy ndarray to a JSONML table."""
    dtype = str(arr.dtype)
    shape = arr.shape
    ndim = arr.ndim
    if ndim == 1:
        dims = f"({shape[0]},)"
        caption = f"{dtype} {dims}"
        row = ["tr", {}] + [["td", {}, str(v)] for v in arr]
        return ["table", {}, ["caption", {}, caption], row]
    elif ndim == 2:
        dims = f"({shape[0]}×{shape[1]})"
        caption = f"{dtype} {dims}"
        rows = []
        for r in arr:
            rows.append(["tr", {}] + [["td", {}, str(v)] for v in r])
        return ["table", {}, ["caption", {}, caption]] + rows
    else:
        # 3D+: caption + first 2D slice
        dims = "×".join(str(s) for s in shape)
        caption = f"{dtype} ({dims}) — first slice:"
        leading = (0,) * (ndim - 2)
        sub = arr[leading]
        tbl = _make_jsonml_for_ndarray(sub)
        return ["div", {}, ["div", {}, caption], tbl]


def _make_jsonml_for_dataframe(obj: Any) -> Any:
    """Convert a pandas DataFrame or Series to a JSONML table."""
    cls = type(obj).__name__
    if cls == "DataFrame":
        cols = list(obj.columns)
        head = ["tr", {}] + [["th", {}, "index"]] + [["th", {}, str(c)] for c in cols]
        rows = [head]
        for idx, row in obj.iterrows():
            rows.append(["tr", {}] + [["td", {}, str(idx)]] + [["td", {}, str(row[c])] for c in cols])
        return ["table", {}] + rows
    else:  # Series
        head = ["tr", {}, ["th", {}, "index"], ["th", {}, "value"]]
        rows = [head]
        for idx, val in obj.items():
            rows.append(["tr", {}, ["td", {}, str(idx)], ["td", {}, str(val)]])
        return ["table", {}] + rows


def _make_show_func(collector: "_ShowCollector"):
    """Build the show() callable injected into cell execution."""
    def show(obj: Any) -> None:
        # 1. __show__ protocol
        if hasattr(obj, "__show__") and callable(obj.__show__):
            try:
                result = obj.__show__()
            except Exception as exc:
                collector.append(f"[show: __show__() raised {exc!r}]")
                return
            collector.append(result)
            return
        # 2. numpy ndarray
        try:
            import numpy as _np
            if isinstance(obj, _np.ndarray):
                collector.append(_make_jsonml_for_ndarray(obj))
                return
        except ImportError:
            pass
        # 3. pandas DataFrame / Series
        try:
            import pandas as _pd
            if isinstance(obj, (_pd.DataFrame, _pd.Series)):
                collector.append(_make_jsonml_for_dataframe(obj))
                return
        except ImportError:
            pass
        # 4. fallback: str()
        try:
            collector.append(str(obj))
        except Exception as exc:
            collector.append(f"[show: str() raised {exc!r}]")
    return show


def format_syntax_error(exc: SyntaxError) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc))


def format_runtime_exception(exc: BaseException) -> str:
    tb = getattr(exc, '__traceback__', None)
    original_tb = tb
    # skip frames that are not from the relevant cell
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
        self._cells: Dict[str, CellRecord] = {}
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

    def list_cells(self) -> List[CellInfo]:
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

    def get_output(self, name: str) -> Tuple[str, str, bool, list]:
        record = self._cells[name]
        return (
            record.last_output,
            record.last_error,
            record.status is CellStatus.OK,
            [],
        )

    def run_cell(
        self,
        name: str,
        source: str,
        inputs: Sequence[str] = (),
    ) -> Tuple[str, str, bool, list]:
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
        show_collector = _ShowCollector()
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

                show_func = _make_show_func(show_collector)
                plot_func = _make_plot_func(show_collector)
                with contextlib.redirect_stdout(stdout_buf), \
                     contextlib.redirect_stderr(stderr_buf), \
                     patched_input(input_shim), \
                     patched_builtin("show", show_func), \
                     patched_builtin("plot", plot_func), \
                     patched_builtin("HtmlOutput", HtmlOutput):
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
        return record.last_output, record.last_error, ok, list(show_collector.items)


def detect_inputs(source: str) -> List[str]:
    """Detect input() calls and extract prompt strings (best effort)."""
    try:
        # Parse the code into an AST tree
        tree = ast.parse(source)
    except SyntaxError:
        return []

    prompts: List[str] = []

    class InputVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> Any:
            # Check if this is an input() call
            if isinstance(node.func, ast.Name) and node.func.id == "input":
                if not node.args:
                    prompts.append("")
                else:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        prompts.append(arg.value)
                    else:
                        prompts.append("")
            self.generic_visit(node)

    InputVisitor().visit(tree)
    return prompts
