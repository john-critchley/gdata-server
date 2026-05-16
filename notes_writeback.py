#!/usr/bin/env python3
"""
notes_writeback.py — POST handler for writable_note elements.

This module is imported and reloaded by notes.wsgi on each request,
so changes here don't require a server restart.

All POST processing logic lives here; notes.wsgi delegates to handle_post().
"""

import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


# Web server configuration
WEBDAV_ROOT = '/var/www/webdav'
WRITEBACK_DIR = os.path.join(WEBDAV_ROOT, 'writeback')


def _iso8601_now() -> str:
    """Return current time in ISO 8601 format with Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _safe_notename(notename: str) -> str:
    """Convert notename to safe filesystem name (replace / with __)."""
    return notename.replace('/', '__')


def _validate_notename(notename: str) -> tuple[bool, str]:
    """
    Validate that notename matches regex: ^[a-zA-Z0-9_-]+$
    Returns (is_valid, error_message).
    """
    if not notename:
        return False, "notename is empty"
    if not re.match(r'^[a-zA-Z0-9_\-]+$', notename):
        return False, "notename contains invalid characters (allowed: a-z A-Z 0-9 _ -)"
    return True, ""


def _ensure_writeback_dir():
    """Create writeback directory if it doesn't exist."""
    try:
        os.makedirs(WRITEBACK_DIR, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"Cannot create writeback directory: {e}")


def _parse_json_body(body: bytes) -> dict:
    """Parse JSON body, return dict with 'text' key."""
    try:
        data = json.loads(body.decode('utf-8'))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def handle_post(environ, body_bytes: bytes) -> tuple[int, dict]:
    """
    Handle POST /writeback/<notename> request.
    
    Returns (status_code, response_dict).
    
    On success (200): {
        "status": "ok",
        "saved": true,
        "notename": "...",
        "timestamp": "2026-05-05T15:30:42Z",
        "path": "writeback/..."
    }
    
    On error (4xx/5xx): {
        "status": "error",
        "message": "description"
    }
    """
    try:
        # Extract notename from PATH_INFO
        path_info = environ.get('PATH_INFO', '')
        # path_info should be "/writeback/notename"
        parts = path_info.strip('/').split('/')
        
        if len(parts) < 2 or parts[0] != 'writeback':
            return 400, {"status": "error", "message": "invalid path"}
        
        notename = parts[1]
        
        # Validate notename
        valid, error_msg = _validate_notename(notename)
        if not valid:
            return 400, {"status": "error", "message": error_msg}
        
        # Parse POST body
        data = _parse_json_body(body_bytes)
        text = data.get('text', '')
        
        # Ensure writeback directory exists
        _ensure_writeback_dir()
        
        # Generate timestamp and safe filename
        timestamp = _iso8601_now()
        safe_name = _safe_notename(notename)
        filename = f"{safe_name}_{timestamp}.txt"
        filepath = os.path.join(WRITEBACK_DIR, filename)
        
        # Write file
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(text)
        except OSError as e:
            return 500, {"status": "error", "message": f"cannot write file: {e}"}
        
        # Return success
        return 200, {
            "status": "ok",
            "saved": True,
            "notename": notename,
            "timestamp": timestamp,
            "path": f"writeback/{filename}"
        }
    
    except Exception as e:
        return 500, {"status": "error", "message": f"server error: {e}"}
