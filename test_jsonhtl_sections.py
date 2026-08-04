"""Tests for jsonhtl_sections: flat<->nested transforms (Phase 1)."""
from jsonhtl_sections import nest_content, flatten_content, normalise_note


def test_nest_simple():
    flat = [{"heading": {"level": 2, "text": "S"}}, {"para": ["x"]}]
    assert nest_content(flat) == [
        {"section": {"title": "S", "level": 2, "content": [{"para": ["x"]}]}}]


def test_nest_recurses_subheadings():
    flat = [
        {"heading": {"level": 2, "text": "Outer"}},
        {"para": ["lead"]},
        {"heading": {"level": 3, "text": "Inner"}},
        {"para": ["deep"]},
        {"para": ["still outer? no — under Inner (flat semantics)"]},
    ]
    nested = nest_content(flat)
    assert len(nested) == 1
    outer = nested[0]["section"]
    assert outer["title"] == "Outer" and outer["level"] == 2
    # lead para, then the Inner section (which swallows the trailing paras)
    assert outer["content"][0] == {"para": ["lead"]}
    assert outer["content"][1]["section"]["title"] == "Inner"
    assert len(outer["content"][1]["section"]["content"]) == 2


def test_preamble_before_first_heading_stays_flat():
    flat = [{"para": ["preamble"]}, {"heading": {"level": 2, "text": "S"}}, {"para": ["x"]}]
    nested = nest_content(flat)
    assert nested[0] == {"para": ["preamble"]}
    assert nested[1]["section"]["title"] == "S"


def test_higher_level_heading_later_makes_siblings():
    flat = [
        {"heading": {"level": 3, "text": "A"}}, {"para": ["a"]},
        {"heading": {"level": 2, "text": "B"}}, {"para": ["b"]},
    ]
    nested = nest_content(flat)
    assert [n["section"]["title"] for n in nested] == ["A", "B"]
    assert nested[0]["section"]["level"] == 3 and nested[1]["section"]["level"] == 2


ROUNDTRIP_CASES = [
    [],
    [{"para": ["just text"]}, {"table": {"columns": ["A"], "rows": [["1"]]}}],
    [{"heading": {"level": 1, "text": "Title"}},
     {"para": ["intro"]},
     {"heading": {"level": 2, "text": "Sec"}},
     {"para": ["body"]},
     {"heading": {"level": 3, "text": "Sub"}},
     {"para": ["deep"]},
     {"heading": {"level": 2, "text": "Sec2"}},
     {"para": ["more"]}],
    [{"para": ["pre"]}, {"heading": {"level": 4, "text": "skip"}}, {"para": ["x"]}],
]


def test_flatten_is_inverse_of_nest():
    for flat in ROUNDTRIP_CASES:
        assert flatten_content(nest_content(flat)) == flat, flat


def test_nest_is_idempotent():
    for flat in ROUNDTRIP_CASES:
        once = nest_content(flat)
        assert nest_content(once) == once, flat


def test_normalise_note_nests_content_only():
    doc = {"title": "T", "version": 3,
           "content": [{"heading": {"level": 2, "text": "S"}}, {"para": ["x"]}]}
    out = normalise_note(doc)
    assert out["title"] == "T" and out["version"] == 3
    assert out["content"][0]["section"]["title"] == "S"
    # original not mutated
    assert doc["content"][0] == {"heading": {"level": 2, "text": "S"}}


def test_normalise_note_passthrough_for_string_content():
    doc = {"title": "T", "content": "just a string"}
    assert normalise_note(doc) == doc
