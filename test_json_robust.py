"""
test_json_robust.py — unit tests for _parse_json_robust, the shared helper that
tolerates a common client mistake: escaping apostrophes as \\' inside JSON
string values (e.g. "I\\'ll"). \\' is never valid JSON -- the only recognised
escapes are \\" \\\\ \\/ \\b \\f \\n \\r \\t and \\uXXXX -- so any occurrence
can safely be unescaped to a literal apostrophe and re-parsed.

Both gdata_server.py and gdata_mcp_server.py carry their own copy of this
function (kept in sync by convention, not by import) -- run both against the
same cases here so a fix applied to only one file shows up as a failure.

Run: python -m pytest test_json_robust.py -v
"""
import json

import pytest

from gdata_server import _parse_json_robust as robust_server
from gdata_mcp_server import _parse_json_robust as robust_mcp

IMPLS = pytest.mark.parametrize("parse", [robust_server, robust_mcp],
                                ids=["gdata_server", "gdata_mcp_server"])


@IMPLS
def test_plain_unescaped_apostrophe_parses_directly(parse):
    """A literal apostrophe needs no escaping in JSON -- should parse first try."""
    text = json.dumps({"para": ["I'll do this"]})
    assert parse(text) == {"para": ["I'll do this"]}


@IMPLS
def test_single_escaped_apostrophe_is_repaired(parse):
    """The reported bug: \\' (invalid JSON escape) should be unescaped and parsed."""
    text = r'{"para": ["I\'ll do this"]}'
    assert parse(text) == {"para": ["I'll do this"]}


@IMPLS
def test_multiple_escaped_apostrophes_all_repaired(parse):
    text = r'["I\'ll", "don\'t", "it\'s"]'
    assert parse(text) == ["I'll", "don't", "it's"]


@IMPLS
def test_escaped_apostrophe_inside_nested_structure(parse):
    text = r'[{"op": "append_block", "block": {"para": ["I\'ll do this"]}}]'
    assert parse(text) == [{"op": "append_block", "block": {"para": ["I'll do this"]}}]


@IMPLS
def test_real_backslash_followed_by_literal_apostrophe_parses_directly(parse):
    """A correctly-escaped backslash (\\\\) followed by a literal apostrophe is
    valid JSON on its own and must not be mistaken for the broken \\' escape."""
    text = r'["a\\'"'"'b"]'  # JSON text: ["a\\'b"] -> decodes to the string a\'b
    assert parse(text) == ["a\\'b"]


@IMPLS
def test_no_apostrophe_invalid_json_raises_normally(parse):
    """Unrelated invalid JSON (no apostrophe involved) must still raise -- the
    apostrophe repair path should not mask or alter this error."""
    with pytest.raises(json.JSONDecodeError):
        parse("{not json at all")


@IMPLS
def test_escaped_apostrophe_plus_unrelated_bad_escape_raises_combined_error(parse):
    """If repairing the apostrophe still leaves the JSON broken for an unrelated
    reason (e.g. a Windows path with \\U), the error must surface both the
    original and post-repair failure, not just silently re-raise the original
    (which would look like the apostrophe fix was never attempted)."""
    text = r'{"path": "C:\Users", "note": "I\'ll"}'
    with pytest.raises(json.JSONDecodeError) as exc_info:
        parse(text)
    msg = str(exc_info.value)
    assert "apostrophe" in msg
    assert "original error" in msg


@IMPLS
def test_empty_string_raises(parse):
    with pytest.raises(json.JSONDecodeError):
        parse("")


@IMPLS
def test_valid_json_without_any_apostrophe_unaffected(parse):
    text = json.dumps({"a": 1, "b": [1, 2, 3], "c": None})
    assert parse(text) == {"a": 1, "b": [1, 2, 3], "c": None}
