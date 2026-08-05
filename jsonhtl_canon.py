"""Canonical field normalisation for JSONHTL blocks.

A single place that defines the canonical field names for each block type and
the non-canonical aliases that are *tolerated*, so that:

  * every renderer reads a block the same way (via ``read_codeblock``), and a
    block can never render fine in one renderer and empty in another; and
  * the write/read paths can *normalise and report* deviations instead of
    silently accepting them (which hides technical debt) or silently breaking
    them (which is the bug that motivated this module: a codeblock stored with
    ``language``/``text`` rendered empty on the web while showing fine on the
    desktop).

Design stance (agreed with the "codex" reviewer and John): keep alias support
indefinitely, but make it *never silent*. A non-canonical document is served
and stored, but every access is flagged (HTTP 203 + warnings) until a sweep
rewrites storage to canonical, at which point it returns to 200.

This module is deliberately structural-agnostic about *sectioning* -- that is
``jsonhtl_sections``'s job. Here we only canonicalise field *names* within
blocks, recursing through ``section`` containers so nested codeblocks are
covered too.
"""

from __future__ import annotations

# Per-block-type alias -> canonical field-name maps. First tuple entry wins when
# several aliases are present. Extend this (not the renderers) to tolerate more.
_CODEBLOCK_ALIASES = (
    ("language", "lang"),
    ("text", "body"),
    ("content", "body"),
)


def read_codeblock(cb) -> tuple[str, str]:
    """Return ``(lang, body)`` for a codeblock value, tolerating known aliases.

    Read-only and warning-free -- this is what renderers call so they agree on
    how to read a block. A non-dict value degrades to ``("", str(value))``.
    """
    if not isinstance(cb, dict):
        return "", "" if cb is None else str(cb)
    lang = cb.get("lang")
    if lang is None:
        lang = cb.get("language", "")
    body = cb.get("body")
    if body is None:
        body = cb.get("text")
    if body is None:
        body = cb.get("content", "")
    return (lang or ""), ("" if body is None else str(body))


def _canonicalise_codeblock(cb: dict) -> tuple[dict, list[str]]:
    """Rename alias fields on a codeblock dict. Returns (new_cb, warnings)."""
    warnings: list[str] = []
    out = dict(cb)
    for alias, canon in _CODEBLOCK_ALIASES:
        if alias not in out:
            continue
        if canon in out:
            warnings.append(
                f"dropped non-canonical field {alias!r} (canonical {canon!r} already present)"
            )
        else:
            out[canon] = out[alias]
            warnings.append(f"renamed non-canonical field {alias!r} -> {canon!r}")
        del out[alias]
    return out, warnings


def canonicalise_content(content, prefix: str = "content") -> tuple[list, list[str]]:
    """Canonicalise a list of blocks, recursing into ``section`` content.

    Non-mutating: returns a new list only where something changed, and a list of
    human-readable warnings keyed by block location (e.g.
    ``content[3].codeblock: renamed ...``).
    """
    warnings: list[str] = []
    if not isinstance(content, list):
        return content, warnings
    out = list(content)
    for i, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        loc = f"{prefix}[{i}]"
        new_block = block

        cb = block.get("codeblock")
        if isinstance(cb, dict):
            new_cb, w = _canonicalise_codeblock(cb)
            if w:
                new_block = {**new_block, "codeblock": new_cb}
                warnings += [f"{loc}.codeblock: {m}" for m in w]

        sec = block.get("section")
        if isinstance(sec, dict) and isinstance(sec.get("content"), list):
            sub, w = canonicalise_content(sec["content"], f"{loc}.section.content")
            if w:
                new_block = {**new_block, "section": {**sec, "content": sub}}
                warnings += w

        out[i] = new_block
    return out, warnings


def canonicalise_doc(doc) -> tuple[object, list[str]]:
    """Canonicalise a whole document's ``content``. Returns (doc, warnings).

    Non-dict values and docs without a list ``content`` pass through unchanged
    (this is a general KV store, not JSONHTL-only). The original is never
    mutated; when there are no warnings the *same* object is returned so callers
    can cheaply detect "nothing to do".
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("content"), list):
        return doc, []
    new_content, warnings = canonicalise_content(doc["content"])
    if not warnings:
        return doc, []
    return {**doc, "content": new_content}, warnings
