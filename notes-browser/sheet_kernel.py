import ast
import builtins
import code
import contextlib
import io
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

    def get_output(self, name: str) -> Tuple[str, str, bool]:
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
    ) -> Tuple[str, str, bool]:
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
