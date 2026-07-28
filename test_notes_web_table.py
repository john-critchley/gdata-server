"""HTML table rendering tests for notes_web._render_block.

Covers the canonical {columns, rows} shape, inline markdown in cells, and the
sprint-row background colouring (rows whose Sprint column names "Sprint N" get
a stable colour).
"""

import re

import notes_web as w


def test_basic_table_headers_and_rows():
    html = w._render_block({"table": {"columns": ["A", "B"], "rows": [["1", "2"]]}})
    assert "<table" in html
    assert "<th>A</th><th>B</th>" in html
    assert "<td>1</td><td>2</td>" in html


def test_table_cell_markdown_is_rendered():
    html = w._render_block({"table": {"columns": ["C"], "rows": [["**bold**"]]}})
    assert "<strong>bold</strong>" in html


def test_sprint_rows_get_background_colour():
    html = w._render_block({"table": {
        "columns": ["Sprint", "Item"],
        "rows": [["Sprint 3", "x"], ["Backlog", "y"]],
    }})
    rows = re.findall(r"<tr[^>]*>", html)
    # header row + 2 body rows
    assert len(rows) == 3
    body = html.split("</thead>", 1)[1]
    sprint_row, plain_row = re.findall(r"<tr[^>]*>", body)
    assert "background-color" in sprint_row      # "Sprint 3" -> coloured
    assert "background-color" not in plain_row    # "Backlog" -> no colour


def test_no_sprint_column_means_no_colouring():
    html = w._render_block({"table": {"columns": ["A"], "rows": [["Sprint 3"]]}})
    body = html.split("</thead>", 1)[1]
    assert "background-color" not in body
