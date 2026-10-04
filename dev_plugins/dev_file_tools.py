"""Confined file-management tools for the MCP development directory."""

import json
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path('/home/john/py/mcp-tools').resolve()


def _path(relative_path, *, allow_missing=False):
    if relative_path == '' and allow_missing is False:
        relative_path = '.'
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError('path is required')
    candidate = (ROOT / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError('path must remain inside /home/john/py/mcp-tools') from exc
    return candidate


def _text(value, field='content'):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    return value


def _lines(content):
    return content.splitlines(keepends=True)


def _line_range(arguments, count):
    start = arguments.get('start_line', 1)
    end = arguments.get('end_line', count)
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError('start_line and end_line must be integers')
    if start < 1 or end < start or end > count:
        raise ValueError(f'line range must be between 1 and {count}, inclusive')
    return start - 1, end


def _write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _create(arguments):
    path = _path(arguments.get('path'), allow_missing=True)
    if path.exists():
        raise FileExistsError(f'file already exists: {arguments["path"]}')
    content = _text(arguments.get('content', ''))
    _write_atomic(path, content)
    return {'status': 'ok', 'operation': 'create', 'path': arguments['path'], 'bytes': len(content.encode('utf-8'))}


def _read(arguments):
    path = _path(arguments.get('path'))
    if not path.is_file():
        raise IsADirectoryError(f'not a file: {arguments["path"]}')
    content = path.read_text(encoding='utf-8')
    lines = _lines(content)
    if 'start_line' in arguments or 'end_line' in arguments:
        start, end = _line_range(arguments, len(lines))
        content = ''.join(lines[start:end])
        return {'status': 'ok', 'operation': 'read', 'path': arguments['path'], 'start_line': start + 1, 'end_line': end, 'content': content}
    return {'status': 'ok', 'operation': 'read', 'path': arguments['path'], 'line_count': len(lines), 'content': content}


def _edit_lines(arguments, operation):
    path = _path(arguments.get('path'))
    if not path.is_file():
        raise IsADirectoryError(f'not a file: {arguments["path"]}')
    old = path.read_text(encoding='utf-8')
    lines = _lines(old)
    if operation == 'insert_lines' and arguments.get('start_line') == len(lines) + 1:
        start, end = len(lines), len(lines)
    else:
        start, end = _line_range(arguments, len(lines))
    replacement = [] if operation == 'delete_lines' else _lines(_text(arguments.get('content'), 'content'))
    if operation == 'insert_lines':
        updated = lines[:start] + replacement + lines[start:]
    elif operation == 'replace_lines':
        updated = lines[:start] + replacement + lines[end:]
    else:
        updated = lines[:start] + lines[end:]
    _write_atomic(path, ''.join(updated))
    return {'status': 'ok', 'operation': operation, 'path': arguments['path'], 'line_count': len(updated)}


def _list_directory(arguments):
    path = _path(arguments.get('path', ''), allow_missing=False)
    if not path.is_dir():
        raise NotADirectoryError(f'not a directory: {arguments.get("path", "") or "."}')
    entries = []
    for entry in sorted(path.iterdir(), key=lambda item: item.name):
        entries.append({'name': entry.name, 'type': 'directory' if entry.is_dir() else 'file'})
    return {'status': 'ok', 'operation': 'list_directory', 'path': arguments.get('path', ''), 'entries': entries}


def _mkdir(arguments):
    path = _path(arguments.get('path'), allow_missing=True)
    path.mkdir(parents=bool(arguments.get('parents', True)), exist_ok=False)
    return {'status': 'ok', 'operation': 'mkdir', 'path': arguments['path']}


def _delete(arguments):
    path = _path(arguments.get('path'))
    if path == ROOT:
        raise ValueError('cannot delete the plugin root directory')
    if path.is_dir():
        if not arguments.get('recursive', False):
            raise IsADirectoryError('recursive=true is required to delete a directory')
        shutil.rmtree(path)
    else:
        path.unlink()
    return {'status': 'ok', 'operation': 'delete', 'path': arguments['path']}


def _rename(arguments):
    source = _path(arguments.get('path'))
    destination = _path(arguments.get('new_path'), allow_missing=True)
    if destination.exists():
        raise FileExistsError(f'destination already exists: {arguments["new_path"]}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return {'status': 'ok', 'operation': 'rename', 'path': arguments['path'], 'new_path': arguments['new_path']}


async def dispatch(operation, arguments):
    """Dispatch one confined development-directory file operation."""
    handlers = {
        'create': _create,
        'read': _read,
        'insert_lines': lambda args: _edit_lines(args, 'insert_lines'),
        'replace_lines': lambda args: _edit_lines(args, 'replace_lines'),
        'delete_lines': lambda args: _edit_lines(args, 'delete_lines'),
        'list_directory': _list_directory,
        'mkdir': _mkdir,
        'delete': _delete,
        'rename': _rename,
    }
    if operation not in handlers:
        raise ValueError(f'unsupported operation: {operation}')
    return json.dumps(handlers[operation](arguments))
