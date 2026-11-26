"""Notes release identity. Usage and release procedure: notes gdata-server/mcp-server."""

from importlib.metadata import version
import json
import logging
from pathlib import Path
import subprocess


def get_version_info(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    release = (root / "NOTES_VERSION").read_text().strip()
    if not release:
        raise ValueError("NOTES_VERSION must contain a release number")
    info = {"notes_release": release, "notes_version": release,
            "mcp_sdk_version": version("mcp"), "git_revision": None}
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--long", "--always", "--dirty",
             "--match", f"notes-v{release}"],
            cwd=root, capture_output=True, text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        logging.getLogger(__name__).warning(
            "Cannot determine Notes Git revision in %s; reporting release file only", root,
            exc_info=True,
        )
    else:
        revision = result.stdout.strip()
        prefix = f"notes-v{release}-"
        if revision.startswith(prefix):
            revision = revision[len(prefix):]
        else:
            revision = "g" + revision
        info["git_revision"] = revision
        info["notes_version"] = release + "+" + revision.replace("-", ".")
    return info


VERSION_INFO = get_version_info()
SERVER_VERSION = (f"{VERSION_INFO['notes_version']} "
                  f"(MCP SDK {VERSION_INFO['mcp_sdk_version']})")


if __name__ == "__main__":
    print(json.dumps(VERSION_INFO, indent=2))
