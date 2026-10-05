#!/usr/bin/env python3
"""Read an Outlook.com mailbox over IMAP using OAuth2/XOAUTH2.

Credentials use the same ~/.netrc entry as popit3.py::

    machine outlook.office365.com
      login user@hotmail.com
      account MSAL:application-client-id
      password refresh-token

The refresh token must have consent for IMAP.AccessAsUser.All.  Run
get_pop_refresh_token.py to obtain a token covering both POP and IMAP.
"""

import argparse
import base64
import email
import html
import imaplib
import json
import netrc
import os
import re
import ssl
import sys
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlparse
from email.header import decode_header, make_header
from email.policy import default as default_policy

import requests
try:
    import socks
except ImportError:  # Optional: direct connections do not need PySocks.
    socks = None


DEFAULT_HOST = "outlook.office365.com"
DEFAULT_MACHINE = DEFAULT_HOST
DEFAULT_PORT = 993
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/consumers"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"


def _credentials(machine):
    auth = netrc.netrc(os.path.expanduser("~/.netrc")).authenticators(machine)
    if not auth:
        raise RuntimeError(f"No entry for machine {machine!r} in ~/.netrc")
    login, account, refresh_token = auth
    if not login or not refresh_token:
        raise RuntimeError(f"~/.netrc entry for {machine!r} is incomplete")
    client_id = None
    if account and account.upper().startswith("MSAL:"):
        client_id = account.split(":", 1)[1] or None
    return login, client_id, refresh_token


def _access_token(client_id, refresh_token, authority, timeout=30, proxy=None):
    response = requests.post(
        authority.rstrip("/") + "/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": IMAP_SCOPE,
        },
        timeout=timeout,
        proxies={"http": proxy, "https": proxy} if proxy else None,
    )
    result = response.json()
    if response.status_code != 200 or "access_token" not in result:
        raise RuntimeError(
            "IMAP token exchange failed: "
            + str(result.get("error_description") or result.get("error") or result)
        )
    return result["access_token"]


class SocksIMAP4SSL(imaplib.IMAP4_SSL):
    """IMAP4_SSL connection whose TCP socket is opened through SOCKS5."""

    def __init__(self, host, port, proxy, timeout=30):
        if socks is None:
            raise RuntimeError("SOCKS proxy requested but PySocks is not installed")
        parsed = urlparse(proxy)
        if parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname:
            raise ValueError("proxy must be a socks5:// or socks5h:// URL")
        self._proxy = parsed
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout):
        raw = socks.create_connection(
            (self.host, self.port),
            timeout=timeout,
            proxy_type=socks.SOCKS5,
            proxy_addr=self._proxy.hostname,
            proxy_port=self._proxy.port or 1080,
            proxy_username=self._proxy.username,
            proxy_password=self._proxy.password,
            proxy_rdns=self._proxy.scheme == "socks5h",
        )
        return self.ssl_context.wrap_socket(raw, server_hostname=self.host)


def connect(
    machine=DEFAULT_MACHINE,
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
    user=None,
    client_id=None,
    authority=DEFAULT_AUTHORITY,
    timeout=30,
    proxy=None,
):
    """Return an authenticated IMAP4_SSL connection."""
    login, stored_client_id, refresh_token = _credentials(machine)
    user = user or login
    client_id = client_id or stored_client_id
    if not client_id:
        raise RuntimeError(
            "client_id not provided; pass --client-id or use account "
            "'MSAL:<client_id>' in ~/.netrc"
        )

    access_token = _access_token(client_id, refresh_token, authority, timeout, proxy)
    auth = f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()
    conn = (SocksIMAP4SSL(host, port, proxy, timeout) if proxy
            else imaplib.IMAP4_SSL(host, port, timeout=timeout))
    try:
        conn.authenticate("XOAUTH2", lambda _: auth)
    except BaseException:
        conn.shutdown()
        raise
    return conn


def list_folders(conn):
    status, rows = conn.list()
    if status != "OK":
        raise RuntimeError(f"IMAP LIST failed: {rows!r}")
    return [row.decode("utf-8", "replace") for row in rows or []]


