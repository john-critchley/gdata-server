import asyncio
import json
from unittest.mock import patch

import outlook_mail_tools


class FakeConnection:
    def __init__(self):
        self.logged_out = False

    def logout(self):
        self.logged_out = True


def run(awaitable):
    return asyncio.run(awaitable)


def test_search_passes_structured_filters_and_logs_out():
    connection = FakeConnection()
    hit = {"folder": "INBOX", "uid": "42", "subject": "Wanted"}
    with (
        patch.object(outlook_mail_tools, "_connect", return_value=connection),
        patch.object(outlook_mail_tools.outlook_imap, "search_messages", return_value=[hit]) as search,
    ):
        result = run(outlook_mail_tools.outlook_search(
            sender="person@example.com", subject="Wanted", since="2026-10-01", limit=5
        ))
    assert result == {"count": 1, "messages": [hit]}
    assert search.call_args.kwargs["sender"] == "person@example.com"
    assert search.call_args.kwargs["subject"] == "Wanted"
    assert search.call_args.kwargs["since"] == "2026-10-01"
    assert connection.logged_out


def test_read_uses_stable_uid_and_logs_out():
    connection = FakeConnection()
    with (
        patch.object(outlook_mail_tools, "_connect", return_value=connection),
        patch.object(outlook_mail_tools.outlook_imap, "get_message", return_value={"uid": "17"}) as read,
    ):
        assert run(outlook_mail_tools.outlook_read("17", "Archive")) == {"uid": "17"}
    read.assert_called_once_with(connection, "17", folder="Archive")
    assert connection.logged_out


def test_plugin_discovery_exports_four_outlook_tools():
    from mcp_tool_plugins import discover_tools

    tools = {tool.name: tool for tool in discover_tools(
        [outlook_mail_tools.os.path.dirname(outlook_mail_tools.__file__)]
    )}
    names = {"outlook_folders", "outlook_status", "outlook_search", "outlook_read"}
    assert names <= tools.keys()
    for name in names:
        assert "notes key: misc-server/outlook-mail" in tools[name].description
