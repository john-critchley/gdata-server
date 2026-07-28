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


# JSONHTL -> Markdown rendering lives in the shared jsonhtl_md module so the
# web front end (notes_web.py) and this PDF exporter render notes identically.
from jsonhtl_md import note_to_markdown


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