def folder_names(conn):
    """Return selectable folder names parsed from IMAP LIST responses."""
    names = []
    for row in list_folders(conn):
        match = re.search(r' (?:"((?:[^"\\]|\\.)*)"|([^ ]+))$', row)
        if match:
            names.append((match.group(1) or match.group(2)).replace(r'\"', '"'))
    return names


def _select_readonly(conn, folder):
    status, _ = conn.select(_mailbox_arg(folder), readonly=True)
    if status != "OK":
        raise RuntimeError(f"Cannot select IMAP folder {folder!r}")


def _quoted(value):
    """Quote a user value for an IMAP SEARCH string argument."""
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError("IMAP search values cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', r'\"') + '"'


def _mailbox_arg(folder):
    """Quote mailbox names when imaplib would otherwise split on spaces."""
    return folder if re.fullmatch(r"[A-Za-z0-9_./-]+", folder) else _quoted(folder)


def _imap_date(value):
    parsed = date.fromisoformat(value)
    return parsed.strftime("%d-%b-%Y")


def build_search_criteria(
    unread=False, sender=None, recipient=None, subject=None, text=None,
    since=None, before=None,
):
    criteria = ["UNSEEN" if unread else "ALL"]
    for key, value in (
        ("FROM", sender), ("TO", recipient), ("SUBJECT", subject), ("TEXT", text)
    ):
        if value:
            criteria.extend((key, _quoted(value)))
    if since:
        criteria.extend(("SINCE", _imap_date(since)))
    if before:
        criteria.extend(("BEFORE", _imap_date(before)))
    return criteria


def _fetch_bytes(conn, uid, query):
    status, data = conn.uid("fetch", str(uid), query)
    if status != "OK":
        raise RuntimeError(f"IMAP FETCH failed for UID {uid!r}: {data!r}")
    for item in data or []:
        if isinstance(item, tuple) and len(item) > 1:
            return item[1]
    raise RuntimeError(f"IMAP FETCH returned no message data for UID {uid!r}")


def _header_summary(raw, uid, folder):
    msg = email.message_from_bytes(raw, policy=default_policy)
    return {
        "folder": folder,
        "uid": str(uid),
        "date": str(msg.get("Date", "")),
        "from": str(make_header(decode_header(str(msg.get("From", ""))))),
        "to": str(make_header(decode_header(str(msg.get("To", ""))))),
        "subject": str(make_header(decode_header(str(msg.get("Subject", ""))))),
        "message_id": str(msg.get("Message-ID", "")),
    }


def search_messages(conn, folders=("INBOX",), limit=20, **filters):
    """Search folders and return newest matching headers without setting Seen."""
    criteria = build_search_criteria(**filters)
    messages = []
    for folder in folders:
        _select_readonly(conn, folder)
        status, rows = conn.uid("search", None, *criteria)
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH failed in {folder!r}: {rows!r}")
        uids = (rows[0] or b"").split()
        for uid in reversed(uids[-limit:]):
            raw = _fetch_bytes(
                conn, uid.decode("ascii"),
                "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])",
            )
            messages.append(_header_summary(raw, uid.decode("ascii"), folder))
    # Dates from arbitrary email can be malformed, so UID is the dependable
    # within-folder ordering. Preserve folder order for multi-folder searches.
    return messages[:limit] if len(folders) == 1 else messages


def list_messages(conn, folder="INBOX", limit=20, unread=False):
    """Return recent message summaries without changing Seen flags."""
    return search_messages(conn, folders=(folder,), limit=limit, unread=unread)


def mailbox_status(conn, folder="INBOX"):
    status, rows = conn.status(_mailbox_arg(folder), "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)")
    if status != "OK":
        raise RuntimeError(f"IMAP STATUS failed for {folder!r}: {rows!r}")
    text = (rows[0] or b"").decode("utf-8", "replace")
    values = {key.lower(): int(value) for key, value in
              re.findall(r"(MESSAGES|UNSEEN|UIDNEXT|UIDVALIDITY) (\d+)", text)}
    return {"folder": folder, **values}


def _iter_mime_parts(msg, prefix=""):
    """Yield deterministic MIME-tree paths and parts, including containers."""
    if not msg.is_multipart():
        yield prefix or "1", msg
        return
    for index, part in enumerate(msg.iter_parts(), 1):
        part_id = f"{prefix}.{index}" if prefix else str(index)
        yield part_id, part
        if part.is_multipart():
            yield from _iter_mime_parts(part, part_id)


def _part_bytes(part):
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    if part.is_multipart():
        return b""
    value = part.get_payload()
    if isinstance(value, str):
        return value.encode(part.get_content_charset() or "utf-8", "replace")
    return b""


def _part_summary(part_id, part):
    payload = _part_bytes(part)
    return {
        "part_id": part_id,
        "content_type": part.get_content_type(),
        "charset": part.get_content_charset(),
        "disposition": part.get_content_disposition(),
        "filename": part.get_filename(),
        "content_id": part.get("Content-ID"),
        "transfer_encoding": part.get("Content-Transfer-Encoding"),
        "size": len(payload),
        "is_multipart": part.is_multipart(),
        "child_count": len(part.get_payload()) if part.is_multipart() else 0,
        "is_attachment": part.get_content_disposition() == "attachment",
        "is_inline": part.get_content_disposition() == "inline",
        "is_candidate_body": (
            not part.is_multipart()
            and part.get_content_disposition() != "attachment"
            and part.get_content_type() in {"text/plain", "text/html"}
        ),
    }


def message_parts(msg):
    return [_part_summary(part_id, part) for part_id, part in _iter_mime_parts(msg)]


def _decode_text_part(part):
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        charset = part.get_content_charset() or "utf-8"
        try:
            content = _part_bytes(part).decode(charset, "replace")
        except LookupError:
            content = _part_bytes(part).decode("utf-8", "replace")
    return content if isinstance(content, str) else str(content)


class _ReadableHTMLParser(HTMLParser):
    """Small, dependency-free HTML-to-text renderer which retains link targets."""

    _BREAK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.hidden = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in self._BREAK_TAGS:
            self.output.append("\n")
        if not self.hidden and tag == "a":
            self.links.append(dict(attrs).get("href"))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in self._BREAK_TAGS:
            self.output.append("\n")
        elif not self.hidden and tag == "a" and self.links:
            target = self.links.pop()
            if target:
                self.output.append(f" <{target}>")

    def handle_data(self, data):
        if not self.hidden:
            self.output.append(data)


def _html_to_text(content):
    parser = _ReadableHTMLParser()
    parser.feed(content)
    parser.close()
    lines = [re.sub(r"[ \t]+", " ", line).strip()
             for line in html.unescape("".join(parser.output)).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _body_text(msg):
    plain = []
    rich = []
    parts = list(_iter_mime_parts(msg))
    attachment_prefixes = tuple(
        part_id for part_id, part in parts
        if part.get_content_disposition() == "attachment"
    )
    for part_id, part in parts:
        if any(part_id == prefix or part_id.startswith(prefix + ".")
               for prefix in attachment_prefixes):
            continue
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        content = _decode_text_part(part).strip()
        if not content:
            continue
        if part.get_content_type() == "text/plain":
            plain.append((part_id, content))
        else:
            rich.append((part_id, _html_to_text(content)))
    candidates = plain or rich
    if not candidates:
        return "", "none"
    part_id, content = candidates[0]
    return content, part_id


def _parsed_message(conn, uid, folder):
    _select_readonly(conn, folder)
    message_bytes = _fetch_bytes(conn, uid, "(BODY.PEEK[])")
    return message_bytes, email.message_from_bytes(message_bytes, policy=default_policy)


def _targeted_part(conn, uid, part_id, folder, max_bytes):
    """Fetch one MIME leaf without downloading the complete message."""
    if not re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", str(part_id)):
        raise ValueError("part_id must be a dot-separated MIME path such as '2' or '1.2'")
    _select_readonly(conn, folder)
    mime_headers = _fetch_bytes(conn, uid, f"(BODY.PEEK[{part_id}.MIME])")
    header_only = email.message_from_bytes(mime_headers, policy=default_policy)
    if header_only.get_content_maintype() == "multipart":
        raise ValueError(f"MIME part {part_id!r} is a container; choose a child part")

    # Transfer encodings expand rather than compress normal MIME content. Four
    # encoded bytes per permitted decoded byte leaves ample room for base64,
    # quoted-printable, and line wrapping while still bounding network input.
    wire_limit = max_bytes * 4 + 4096
    payload = _fetch_bytes(conn, uid, f"(BODY.PEEK[{part_id}]<0.{wire_limit + 1}>)")
    if len(payload) > wire_limit:
        raise ValueError(
            f"MIME part transfer data exceeds the safe limit for max_bytes={max_bytes}"
        )
    separator = b"" if mime_headers.endswith((b"\n\n", b"\r\n\r\n")) else b"\r\n"
    return email.message_from_bytes(
        mime_headers + separator + payload, policy=default_policy
    )


def list_message_parts(conn, uid, folder="INBOX"):
    """Describe a message's complete MIME tree without marking it read."""
    _, msg = _parsed_message(conn, uid, folder)
    return {"folder": folder, "uid": str(uid), "parts": message_parts(msg)}


def read_message_part(conn, uid, part_id, folder="INBOX", format="auto",
                      max_bytes=1_000_000):
    """Read one deterministic MIME part as text, HTML, or bounded base64."""
    if format not in {"auto", "text", "html", "base64"}:
        raise ValueError("format must be one of: auto, text, html, base64")
    if max_bytes < 1 or max_bytes > 5_000_000:
        raise ValueError("max_bytes must be between 1 and 5000000")
    part = _targeted_part(conn, uid, str(part_id), folder, max_bytes)
    payload = _part_bytes(part)
    if len(payload) > max_bytes:
        raise ValueError(
            f"MIME part is {len(payload)} bytes, exceeding max_bytes={max_bytes}"
        )
    result = {"folder": folder, "uid": str(uid), **_part_summary(str(part_id), part)}
    content_type = part.get_content_type()
    chosen = format
    if chosen == "auto":
        chosen = "text" if content_type.startswith("text/") else "base64"
    if chosen in {"text", "html"}:
        if not content_type.startswith("text/"):
            raise ValueError(f"MIME part {part_id!r} is {content_type}, not text")
        content = _decode_text_part(part)
        if chosen == "text" and content_type == "text/html":
            content = _html_to_text(content)
        result.update({"format": chosen, "content": content})
        if chosen == "html" and content_type != "text/html":
            result["warning"] = (
                f"Requested html, but MIME part {part_id!r} is {content_type}; "
                "returning its decoded text unchanged"
            )
    else:
        result.update({"format": "base64", "content_base64": base64.b64encode(payload).decode("ascii")})
    return result


def get_message(conn, uid, folder="INBOX", raw=False):
    """Fetch one message by stable folder UID without setting Seen."""
    message_bytes, msg = _parsed_message(conn, uid, folder)
    if raw:
        return message_bytes
    result = _header_summary(message_bytes, uid, folder)
    result["cc"] = str(msg.get("Cc", ""))
    result["body"], result["body_source"] = _body_text(msg)
    result["body_status"] = "found" if result["body_source"] != "none" else "none"
    result["parts"] = message_parts(msg)
    result["attachments"] = [
        {
            "part_id": part_id,
            "filename": part.get_filename(),
            "content_type": part.get_content_type(),
            "size": len(part.get_payload(decode=True) or b""),
        }
        for part_id, part in _iter_mime_parts(msg)
        if part.get_content_disposition() == "attachment"
    ]
    return result


def main(command="folders", folder="INBOX", limit=20, unread=False,
         all_folders=False, uid=None, raw=False, sender=None, recipient=None,
         subject=None, text=None, since=None, before=None, part_id=None,
         format="auto", max_bytes=1_000_000, **connect_args):
    conn = connect(**connect_args)
    try:
        if command == "folders":
            return list_folders(conn)
        if command == "status":
            return mailbox_status(conn, folder)
        if command == "list":
            return list_messages(conn, folder=folder, limit=limit, unread=unread)
        if command == "search":
            folders = folder_names(conn) if all_folders else (folder,)
            return search_messages(
                conn, folders=folders, limit=limit, unread=unread,
                sender=sender, recipient=recipient, subject=subject, text=text,
                since=since, before=before,
            )
        if command in {"show", "raw"}:
            return get_message(conn, uid, folder=folder, raw=raw or command == "raw")
        if command == "parts":
            return list_message_parts(conn, uid, folder=folder)
        if command == "part":
            return read_message_part(conn, uid, part_id, folder=folder,
                                     format=format, max_bytes=max_bytes)
        raise ValueError(f"Unknown command: {command}")
    finally:
        try:
            conn.logout()
        except imaplib.IMAP4.error:
            pass


def _parser():
    parser = argparse.ArgumentParser(
        description="Access Outlook.com through IMAP OAuth2",
        epilog="Extended documentation: notes key misc-server/outlook-mail",
    )
    parser.add_argument("--machine", default=DEFAULT_MACHINE)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--user")
    parser.add_argument("--client-id")
    parser.add_argument("--authority", default=DEFAULT_AUTHORITY)
    parser.add_argument(
        "--proxy",
        default=os.environ.get("OUTLOOK_SOCKS_PROXY"),
        help="SOCKS proxy URL, e.g. socks5h://127.0.0.1:1080",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("folders", help="List mailbox folders")
    status = sub.add_parser("status", help="Show message and unread counts")
    status.add_argument("--folder", default="INBOX")
    listing = sub.add_parser("list", help="List recent messages without marking them read")
    listing.add_argument("--folder", default="INBOX")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--unread", action="store_true")
    search = sub.add_parser("search", help="Find messages without marking them read")
    search.add_argument("--folder", default="INBOX")
    search.add_argument("--all-folders", action="store_true")
    search.add_argument("--limit", type=int, default=20,
                        help="Maximum matches per folder (default: 20)")
    search.add_argument("--unread", action="store_true")
    search.add_argument("--from", dest="sender")
    search.add_argument("--to", dest="recipient")
    search.add_argument("--subject")
    search.add_argument("--text", help="Search headers and message body")
    search.add_argument("--since", metavar="YYYY-MM-DD")
    search.add_argument("--before", metavar="YYYY-MM-DD")
    show = sub.add_parser("show", help="Show a decoded message by folder UID")
    show.add_argument("uid")
    show.add_argument("--folder", default="INBOX")
    raw = sub.add_parser("raw", help="Write the original RFC822 message to stdout")
    raw.add_argument("uid")
    raw.add_argument("--folder", default="INBOX")
    parts = sub.add_parser("parts", help="List a message's MIME structure")
    parts.add_argument("uid")
    parts.add_argument("--folder", default="INBOX")
    part = sub.add_parser("part", help="Read one MIME part by deterministic part ID")
    part.add_argument("uid")
    part.add_argument("part_id")
    part.add_argument("--folder", default="INBOX")
    part.add_argument("--format", choices=("auto", "text", "html", "base64"), default="auto")
    part.add_argument("--max-bytes", type=int, default=1_000_000)
    return parser


if __name__ == "__main__":
    args = vars(_parser().parse_args())
    json_output = args.pop("json")
    result = main(**args)
    if isinstance(result, bytes):
        sys.stdout.buffer.write(result)
    elif json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                print(f"{item['folder']}\t{item['uid']}\t{item['date']}\t"
                      f"{item['from']}\t{item['subject']}")
            else:
                print(item)
    elif isinstance(result, dict) and "body" in result:
        for key in ("folder", "uid", "date", "from", "to", "cc", "subject", "message_id"):
            print(f"{key.replace('_', '-').title()}: {result.get(key, '')}")
        if result["attachments"]:
            print("Attachments: " + ", ".join(
                f"{item['filename'] or '(unnamed)'} ({item['content_type']}, {item['size']} bytes)"
                for item in result["attachments"]
            ))
        print("\n" + result["body"])
    elif isinstance(result, dict):
        print("\t".join(f"{key}={value}" for key, value in result.items()))
