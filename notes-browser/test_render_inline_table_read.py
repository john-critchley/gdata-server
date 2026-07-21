"""
test_render_inline_table_read.py — headless unit tests for three pieces of
notes_browser.py rendering/data logic:

  1. Inline span rendering (_render_inline_list -> HTML, _inline_text -> plain
     text) including the strong/em/italic types added to INLINE_SPAN_TAGS.
  2. NotesDataSource.read handling both a parsed dict (gdata_local) and a JSON
     string/bytes (gdata_local_simple) from the underlying store.
  3. _render_table sprint-row colouring, with a fallback to alternating stripes.

Importing notes_browser pulls in wx, so these are display-guarded like the
other browser tests even though the functions under test are pure Python.

Run: python -m pytest test_render_inline_table_read.py -v
"""
import os
import re

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="No display available — importing notes_browser requires wx",
)

import notes_browser as nb  # noqa: E402


# The exact para block that leaked raw JSON on proposals/structured-context-protocol.
LEAKING_BLOCK = ["1. ", {"strong": "Name the phases"}, " — document them here"]


@pytest.fixture(scope="module")
def renderer():
    return nb.NotesHTMLRenderer()


def _inline_text(items):
    # _inline_text lives on NotesBrowser (a wx.Frame) but uses no instance
    # state, so call it unbound with a throwaway self.
    return nb.NotesBrowser._inline_text(object(), items)


# ---------------------------------------------------------------------------
# 1. Inline span rendering — HTML (_render_inline_list)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item, expected", [
    ({"strong": "S"}, "<b>S</b>"),
    ({"bold": "B"}, "<b>B</b>"),
    ({"em": "E"}, "<i>E</i>"),
    ({"italic": "I"}, "<i>I</i>"),
    ({"code": "C"}, "<code>C</code>"),
])
def test_render_inline_span_types(renderer, item, expected):
    assert renderer._render_inline_list([item]) == expected


def test_render_inline_strong_no_longer_leaks(renderer):
    html = renderer._render_inline_list(LEAKING_BLOCK)
    assert "<b>Name the phases</b>" in html
    assert "{'strong'" not in html and "{" not in html


def test_render_inline_escapes_span_content(renderer):
    html = renderer._render_inline_list([{"strong": '<x> & "y"'}])
    assert html == '<b>&lt;x&gt; &amp; &quot;y&quot;</b>'


def test_render_inline_link_and_href(renderer):
    link = renderer._render_inline_list([{"link": {"text": "L", "href": "some/key"}}])
    assert "navigate://some/key" in link and ">L</a>" in link
    href = renderer._render_inline_list([{"href": "k", "text": "H"}])
    assert ">H</a>" in href


def test_render_inline_unknown_dict_falls_back(renderer):
    # Unrecognised inline dict must not crash; it is escaped, not rendered raw.
    html = renderer._render_inline_list([{"mystery": "z"}])
    assert "mystery" in html and "<" not in html.replace("&lt;", "")


def test_render_inline_plain_string(renderer):
    assert renderer._render_inline_list(["plain"]) == "plain"


def test_render_inline_mixed_list_preserves_order(renderer):
    # A list of several spans + strings must render in order, each mapped to
    # its own tag — guards against a dispatch that mishandles multi-item lists.
    out = renderer._render_inline_list(
        ["a ", {"strong": "b"}, {"em": "c"}, {"code": "d"}, " e"]
    )
    assert out == "a <b>b</b><i>c</i><code>d</code> e"


# ---------------------------------------------------------------------------
# 1b. Inline span rendering — plain text (_inline_text)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item, expected", [
    ({"strong": "S"}, "S"),
    ({"bold": "B"}, "B"),
    ({"em": "E"}, "E"),
    ({"italic": "I"}, "I"),
    ({"code": "C"}, "C"),
])
def test_inline_text_span_types(item, expected):
    assert _inline_text([item]) == expected


def test_inline_text_strong_no_longer_leaks():
    text = _inline_text(LEAKING_BLOCK)
    assert text == "1. Name the phases — document them here"
    assert "{" not in text


def test_inline_text_link_and_href():
    assert _inline_text([{"link": {"text": "L", "href": "k"}}]) == "L"
    assert _inline_text([{"link": "bare"}]) == "bare"
    assert _inline_text([{"href": "k", "text": "H"}]) == "H"


def test_inline_text_unknown_dict_falls_back():
    # Preserves the previous behaviour for genuinely unknown inline dicts.
    assert _inline_text([{"mystery": "z"}]) == "{'mystery': 'z'}"


# ---------------------------------------------------------------------------
# 2. NotesDataSource.read — dict vs JSON string vs bytes
# ---------------------------------------------------------------------------

class FakeDB:
    """Minimal gdbm-like store returning whatever value it was given."""
    def __init__(self, store):
        self.store = store

    def __contains__(self, key):
        return key in self.store

    def __getitem__(self, key):
        return self.store[key]


def _data_source(store):
    ds = object.__new__(nb.NotesDataSource)   # bypass __init__ (needs http/gdbm)
    ds.use_http = False
    ds.db = FakeDB(store)
    return ds


