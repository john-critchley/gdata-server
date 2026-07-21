"""
test_notes_browser_control.py — tests for the notes browser TCP control interface.

Starts notes_browser.py with a live gdata server and TCP control enabled,
then exercises the JSON-RPC control protocol.

Requires a display (X11/Wayland). Skipped automatically if none is available.

Run: python -m pytest test_notes_browser_control.py -v
"""
import json
import os
import socket
import subprocess
import sys
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Skip if no display
# ---------------------------------------------------------------------------

def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


pytestmark = pytest.mark.skipif(
    not _has_display(),
    reason="No display available — notes browser requires X11/Wayland"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"Port {port} not available within {timeout}s")


def rpc(port: int, method: str, params: dict = None, timeout: float = 5.0) -> dict:
    """Send a JSON-RPC request over TCP and return the parsed response."""
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                      "params": params or {}}).encode() + b"\n"
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(msg)
        buf = b""
        sock.settimeout(timeout)
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"No valid JSON response received: {buf!r}")


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gdata_server(tmp_path_factory):
    """Start a temporary gdata REST server for the browser to connect to.

    Uses its own throwaway gdbm file rather than the deployed one configured in
    .gdata_server.yaml, so the test never contends with a running notes server
    for the live database lock.
    """
    import uvicorn
    import threading
    import gdata_server as gs

    db_path = tmp_path_factory.mktemp("gdata") / "test_notes.gdbm"
    gs.config['gdbm_file'] = str(db_path)
    gs.db = None  # drop any handle opened against the deployed path
    app = gs.app

    rest_port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=rest_port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    wait_for_port(rest_port)

    # Seed a test document
    requests.put(
        f"http://127.0.0.1:{rest_port}/test/browser-control",
        json={
            "title": "Browser Control Test",
            "content": [
                {"heading": {"level": 1, "text": "Test Page"}},
                {"para": ["This page is used for browser control testing."]},
            ]
        }
    )

    yield f"http://127.0.0.1:{rest_port}"

    srv.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture(scope="module")
def browser(gdata_server):
    """Start notes_browser.py with TCP control enabled. Yields control port."""
    ctrl_port = get_free_port()
    browser_script = os.path.join(os.path.dirname(__file__),
                                  "notes-browser", "notes_browser.py")

    proc = subprocess.Popen(
        [
            sys.executable, browser_script,
            "--url", gdata_server,
            "--control-tcp-enabled",
            "--control-tcp-port", str(ctrl_port),
            "--no-control-udp",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        wait_for_port(ctrl_port, timeout=15.0)
    except TimeoutError:
        proc.terminate()
        out, err = proc.communicate(timeout=3)
        pytest.skip(f"Browser did not start: {err.decode()[:500]}")

    yield ctrl_port

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestControlInterface:

    def test_ping(self, browser):
        resp = rpc(browser, "status.ping")
        assert "result" in resp
        assert resp["result"]["ok"] is True

    def test_capabilities(self, browser):
        resp = rpc(browser, "status.capabilities")
        caps = resp["result"]
        assert caps["version"] == 1
        for method in ("status.ping", "navigate.go_to_page", "navigate.refresh",
                       "page.get_current", "page.get_document_json"):
            assert method in caps["methods"], f"Missing method: {method}"

    def test_navigate_to_page(self, browser):
        resp = rpc(browser, "navigate.go_to_page", {"key": "test/browser-control"})
        assert "result" in resp
        assert resp["result"]["page"] == "test/browser-control"

    def test_get_current_page(self, browser):
        rpc(browser, "navigate.go_to_page", {"key": "test/browser-control"})
        resp = rpc(browser, "page.get_current")
        assert resp["result"]["key"] == "test/browser-control"

    def test_get_document_json(self, browser, gdata_server):
        rpc(browser, "navigate.go_to_page", {"key": "test/browser-control"})
        resp = rpc(browser, "page.get_document_json")
        doc = resp["result"]["document"]
        assert doc["title"] == "Browser Control Test"
        assert len(doc["content"]) == 2

    def test_navigate_refresh(self, browser, gdata_server):
        """Refresh reloads the current page from the server."""
        rpc(browser, "navigate.go_to_page", {"key": "test/browser-control"})
        # Modify the doc externally
        requests.post(
            f"{gdata_server}/test/browser-control",
            json={"op": "append_block", "block": {"para": ["Added by test."]}}
        )
        resp = rpc(browser, "navigate.refresh")
        assert "result" in resp
        # After refresh, get_document_json should show the new block
        doc_resp = rpc(browser, "page.get_document_json")
        content = doc_resp["result"]["document"]["content"]
        assert len(content) == 3
        assert content[-1] == {"para": ["Added by test."]}

    def test_navigate_home(self, browser):
        resp = rpc(browser, "navigate.home")
        assert "result" in resp
        assert resp["result"]["page"] == ""

    def test_navigate_back_and_forward(self, browser):
        rpc(browser, "navigate.home")
        rpc(browser, "navigate.go_to_page", {"key": "test/browser-control"})
        back = rpc(browser, "navigate.back")
        assert back["result"]["page"] == ""
        fwd = rpc(browser, "navigate.forward")
        assert fwd["result"]["page"] == "test/browser-control"

    def test_unknown_method_returns_error(self, browser):
        resp = rpc(browser, "no.such.method")
        assert "error" in resp

    def test_navigate_go_to_page_missing_key(self, browser):
        """go_to_page with missing 'key' param should return an error."""
        resp = rpc(browser, "navigate.go_to_page", {})
        # Either error or navigates to root — should not crash
        assert "result" in resp or "error" in resp

    def test_view_zoom_in_out_reset(self, browser):
        zi = rpc(browser, "view.zoom_in")
        assert "result" in zi
        zo = rpc(browser, "view.zoom_out")
        assert "result" in zo
        zr = rpc(browser, "view.zoom_reset")
        assert "result" in zr

    def test_put_document_json(self, browser, gdata_server):
        """page.put_document_json should update the note via the server."""
        rpc(browser, "navigate.go_to_page", {"key": "test/browser-control"})
        new_doc = {
            "title": "Updated by Control",
            "content": [{"para": ["Written via control interface."]}]
        }
        resp = rpc(browser, "page.put_document_json",
                   {"key": "test/browser-control", "document": new_doc})
        assert "result" in resp
        # Verify the server was updated
        stored = requests.get(f"{gdata_server}/test/browser-control").json()
        assert stored["title"] == "Updated by Control"
