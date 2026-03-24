"""
test_mcp_transports.py — integration tests for gdata MCP server.

Tests both SSE and Streamable HTTP transports against a real server
started in a background thread. All tests have timeouts to avoid hanging.

Run:  python -m pytest test_mcp_transports.py -v
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time

# Set env vars BEFORE importing any gdata modules that read them at module load
_token_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_token_file.close()
os.environ["OAUTH_TOKEN_FILE"] = _token_file.name
os.environ["MCP_SSE"] = "true"
os.environ["MCP_STREAMABLE"] = "true"

sys.path.insert(0, os.path.dirname(__file__))

import httpx
import pytest
import uvicorn

import gdata_oauth
from gdata_mcp_server import make_mcp_app
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BEARER = "test-bearer-token-for-pytest"
TIMEOUT = 8  # seconds per async operation
TEST_KEY_SSE = "_test/sse/roundtrip"
TEST_KEY_STREAMABLE = "_test/streamable/roundtrip"
TEST_KEY_CROSS = "_test/cross/transport"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Server on :{port} did not start within {timeout}s")


def run_async(coro, timeout: float = TIMEOUT):
    """Run an async coroutine from sync test code with a hard timeout."""
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def parse_tool_result(content) -> dict:
    """Extract the JSON dict from a tool call result."""
    return json.loads(content[0].text)


# ---------------------------------------------------------------------------
# Module-scoped fixtures: one server for the whole test run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_file():
    with tempfile.NamedTemporaryFile(suffix=".gdbm", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
        os.unlink(path + ".db")  # gdbm may append .db
    except FileNotFoundError:
        pass


@pytest.fixture(scope="module")
def server_url(db_file):
    # Re-initialise the token store singleton so it uses our temp file
    gdata_oauth._store = None
    store = gdata_oauth._get_store()
    store.save_token(BEARER)

    app = make_mcp_app(db_file)
    port = get_free_port()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    wait_for_port(port)

    yield f"http://127.0.0.1:{port}"

    srv.should_exit = True
    thread.join(timeout=3.0)


# ---------------------------------------------------------------------------
# SSE transport tests
# ---------------------------------------------------------------------------

class TestSSETransport:

    def test_sse_rejected_without_token(self, server_url):
        """GET /mcp/ without Bearer token must return 401."""
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(f"{server_url}/mcp/")
        assert r.status_code == 401

    def test_sse_initialize(self, server_url):
        """SSE client can connect and complete MCP initialize handshake."""
        async def go():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    result = await session.initialize()
                    assert result.serverInfo.name == "gdata"

        run_async(go())

    def test_sse_list_tools(self, server_url):
        """SSE: list_tools returns the expected five tools."""
        async def go():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    names = {t.name for t in result.tools}
                    assert names == {"get", "put", "delete", "keys", "dump"}

        run_async(go())

    def test_sse_put_get_roundtrip(self, server_url):
        """SSE: put an object, get it back — verifies double-encoding fix."""
        payload = {"hello": "world", "nested": {"x": 42}}

        async def go():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()

                    put_result = parse_tool_result(
                        (await session.call_tool("put", {"key": TEST_KEY_SSE, "value": payload})).content
                    )
                    assert put_result == {"status": "ok"}

                    get_result = parse_tool_result(
                        (await session.call_tool("get", {"key": TEST_KEY_SSE})).content
                    )
                    assert get_result == payload, f"Round-trip mismatch: {get_result!r}"

        run_async(go())

    def test_sse_keys_includes_test_key(self, server_url):
        """SSE: keys() lists the key written in the previous test."""
        async def go():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = parse_tool_result(
                        (await session.call_tool("keys", {})).content
                    )
                    assert TEST_KEY_SSE in result["keys"]

        run_async(go())

    def test_sse_delete(self, server_url):
        """SSE: delete removes the key, subsequent get returns an error."""
        async def go():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()

                    del_result = parse_tool_result(
                        (await session.call_tool("delete", {"key": TEST_KEY_SSE})).content
                    )
                    assert del_result == {"status": "deleted"}

                    get_result = parse_tool_result(
                        (await session.call_tool("get", {"key": TEST_KEY_SSE})).content
                    )
                    assert "error" in get_result

        run_async(go())

    def test_sse_get_missing_key(self, server_url):
        """SSE: getting a non-existent key returns an error dict, not an exception."""
        async def go():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = parse_tool_result(
                        (await session.call_tool("get", {"key": "_test/does-not-exist"})).content
                    )
                    assert "error" in result

        run_async(go())


# ---------------------------------------------------------------------------
# Streamable HTTP transport tests
# ---------------------------------------------------------------------------

class TestStreamableHTTPTransport:

    def test_streamable_rejected_without_token(self, server_url):
        """POST /mcp without Bearer token must return 401."""
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                f"{server_url}/mcp",
                headers={"Accept": "application/json, text/event-stream",
                         "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1,
                      "params": {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1.0"}}},
            )
        assert r.status_code == 401

    def test_streamable_initialize(self, server_url):
        """Streamable HTTP client can connect and complete MCP initialize."""
        async def go():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    result = await session.initialize()
                    assert result.serverInfo.name == "gdata"

        run_async(go())

    def test_streamable_list_tools(self, server_url):
        """Streamable HTTP: list_tools returns the expected five tools."""
        async def go():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    names = {t.name for t in result.tools}
                    assert names == {"get", "put", "delete", "keys", "dump"}

        run_async(go())

    def test_streamable_put_get_roundtrip(self, server_url):
        """Streamable HTTP: put an object, get it back — verifies double-encoding fix."""
        payload = {"transport": "streamable", "nested": {"y": 99}}

        async def go():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    put_result = parse_tool_result(
                        (await session.call_tool("put", {"key": TEST_KEY_STREAMABLE, "value": payload})).content
                    )
                    assert put_result == {"status": "ok"}

                    get_result = parse_tool_result(
                        (await session.call_tool("get", {"key": TEST_KEY_STREAMABLE})).content
                    )
                    assert get_result == payload, f"Round-trip mismatch: {get_result!r}"

        run_async(go())

    def test_streamable_keys_includes_test_key(self, server_url):
        """Streamable HTTP: keys() lists the key written in the previous test."""
        async def go():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = parse_tool_result(
                        (await session.call_tool("keys", {})).content
                    )
                    assert TEST_KEY_STREAMABLE in result["keys"]

        run_async(go())

    def test_streamable_delete(self, server_url):
        """Streamable HTTP: delete removes the key."""
        async def go():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    del_result = parse_tool_result(
                        (await session.call_tool("delete", {"key": TEST_KEY_STREAMABLE})).content
                    )
                    assert del_result == {"status": "deleted"}

                    get_result = parse_tool_result(
                        (await session.call_tool("get", {"key": TEST_KEY_STREAMABLE})).content
                    )
                    assert "error" in get_result

        run_async(go())

    def test_streamable_get_missing_key(self, server_url):
        """Streamable HTTP: getting a non-existent key returns an error dict."""
        async def go():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = parse_tool_result(
                        (await session.call_tool("get", {"key": "_test/does-not-exist"})).content
                    )
                    assert "error" in result

        run_async(go())


# ---------------------------------------------------------------------------
# Cross-transport tests: both transports see the same data
# ---------------------------------------------------------------------------

class TestCrossTransport:

    def test_put_via_sse_get_via_streamable(self, server_url):
        """Data written via SSE is immediately visible via Streamable HTTP."""
        payload = {"written_by": "sse", "read_by": "streamable"}

        async def write_via_sse():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    await session.call_tool("put", {"key": TEST_KEY_CROSS, "value": payload})

        async def read_via_streamable():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return parse_tool_result(
                        (await session.call_tool("get", {"key": TEST_KEY_CROSS})).content
                    )

        run_async(write_via_sse())
        result = run_async(read_via_streamable())
        assert result == payload

    def test_put_via_streamable_get_via_sse(self, server_url):
        """Data written via Streamable HTTP is immediately visible via SSE."""
        payload = {"written_by": "streamable", "read_by": "sse"}

        async def write_via_streamable():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("put", {"key": TEST_KEY_CROSS, "value": payload})

        async def read_via_sse():
            async with sse_client(
                f"{server_url}/mcp/",
                headers={"Authorization": f"Bearer {BEARER}"},
                sse_read_timeout=TIMEOUT,
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    return parse_tool_result(
                        (await session.call_tool("get", {"key": TEST_KEY_CROSS})).content
                    )

        run_async(write_via_streamable())
        result = run_async(read_via_sse())
        assert result == payload

    def test_cleanup(self, server_url):
        """Remove cross-transport test key."""
        async def go():
            async with streamablehttp_client(
                f"{server_url}/mcp",
                headers={"Authorization": f"Bearer {BEARER}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("delete", {"key": TEST_KEY_CROSS})

        run_async(go())
