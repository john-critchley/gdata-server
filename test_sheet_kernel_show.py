"""
test_sheet_kernel_show.py — unit tests for SheetKernel.run_cell()'s show()
dispatch: the __show__ protocol, ndarray/DataFrame table conversion, the
plain-string fallback, and last_run_at tracking.

matplotlib Figure -> inline PNG is tested in test_sheet_image_output.py
(ImageOutput.__show__() in sheet_kernel.py — {"kind": "image", ...}, not
a JSONML node; an earlier version of this file tested a since-abandoned
JSONML-node approach to the same problem, since replaced).

No display/GUI required — SheetKernel executes cells and collects show()
items directly, independent of the wx UI layer.

Run: python -m pytest test_sheet_kernel_show.py -v
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "notes-browser"))

from sheet_kernel import SheetKernel  # noqa: E402


@pytest.fixture
def kernel():
    return SheetKernel()


# --- Regression coverage for the other show() branches, since this change
# reordered/touched the dispatch chain in show(). ---

def test_show_plain_string_falls_back_to_str(kernel):
    _, _, ok, show_items = kernel.run_cell("s", "show('hello')")
    assert ok
    assert show_items == ["hello"]


def test_show_dunder_show_protocol(kernel):
    source = """
class Thing:
    def __show__(self):
        return {'kind': 'html', 'content': '<b>hi</b>'}
show(Thing())
"""
    _, _, ok, show_items = kernel.run_cell("dunder", source)
    assert ok
    assert show_items == [{"kind": "html", "content": "<b>hi</b>"}]


def test_show_numpy_ndarray(kernel):
    source = """
import numpy as np
show(np.array([1, 2, 3]))
"""
    _, _, ok, show_items = kernel.run_cell("nd", source)
    assert ok
    assert len(show_items) == 1
    node = show_items[0]
    assert node[0] == "table"


def test_show_pandas_dataframe(kernel):
    source = """
import pandas as pd
show(pd.DataFrame({'a': [1, 2]}))
"""
    _, _, ok, show_items = kernel.run_cell("df", source)
    assert ok
    assert len(show_items) == 1
    node = show_items[0]
    assert node[0] == "table"


# --- last_run_at tracking (for the "Fix as new note" default name) ---

def test_last_run_at_is_none_before_first_run(kernel):
    assert kernel.list_cells() == []


def test_last_run_at_set_after_run(kernel):
    before = datetime.datetime.now()
    kernel.run_cell("a", "x = 1")
    after = datetime.datetime.now()
    info = kernel.list_cells()[0]
    assert info.last_run_at is not None
    assert before <= info.last_run_at <= after


def test_last_run_at_updates_on_rerun(kernel):
    kernel.run_cell("a", "x = 1")
    first = kernel.list_cells()[0].last_run_at
    kernel.run_cell("a", "x = 2")
    second = kernel.list_cells()[0].last_run_at
    assert second >= first


def test_last_run_at_is_set_even_on_error(kernel):
    """A failed cell still "ran" at a specific time — still useful for
    naming a fixed note even if that particular cell errored."""
    _, _, ok, _ = kernel.run_cell("a", "1 / 0")
    assert not ok
    info = kernel.list_cells()[0]
    assert info.last_run_at is not None