def test_read_returns_parsed_dict_unchanged():
    doc = {"title": "T", "content": []}
    ds = _data_source({"k": doc})           # gdata_local returns a dict
    assert ds.read("k") == doc


def test_read_parses_json_string():
    ds = _data_source({"k": '{"title": "T", "content": []}'})   # gdata_local_simple
    assert ds.read("k") == {"title": "T", "content": []}


def test_read_parses_json_bytes():
    ds = _data_source({"k": b'{"title": "T"}'})
    assert ds.read("k") == {"title": "T"}


def test_read_missing_key_returns_none():
    ds = _data_source({})
    assert ds.read("absent") is None


def test_read_invalid_json_returns_none():
    # A stored value that is a string but not valid JSON must be swallowed
    # (logged) and reported as None, not raised.
    ds = _data_source({"k": "{not json"})
    assert ds.read("k") is None


# ---------------------------------------------------------------------------
# 3. _render_table — sprint-row colouring with stripe fallback
# ---------------------------------------------------------------------------

def _row_bgcolors(html):
    """Return the bgcolor (or None) of each data row, in order."""
    colors = []
    for tr in html.split("<tr>")[1:]:
        if "<th " in tr:            # header row
            continue
        m = re.search(r'<td bgcolor="([^"]+)"', tr)
        colors.append(m.group(1) if m else None)
    return colors


@pytest.mark.parametrize("col_name", ["Sprint", "sprint", "Sprint / Queue"])
def test_table_sprint_column_detected(renderer, col_name):
    html = renderer._render_table({
        "columns": [col_name, "Item"],
        "rows": [["Sprint 61", "a"]],
    })
    assert _row_bgcolors(html) == ["#ffe08a"]   # palette index 61 % 8 == 5


def test_table_same_sprint_same_colour(renderer):
    html = renderer._render_table({
        "columns": ["Sprint", "Item"],
        "rows": [["Sprint 62", "a"], ["Hybrid Cloud Sprint 62", "b"], ["Sprint 61", "c"]],
    })
    c = _row_bgcolors(html)
    assert c[0] == c[1]          # same sprint number -> same colour (the whole point)
    assert c[0] != c[2]          # different sprint -> different colour


def test_table_non_sprint_row_falls_back_to_stripe(renderer):
    html = renderer._render_table({
        "columns": ["Sprint", "Item"],
        "rows": [["Backlog", "a"], ["Backlog", "b"]],   # no "Sprint N" match
    })
    # even row: no stripe; odd row: #f9f9f9 stripe
    assert _row_bgcolors(html) == [None, "#f9f9f9"]


def test_table_without_sprint_column_uses_stripes_only(renderer):
    html = renderer._render_table({
        "columns": ["Name", "Value"],
        "rows": [["a", 1], ["b", 2], ["c", 3]],
    })
    assert _row_bgcolors(html) == [None, "#f9f9f9", None]


def test_table_non_dict_input(renderer):
    assert renderer._render_table("nope") == "<p>nope</p>"


def _all_td_bgcolors(tr_html):
    """Every <td>'s bgcolor (None if absent) within one row's HTML."""
    return [
        (m.group(1) if m.group(1) else None)
        for m in re.finditer(r'<td(?: bgcolor="([^"]*)")?', tr_html)
    ]


def test_table_colour_applies_to_all_cells_including_non_first_sprint_col(renderer):
    # Sprint column is second; the colour must land on every cell of the row,
    # not just the sprint cell — _row_bgcolors only inspects the first <td>.
    html = renderer._render_table({
        "columns": ["Item", "Sprint / Queue"],
        "rows": [["a", "Hybrid Cloud Sprint 61"]],
    })
    row = html.split("<tr>")[2]           # [0]=preamble, [1]=header, [2]=data row
    cell_colours = _all_td_bgcolors(row)
    assert cell_colours == ["#ffe08a", "#ffe08a"]


def test_table_scalar_row_is_wrapped(renderer):
    # A row given as a scalar rather than a list must still render as one cell.
    html = renderer._render_table({
        "columns": ["Sprint"],
        "rows": ["Sprint 61"],
    })
    assert _row_bgcolors(html) == ["#ffe08a"]


def test_table_sprint_match_is_case_insensitive(renderer):
    # The "Sprint N" match in the cell value uses re.IGNORECASE.
    html = renderer._render_table({
        "columns": ["Sprint", "Item"],
        "rows": [["sprint 61", "a"]],
    })
    assert _row_bgcolors(html) == ["#ffe08a"]


# ---------------------------------------------------------------------------
# 4. Metadata value formatting — list/tuple joined, not shown as raw repr
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (["chess", "board"], "chess, board"),
    (("a", "b", "c"), "a, b, c"),
    ([], ""),
    ("plain", "plain"),
    (5, "5"),
])
def test_format_meta_value(value, expected):
    assert nb.format_meta_value(value) == expected


def test_render_metadata_footer_joins_tag_list(renderer):
    # Regression for tags rendering as ['chess', 'board'] in the footer.
    html = renderer.render("chess", {
        "title": "Chess", "content": [], "version": 5, "tags": ["chess", "board"],
    })
    assert "chess, board" in html
    assert "['chess'" not in html
