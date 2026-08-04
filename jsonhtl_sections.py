"""jsonhtl_sections.py — convert between flat (heading-implied) and nested
(section-container) JSONHTL content.

Phase 1 of proposals/section-editing. Two pure, inverse transforms over a
`content` block list:

  nest_content(flat)    -> nested   (heading + following blocks -> section)
  flatten_content(nested) -> flat   (section -> heading + content)

Design points that make them a faithful round-trip:
  * A heading of level L opens a section spanning every following block until the
    next heading of level <= L (or end of list); nested headings recurse.
  * The section keeps the original heading's `level`, so re-rendering (and
    flattening back) reproduces the exact heading levels — even for docs that
    skip levels or lead with a non-H1. So flatten_content(nest_content(x)) == x.
  * Blocks before the first heading stay flat at the top level (preamble).
  * Idempotent: an existing `section` block is passed through untouched, so
    nest_content(nest_content(x)) == nest_content(x) and mixed docs are safe.
"""
from copy import deepcopy


def _is_heading(b):
    return isinstance(b, dict) and isinstance(b.get("heading"), dict)


def nest_content(blocks):
    """Return a new block list with heading-led runs folded into section blocks."""
    if not isinstance(blocks, list):
        return blocks
    out = []
    i, n = 0, len(blocks)
    while i < n:
        b = blocks[i]
        if _is_heading(b):
            level = b["heading"].get("level", 1)
            title = b["heading"].get("text", "")
            j = i + 1
            while j < n:
                bj = blocks[j]
                if _is_heading(bj) and bj["heading"].get("level", 1) <= level:
                    break
                j += 1
            out.append({"section": {
                "title": title,
                "level": level,               # preserve for faithful round-trip
                "content": nest_content(blocks[i + 1:j]),
            }})
            i = j
        else:
            out.append(deepcopy(b))
            i += 1
    return out


def flatten_content(blocks, depth_level=2):
    """Return a new block list with section blocks expanded back to a heading
    followed by their (recursively flattened) content."""
    if not isinstance(blocks, list):
        return blocks
    out = []
    for b in blocks:
        if isinstance(b, dict) and isinstance(b.get("section"), dict):
            sec = b["section"]
            lvl = sec.get("level")
            if not isinstance(lvl, int):
                lvl = depth_level
            lvl = min(max(lvl, 1), 6)
            out.append({"heading": {"level": lvl, "text": sec.get("title", "")}})
            out.extend(flatten_content(sec.get("content", []) or [], min(lvl + 1, 6)))
        else:
            out.append(deepcopy(b))
    return out


def normalise_note(doc):
    """Return a copy of a whole note dict with its content nested. Non-dict or
    string-content docs are returned unchanged."""
    if not isinstance(doc, dict) or not isinstance(doc.get("content"), list):
        return doc
    new = dict(doc)
    new["content"] = nest_content(doc["content"])
    return new
