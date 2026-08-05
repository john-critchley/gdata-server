"""Tests for jsonhtl_canon: the shared codeblock reader + write/read canonicaliser."""
from jsonhtl_canon import read_codeblock, canonicalise_content, canonicalise_doc


# ---- read_codeblock: the one reader every renderer uses --------------------

def test_read_canonical():
    assert read_codeblock({"lang": "perl", "body": "x"}) == ("perl", "x")


def test_read_aliases():
    assert read_codeblock({"language": "perl", "text": "x"}) == ("perl", "x")
    assert read_codeblock({"content": "y"}) == ("", "y")


def test_read_canonical_wins_over_alias():
    assert read_codeblock({"lang": "sh", "language": "perl", "body": "b", "text": "t"}) == ("sh", "b")


def test_read_missing_and_nondict():
    assert read_codeblock({}) == ("", "")
    assert read_codeblock("raw") == ("", "raw")
    assert read_codeblock(None) == ("", "")


def test_read_stringifies_nonstring_body():
    assert read_codeblock({"lang": "json", "body": 123}) == ("json", "123")


# ---- canonicalise: rename + warn, non-mutating, idempotent -----------------

def test_canonicalise_renames_and_warns():
    content = [{"codeblock": {"language": "perl", "text": "x"}}]
    out, warns = canonicalise_content(content)
    assert out == [{"codeblock": {"lang": "perl", "body": "x"}}]
    assert len(warns) == 2
    assert all(w.startswith("content[0].codeblock:") for w in warns)


def test_canonicalise_canonical_is_noop():
    content = [{"codeblock": {"lang": "perl", "body": "x"}}, {"para": ["hi"]}]
    out, warns = canonicalise_content(content)
    assert warns == []
    assert out == content


def test_canonicalise_does_not_mutate_input():
    content = [{"codeblock": {"language": "perl", "text": "x"}}]
    canonicalise_content(content)
    assert content == [{"codeblock": {"language": "perl", "text": "x"}}]


def test_canonicalise_drops_alias_when_canonical_present():
    content = [{"codeblock": {"lang": "sh", "body": "keep", "text": "drop"}}]
    out, warns = canonicalise_content(content)
    assert out == [{"codeblock": {"lang": "sh", "body": "keep"}}]
    assert any("dropped" in w for w in warns)


def test_canonicalise_recurses_into_sections():
    content = [{"section": {"title": "S", "level": 2, "content": [
        {"codeblock": {"language": "py", "text": "deep"}}]}}]
    out, warns = canonicalise_content(content)
    assert out[0]["section"]["content"][0]["codeblock"] == {"lang": "py", "body": "deep"}
    assert any("section.content[0].codeblock" in w for w in warns)


def test_canonicalise_is_idempotent():
    content = [{"codeblock": {"language": "perl", "text": "x"}},
               {"section": {"title": "S", "content": [{"codeblock": {"content": "z"}}]}}]
    once, w1 = canonicalise_content(content)
    twice, w2 = canonicalise_content(once)
    assert w2 == []
    assert twice == once


def test_canonicalise_doc_passthrough_for_non_htl():
    for val in ("a string", ["a", "list"], {"content": "not a list"}, 42):
        doc, warns = canonicalise_doc(val)
        assert warns == []
        assert doc == val


def test_canonicalise_doc_returns_same_object_when_clean():
    doc = {"title": "T", "content": [{"para": ["x"]}]}
    out, warns = canonicalise_doc(doc)
    assert warns == []
    assert out is doc


def test_canonicalise_doc_normalises_and_preserves_other_keys():
    doc = {"title": "T", "version": 3,
           "content": [{"codeblock": {"language": "perl", "text": "x"}}]}
    out, warns = canonicalise_doc(doc)
    assert out["title"] == "T" and out["version"] == 3
    assert out["content"][0]["codeblock"] == {"lang": "perl", "body": "x"}
    assert warns
    # original untouched
    assert doc["content"][0]["codeblock"] == {"language": "perl", "text": "x"}


def test_block_count_unchanged():
    # sidecar block_ids are positional -> canonicalisation must never add/remove blocks
    content = [{"codeblock": {"language": "a", "text": "1"}},
               {"para": ["p"]},
               {"section": {"content": [{"codeblock": {"text": "2"}}]}}]
    out, _ = canonicalise_content(content)
    assert len(out) == len(content)
