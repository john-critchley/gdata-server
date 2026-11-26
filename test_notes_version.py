"""Check release identities before/after tags and without Git metadata."""
import subprocess

from notes_version import get_version_info, SERVER_VERSION
from gdata_mcp_server import _make_tool_server


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def test_revision_changes_without_manual_bump(tmp_path):
    (tmp_path / "NOTES_VERSION").write_text("1.0\n")
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Version test")
    git(tmp_path, "add", "NOTES_VERSION")
    git(tmp_path, "commit", "-m", "Initial release")
    untagged = get_version_info(tmp_path)
    assert untagged["notes_version"].startswith("1.0+g")
    git(tmp_path, "tag", "notes-v1.0")
    released = get_version_info(tmp_path)
    assert released["notes_version"].startswith("1.0+0.g")
    (tmp_path / "change").write_text("new code")
    git(tmp_path, "add", "change")
    git(tmp_path, "commit", "-m", "Code changed without a release bump")
    changed = get_version_info(tmp_path)
    assert changed["notes_version"].startswith("1.0+1.g")
    assert changed != released
    (tmp_path / "change").write_text("uncommitted code")
    assert get_version_info(tmp_path)["notes_version"].endswith(".dirty")
    (tmp_path / "NOTES_VERSION").write_text("1.1\n")
    assert get_version_info(tmp_path)["notes_version"].startswith("1.1+g")


def test_release_file_without_git(tmp_path, caplog):
    (tmp_path / "NOTES_VERSION").write_text("1.0\n")
    info = get_version_info(tmp_path)
    assert info["notes_version"] == "1.0"
    assert info["git_revision"] is None
    assert info["mcp_sdk_version"]
    assert "Cannot determine Notes Git revision" in caplog.text


def test_both_notes_stores_report_application_and_sdk():
    for store in ("public", "private"):
        options = _make_tool_server("unused", store).create_initialization_options()
        assert options.server_version == SERVER_VERSION
        assert "1.0+" in options.server_version
        assert "MCP SDK " in options.server_version
