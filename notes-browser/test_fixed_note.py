"""
test_fixed_note.py — tests for RunnableSheetPanel's "Fix as New Note"
conversion logic in sheet_ui.py: turning a runnable note's current cell
outputs into a static, non-runnable document.

Requires a display (X11/Wayland) since RunnableSheetPanel is a real wx
widget tree. Skipped automatically if none is available.

Run: python -m pytest test_fixed_note.py -v
"""
import datetime
import os

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="No display available — wx requires X11/Wayland",
)

import wx  # noqa: E402

from sheet_ui import RunnableSheetPanel  # noqa: E402
from notes_browser_runnable import NotesHTMLRenderer  # noqa: E402

TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAEElEQVR4nGP8z4AATAxEcQAz0QEHOoQ+uAAAAABJRU5ErkJggg=="


class FakeDataSource:
    def __init__(self):
        self.writes = []

    def write(self, key, doc):
        self.writes.append((key, doc))


class FakeBrowser:
    def __init__(self):
        self.renderer = NotesHTMLRenderer()
        self.data_source = FakeDataSource()
        self._nav_locked = False

    def _navigate_to(self, key):
        pass


@pytest.fixture(scope="module")
def app():
    return wx.App(False)


@pytest.fixture
def frame(app):
    f = wx.Frame(None)
    yield f
    f.Destroy()


def _make_panel(frame, content):
    browser = FakeBrowser()
    data = {"title": "Test note", "runnable": True, "content": content}
    panel = RunnableSheetPanel(frame, browser, "test/original", data)
    return panel, browser


def test_prose_blocks_pass_through_unchanged(frame):
    content = [
        {"heading": {"level": 1, "text": "Title"}},
        {"para": ["Some intro text."]},
    ]
    panel, _ = _make_panel(frame, content)
    doc = panel._build_fixed_document()
    assert doc["content"] == content


def test_runnable_key_is_dropped(frame):
    panel, _ = _make_panel(frame, [{"para": ["x"]}])
    doc = panel._build_fixed_document()
    assert "runnable" not in doc


def test_exec_cell_becomes_collapsed_details_with_code(frame):
    content = [
        {"codeblock": {"lang": "python", "exec": True, "name": "setup", "body": "x = 1\nprint(x)"}},
    ]
    panel, _ = _make_panel(frame, content)
    panel.kernel.run_cell("setup", "x = 1\nprint(x)")
    panel.cell_panels["setup"].set_output("1\n", ok=True)

    doc = panel._build_fixed_document()
    details_blocks = [b for b in doc["content"] if "details" in b]
    assert len(details_blocks) == 1
    d = details_blocks[0]["details"]
    assert d["summary"] == "Cell: setup (code)"
    assert d["content"] == [{"codeblock": {"lang": "python", "body": "x = 1\nprint(x)"}}]


