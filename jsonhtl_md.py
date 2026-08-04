"""jsonhtl_md.py — canonical JSONHTL → Markdown renderer.

Shared by the web front end (notes_web.py, for the text/markdown and
text/plain responses) and the PDF exporter (notes_to_pdf.py), so a note
renders to the same Markdown everywhere. Keep block/inline coverage in step
with the HTML renderers (_render_block in notes_web.py and NotesHTMLRenderer
in notes_browser.py) and the inline span map (INLINE_SPAN_TAGS).

Markdown emphasis convention: bold/strong -> **...**, italic/em -> *...*,
code -> `...`. svg/image blocks become data-URI images so a fixed-note plot
is viewable in Markdown too; details become a bold summary + nested content.
"""

from __future__ import annotations

import base64
import json


def inline_text(value: object) -> str:
    """Render an inline JSONHTL value (string, list, or span dict) to Markdown."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(inline_text(item) for item in value)
    if isinstance(value, dict):
        if "link" in value and isinstance(value["link"], dict):
            link = value["link"]
            text = inline_text(link.get("text", link.get("href", "")))
            href = str(link.get("href", ""))
            return f"[{text}]({href})" if href else text
        if "code" in value:
            return f"`{str(value['code'])}`"
        # bold/strong -> **, italic/em -> * (mirrors INLINE_SPAN_TAGS)
        if "bold" in value:
            return f"**{inline_text(value['bold'])}**"
        if "strong" in value:
            return f"**{inline_text(value['strong'])}**"
        if "italic" in value:
            return f"*{inline_text(value['italic'])}*"
        if "em" in value:
            return f"*{inline_text(value['em'])}*"
        if "para" in value:
            return inline_text(value["para"])
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def markdown_escape_cell(value: object) -> str:
    text = inline_text(value)
    return text.replace("\n", "<br>").replace("|", "\\|")


def list_item_text(item: object) -> str:
    if isinstance(item, list):
        return inline_text(item)
    if isinstance(item, dict) and "para" in item:
        return inline_text(item["para"])
    return inline_text(item)


def render_table(table: dict) -> list[str]:
    # Canonical shape is {columns, rows}; accept headers/cols as legacy aliases.
    headers = table.get("columns") or table.get("headers") or table.get("cols") or []
    rows = table.get("rows") or []
    if not headers and rows:
        first = rows[0] if isinstance(rows[0], (list, tuple)) else [rows[0]]
        headers = [f"Column {idx + 1}" for idx in range(len(first))]
    if not headers:
        return []

    lines = [
        "| " + " | ".join(markdown_escape_cell(cell) for cell in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        row = list(row) if isinstance(row, (list, tuple)) else [row]
        padded = row + [""] * (len(headers) - len(row))
        lines.append("| " + " | ".join(markdown_escape_cell(cell) for cell in padded[: len(headers)]) + " |")
    return lines


def _caption_lines(caption: object) -> list[str]:
    if not caption:
        return []
    return ["", f"*{inline_text(caption)}*"]


def render_block(block: object, level: int = 2) -> list[str]:
    if isinstance(block, str):
        return [block, ""]
    if not isinstance(block, dict):
        return [f"```json\n{json.dumps(block, indent=2, ensure_ascii=False)}\n```", ""]

    if "section" in block and isinstance(block["section"], dict):
        sec = block["section"]
        lvl = sec.get("level")
        if not isinstance(lvl, int):
            lvl = level
        lvl = min(max(lvl, 1), 6)
        title = inline_text(sec.get("title", "") or "")
        lines = [f"{'#' * lvl} {title}", ""] if title else []
        for nested in sec.get("content", []) or []:
            lines.extend(render_block(nested, min(lvl + 1, 6)))
        return lines

    if "heading" in block and isinstance(block["heading"], dict):
        heading = block["heading"]
        level = int(heading.get("level", 1))
        level = min(max(level, 1), 6)
        return [f"{'#' * level} {inline_text(heading.get('text', ''))}", ""]

    if "para" in block:
        return [inline_text(block["para"]), ""]

    if "codeblock" in block and isinstance(block["codeblock"], dict):
        cb = block["codeblock"]
        lang = cb.get("lang", cb.get("language", ""))
        body = cb.get("body", cb.get("content", ""))
        return [f"```{lang}", str(body), "```", ""]

    if "table" in block and isinstance(block["table"], dict):
        lines = render_table(block["table"])
        return lines + [""] if lines else []

    if "svg" in block and isinstance(block["svg"], dict):
        sv = block["svg"]
        body = sv.get("body", "")
        if not isinstance(body, str) or not body.strip():
            return []
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        alt = inline_text(sv.get("alt", "") or "")
        return [f"![{alt}](data:image/svg+xml;base64,{b64})"] + _caption_lines(sv.get("caption")) + [""]

    if "image" in block and isinstance(block["image"], dict):
        im = block["image"]
        data = im.get("data", "")
        if not isinstance(data, str) or not data.strip():
            return []
        fmt = im.get("format", "png") or "png"
        alt = inline_text(im.get("alt", "") or "")
        return [f"![{alt}](data:image/{fmt};base64,{data})"] + _caption_lines(im.get("caption")) + [""]

    if "details" in block and isinstance(block["details"], dict):
        d = block["details"]
        summary = inline_text(d.get("summary", "") or "")
        lines = [f"**{summary}**", ""] if summary else []
        for nested in d.get("content", []) or []:
            lines.extend(render_block(nested))
        return lines

    if "ul" in block:
        return [f"- {list_item_text(item)}" for item in block.get("ul") or []] + [""]

    if "bullet_list" in block:
        return [f"- {list_item_text(item)}" for item in block.get("bullet_list") or []] + [""]

    if "list" in block and isinstance(block["list"], dict):
        lst = block["list"]
        ordered = bool(lst.get("ordered"))
        label = lst.get("label")
        lines = [f"**{inline_text(label)}**", ""] if label else []
        for idx, item in enumerate(lst.get("items", []), start=1):
            prefix = f"{idx}." if ordered else "-"
            lines.append(f"{prefix} {list_item_text(item)}")
        return lines + [""]

    return [f"```json\n{json.dumps(block, indent=2, ensure_ascii=False)}\n```", ""]


def note_to_markdown(note: object, key: str) -> str:
    """Render a whole JSONHTL note (dict with title/content/meta, or bare
    content) to a Markdown document string."""
    if isinstance(note, dict):
        title = note.get("title", key)
        content = note.get("content", [])
        meta = {k: v for k, v in note.items() if k not in {"title", "content"}}
    else:
        title = key
        content = note
        meta = {}

    blocks = content if isinstance(content, list) else [content]
    lines = [f"# {inline_text(title)}", ""]
    for block in blocks:
        lines.extend(render_block(block))

    if meta:
        lines.extend(["## Metadata", ""])
        for key_name, value in meta.items():
            lines.append(f"- **{key_name}:** `{json.dumps(value, ensure_ascii=False)}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
