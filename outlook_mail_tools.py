"""Read-only Outlook.com mailbox tools for the misc MCP server."""

import asyncio
import os

from mcp_tool_plugins import mcp_tool
import outlook_imap


def _connect():
    return outlook_imap.connect(proxy=os.getenv("OUTLOOK_SOCKS_PROXY"))


def _with_connection(operation):
    connection = _connect()
    try:
        return operation(connection)
    finally:
        try:
            connection.logout()
        except Exception:
            pass


@mcp_tool(
    name="outlook_folders",
    description=("List folders in the Outlook/Hotmail mailbox. Read-only. "
                 "See notes key: misc-server/outlook-mail"),
    input_schema={"type": "object", "properties": {}, "required": []},
)
async def outlook_folders():
    folders = await asyncio.to_thread(
        _with_connection, lambda connection: outlook_imap.folder_names(connection)
    )
    return {"count": len(folders), "folders": folders}


@mcp_tool(
    name="outlook_status",
    description=("Get message and unread counts for an Outlook/Hotmail folder. Read-only. "
                 "See notes key: misc-server/outlook-mail"),
    input_schema={
        "type": "object",
        "properties": {
            "folder": {"type": "string", "default": "INBOX"},
        },
        "required": [],
    },
)
async def outlook_status(folder="INBOX"):
    return await asyncio.to_thread(
        _with_connection,
        lambda connection: outlook_imap.mailbox_status(connection, folder),
    )


@mcp_tool(
    name="outlook_search",
    description=(
        "Search Outlook/Hotmail by folder, sender, recipient, subject, body text, "
        "date, or unread state. Returns stable folder UIDs and does not mark mail read. "
        "See notes key: misc-server/outlook-mail"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "folder": {"type": "string", "default": "INBOX"},
            "all_folders": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "unread": {"type": "boolean", "default": False},
            "sender": {"type": "string", "description": "Substring of From address/name"},
            "recipient": {"type": "string", "description": "Substring of To address/name"},
            "subject": {"type": "string", "description": "Substring of subject"},
            "text": {"type": "string", "description": "Text in headers or body"},
            "since": {"type": "string", "format": "date", "description": "Inclusive YYYY-MM-DD"},
            "before": {"type": "string", "format": "date", "description": "Exclusive YYYY-MM-DD"},
        },
        "required": [],
    },
)
async def outlook_search(
    folder="INBOX", all_folders=False, limit=20, unread=False, sender=None,
    recipient=None, subject=None, text=None, since=None, before=None,
):
    limit = max(1, min(int(limit), 100))

    def search(connection):
        folders = outlook_imap.folder_names(connection) if all_folders else (folder,)
        messages = outlook_imap.search_messages(
            connection,
            folders=folders,
            limit=limit,
            unread=unread,
            sender=sender,
            recipient=recipient,
            subject=subject,
            text=text,
            since=since,
            before=before,
        )
        return {"count": len(messages), "messages": messages}

    return await asyncio.to_thread(_with_connection, search)


@mcp_tool(
    name="outlook_read",
    description=(
        "Read and decode one Outlook/Hotmail message using the folder and stable UID "
        "returned by outlook_search. Does not mark mail read. "
        "See notes key: misc-server/outlook-mail"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "uid": {"type": "string", "pattern": "^[0-9]+$"},
            "folder": {"type": "string", "default": "INBOX"},
        },
        "required": ["uid"],
    },
)
async def outlook_read(uid, folder="INBOX"):
    if not str(uid).isdigit():
        raise ValueError("uid must contain decimal digits only")
    return await asyncio.to_thread(
        _with_connection,
        lambda connection: outlook_imap.get_message(connection, str(uid), folder=folder),
    )


@mcp_tool(
    name="outlook_list_parts",
    description=(
        "List the complete MIME structure of one Outlook/Hotmail message using "
        "deterministic part IDs. Read-only. See notes key: misc-server/outlook-mail"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "uid": {"type": "string", "pattern": "^[0-9]+$"},
            "folder": {"type": "string", "default": "INBOX"},
        },
        "required": ["uid"],
    },
)
async def outlook_list_parts(uid, folder="INBOX"):
    if not str(uid).isdigit():
        raise ValueError("uid must contain decimal digits only")
    return await asyncio.to_thread(
        _with_connection,
        lambda connection: outlook_imap.list_message_parts(
            connection, str(uid), folder=folder
        ),
    )


@mcp_tool(
    name="outlook_read_part",
    description=(
        "Read one MIME part by folder, stable UID, and part ID; supports decoded "
        "text, original HTML, or bounded base64 using a targeted IMAP section fetch. "
        "Requesting HTML for another text subtype returns unchanged decoded text plus "
        "a warning. Read-only. "
        "See notes key: misc-server/outlook-mail"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "uid": {"type": "string", "pattern": "^[0-9]+$"},
            "part_id": {"type": "string", "pattern": "^[0-9]+(?:\\.[0-9]+)*$"},
            "folder": {"type": "string", "default": "INBOX"},
            "format": {
                "type": "string",
                "enum": ["auto", "text", "html", "base64"],
                "default": "auto",
            },
            "max_bytes": {
                "type": "integer", "minimum": 1, "maximum": 5000000,
                "default": 1000000,
            },
        },
        "required": ["uid", "part_id"],
    },
)
async def outlook_read_part(uid, part_id, folder="INBOX", format="auto",
                            max_bytes=1_000_000):
    if not str(uid).isdigit():
        raise ValueError("uid must contain decimal digits only")
    return await asyncio.to_thread(
        _with_connection,
        lambda connection: outlook_imap.read_message_part(
            connection, str(uid), str(part_id), folder=folder,
            format=format, max_bytes=int(max_bytes),
        ),
    )