def test_exec_cell_stdout_becomes_text_codeblock(frame):
    content = [{"codeblock": {"lang": "python", "exec": True, "name": "setup", "body": "print('hi')"}}]
    panel, _ = _make_panel(frame, content)
    panel.kernel.run_cell("setup", "print('hi')")
    panel.cell_panels["setup"].set_output("hi\n", ok=True)

    doc = panel._build_fixed_document()
    text_blocks = [b for b in doc["content"] if b.get("codeblock", {}).get("lang") == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["codeblock"]["body"] == "hi\n"


def test_show_image_item_becomes_image_block(frame):
    content = [{"codeblock": {"lang": "python", "exec": True, "name": "plot", "body": "pass"}}]
    panel, _ = _make_panel(frame, content)
    panel.kernel.run_cell("plot", "pass")
    show_items = [["img", {"src": f"data:image/png;base64,{TINY_PNG_B64}"}]]
    panel.cell_panels["plot"].set_output("", ok=True, show_items=show_items)

    doc = panel._build_fixed_document()
    image_blocks = [b for b in doc["content"] if "image" in b]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image"] == {"format": "png", "data": TINY_PNG_B64}


def test_show_table_item_with_header_becomes_table_block(frame):
    # Matches _make_jsonml_for_dataframe's shape: header row of <th>, then <td> rows.
    jsonml_table = [
        "table", {},
        ["tr", {}, ["th", {}, "index"], ["th", {}, "a"]],
        ["tr", {}, ["td", {}, "0"], ["td", {}, "1"]],
        ["tr", {}, ["td", {}, "1"], ["td", {}, "2"]],
    ]
    content = [{"codeblock": {"lang": "python", "exec": True, "name": "df", "body": "pass"}}]
    panel, _ = _make_panel(frame, content)
    panel.kernel.run_cell("df", "pass")
    panel.cell_panels["df"].set_output("", ok=True, show_items=[jsonml_table])

    doc = panel._build_fixed_document()
    table_blocks = [b for b in doc["content"] if "table" in b]
    assert len(table_blocks) == 1
    assert table_blocks[0]["table"] == {
        "columns": ["index", "a"],
        "rows": [["0", "1"], ["1", "2"]],
    }


def test_show_table_item_without_header_ndarray_case(frame):
    jsonml_table = [
        "table", {}, ["caption", {}, "int64 (2, 2)"],
        ["tr", {}, ["td", {}, "1"], ["td", {}, "2"]],
        ["tr", {}, ["td", {}, "3"], ["td", {}, "4"]],
    ]
    content = [{"codeblock": {"lang": "python", "exec": True, "name": "arr", "body": "pass"}}]
    panel, _ = _make_panel(frame, content)
    panel.kernel.run_cell("arr", "pass")
    panel.cell_panels["arr"].set_output("", ok=True, show_items=[jsonml_table])

    doc = panel._build_fixed_document()
    table_blocks = [b for b in doc["content"] if "table" in b]
    assert len(table_blocks) == 1
    assert table_blocks[0]["table"]["columns"] == []
    assert table_blocks[0]["table"]["rows"] == [["1", "2"], ["3", "4"]]


def test_unconvertible_show_item_kind_is_dropped_not_crashed(frame):
    content = [{"codeblock": {"lang": "python", "exec": True, "name": "p", "body": "pass"}}]
    panel, _ = _make_panel(frame, content)
    panel.kernel.run_cell("p", "pass")
    plot_spec = {"kind": "plot", "title": "t", "series": []}
    panel.cell_panels["p"].set_output("", ok=True, show_items=[plot_spec])

    doc = panel._build_fixed_document()
    # No crash, and nothing spurious got added for the unconvertible item.
    kinds = [set(b.keys()) for b in doc["content"]]
    assert {"image"} not in kinds
    assert {"table"} not in kinds


def test_suggest_fixed_name_uses_last_run_at_not_now(frame):
    content = [{"codeblock": {"lang": "python", "exec": True, "name": "a", "body": "pass"}}]
    panel, _ = _make_panel(frame, content)
    panel.kernel.run_cell("a", "pass")
    forced_time = datetime.datetime(2026, 7, 10, 8, 15)
    panel.kernel._cells["a"].last_run_at = forced_time

    name = panel._suggest_fixed_name()
    assert name == "test/original-2026-07-10-0815"


def test_suggest_fixed_name_falls_back_to_now_if_never_run(frame):
    panel, _ = _make_panel(frame, [{"para": ["x"]}])
    name = panel._suggest_fixed_name()
    assert name.startswith("test/original-")


def test_fix_as_new_note_writes_via_data_source(frame):
    content = [{"codeblock": {"lang": "python", "exec": True, "name": "a", "body": "print(1)"}}]
    panel, browser = _make_panel(frame, content)
    panel.kernel.run_cell("a", "print(1)")
    panel.cell_panels["a"].set_output("1\n", ok=True)

    result = panel.fix_as_new_note("test/fixed-copy")

    assert result == {"key": "test/fixed-copy"}
    assert len(browser.data_source.writes) == 1
    key, written_doc = browser.data_source.writes[0]
    assert key == "test/fixed-copy"
    assert "runnable" not in written_doc


def test_fix_as_new_note_uses_suggested_name_if_none_given(frame):
    panel, browser = _make_panel(frame, [{"para": ["x"]}])
    result = panel.fix_as_new_note()
    assert result["key"].startswith("test/original-")
    assert browser.data_source.writes[0][0] == result["key"]


def test_fix_as_new_note_rejects_empty_key(frame):
    panel, _ = _make_panel(frame, [{"para": ["x"]}])
    with pytest.raises(ValueError):
        panel.fix_as_new_note("   ")
