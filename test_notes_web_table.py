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


def test_table_cell_inline_list_internal_link():
    # A cell may be a list of inline elements (same model as para); an internal
    # link href resolves to the web /notes/<key> path.
    html = w._render_block({"table": {
        "columns": ["Ticket", "Summary"],
        "rows": [[
            [{"link": {"href": "jira/tickets/HCLPDRM-38781", "text": "HCLPDRM-38781"}}],
            "Portal UI",
        ]],
    }})
    assert '<a href="/notes/jira/tickets/HCLPDRM-38781">HCLPDRM-38781</a>' in html
    assert "<td>Portal UI</td>" in html  # string cell still renders plainly


def test_table_cell_inline_list_external_link():
    html = w._render_block({"table": {
        "columns": ["Link"],
        "rows": [[[{"link": {"href": "https://example.com/x", "text": "x"}}]]],
    }})
    assert '<a href="https://example.com/x">x</a>' in html


def test_table_cell_link_text_and_href_are_escaped():
    html = w._render_block({"table": {
        "columns": ["Link"],
        "rows": [[[{"link": {"href": "https://e.com/?a=1&b=2", "text": "a<b>c"}}]]],
    }})
    assert 'href="https://e.com/?a=1&amp;b=2"' in html
    assert ">a&lt;b&gt;c</a>" in html


def test_table_cell_bare_inline_dict_shorthand():
    # A bare inline dict is accepted as shorthand for a single-element list.
    html = w._render_block({"table": {
        "columns": ["Link"],
        "rows": [[{"link": {"href": "getting-started", "text": "Start"}}]],
    }})
    assert '<a href="/notes/getting-started">Start</a>' in html


def test_table_mixed_string_and_inline_cells_in_row():
    html = w._render_block({"table": {
        "columns": ["A", "B"],
        "rows": [["**bold**", [{"link": {"href": "k", "text": "L"}}]]],
    }})
    assert "<strong>bold</strong>" in html
    assert '<a href="/notes/k">L</a>' in html
