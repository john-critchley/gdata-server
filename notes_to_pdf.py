#!/usr/bin/env python3
"""Export a JSONHTL note from the notes REST API to PDF.

Default API base is the local private notes REST port.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests


DEFAULT_NOTES_URL = os.environ.get("NOTES_URL", "http://127.0.0.1:8021")
DEFAULT_TOKEN_FILE = os.environ.get("NOTES_TOKEN_FILE", str(Path.home() / ".oauth_tokens.json"))


def slug_from_key(key: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", key.strip("/")).strip("-")
    return slug or "note"


def token_from_store(path: str) -> str | None:
    token_path = Path(path).expanduser()
    if not token_path.exists():
        return None
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"could not read token file {token_path}: {exc}") from exc

    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        return None

    now = time.time()
    valid: list[tuple[float, str]] = []
    for token, info in tokens.items():
        exp = info.get("exp") if isinstance(info, dict) else None
        if exp is None or float(exp) > now:
            valid.append((float(exp or 0), token))
    if not valid:
        return None
    valid.sort(reverse=True)
    return valid[0][1]


def auth_headers(args: argparse.Namespace) -> dict[str, str]:
    token = args.bearer_token or os.environ.get("NOTES_BEARER_TOKEN")
    if token is None and not args.no_token_file:
        token = token_from_store(args.token_file)
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_note(base_url: str, key: str, headers: dict[str, str]) -> object:
    url = f"{base_url.rstrip('/')}/{quote(key.strip('/'), safe='/')}"
    response = requests.get(url, headers=headers, timeout=20)
    if response.status_code == 401:
        raise SystemExit(
            f"unauthorized fetching {url}. Pass --bearer-token, set NOTES_BEARER_TOKEN, "
            f"or make sure {DEFAULT_TOKEN_FILE} contains a valid OAuth token."
        )
    if response.status_code == 404:
        raise SystemExit(f"note not found: {key}")
    response.raise_for_status()
    return response.json()


def inline_text(value: object) -> str:
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
        if "bold" in value:
            return f"**{inline_text(value['bold'])}**"
        if "italic" in value:
            return f"*{inline_text(value['italic'])}*"
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
    headers = table.get("headers") or table.get("cols") or []
    rows = table.get("rows") or []
    if not headers and rows:
        headers = [f"Column {idx + 1}" for idx in range(len(rows[0]))]
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


def render_block(block: object) -> list[str]:
    if isinstance(block, str):
        return [block, ""]
    if not isinstance(block, dict):
        return [f"```json\n{json.dumps(block, indent=2, ensure_ascii=False)}\n```", ""]

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


def run_checked(args: list[str], *, input_text: str | None = None) -> str:
    proc = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(f"{cmd} failed with exit {proc.returncode}\n{proc.stderr.strip()}")
    return proc.stdout


def require_tools(names: list[str]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise SystemExit(
            "missing required PDF tool(s): "
            + ", ".join(missing)
            + "\nInstall them or use --markdown-out to export Markdown only."
        )


def markdown_to_pdf(markdown_text: str, output: Path) -> None:
    require_tools(["pandoc", "groff", "ps2pdf"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="notes-to-pdf-") as tmp:
        tmpdir = Path(tmp)
        ms_path = tmpdir / "note.ms"
        ps_path = tmpdir / "note.ps"
        ms_text = run_checked(["pandoc", "-f", "markdown", "-t", "ms"], input_text=markdown_text)
        ms_path.write_text(ms_text, encoding="utf-8")
        ps_text = run_checked(["groff", "-t", "-e", "-ms", "-Tps", str(ms_path)])
        ps_path.write_text(ps_text, encoding="latin-1")
        run_checked(["ps2pdf", str(ps_path), str(output)])


def write_html_preview(markdown_text: str, output: Path) -> None:
    require_tools(["pandoc"])
    body = run_checked(["pandoc", "-f", "markdown", "-t", "html5", "--standalone"], input_text=markdown_text)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", help="note key, e.g. certhub/jks-delivery-report-for-kondru")
    parser.add_argument(
        "-u",
        "--notes-url",
        default=DEFAULT_NOTES_URL,
        help=f"notes REST API base URL (default: {DEFAULT_NOTES_URL})",
    )
    parser.add_argument(
        "--bearer-token",
        help="Bearer token for protected notes APIs; default also reads NOTES_BEARER_TOKEN",
    )
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=f"OAuth token store to read if no bearer token is supplied (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--no-token-file",
        action="store_true",
        help="do not auto-read an OAuth token from --token-file",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="PDF output path (default: ./<note-key>.pdf)",
    )
    parser.add_argument("--markdown-out", type=Path, help="also write the intermediate Markdown")
    parser.add_argument("--html-out", type=Path, help="also write an HTML preview generated from Markdown")
    parser.add_argument("--no-pdf", action="store_true", help="fetch/render Markdown/HTML but do not create a PDF")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    note = fetch_note(args.notes_url, args.key, auth_headers(args))
    markdown_text = note_to_markdown(note, args.key)

    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown_text, encoding="utf-8")

    if args.html_out:
        write_html_preview(markdown_text, args.html_out)

    if not args.no_pdf:
        output = args.output or Path(f"{slug_from_key(args.key)}.pdf")
        markdown_to_pdf(markdown_text, output)
        print(output)
    elif not args.markdown_out and not args.html_out:
        print(markdown_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
