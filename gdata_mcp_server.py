#!/usr/bin/python
"""
gdata_mcp_server.py — combined REST + MCP server over gdbm.

Runs two uvicorn servers in one process (shared db, no locking conflicts):
  - REST API on --rest-port (default 8020)  — same interface as gdata_server.py
  - MCP      on --mcp-port  (default 8022)  — SSE and/or Streamable HTTP

Transport flags (env vars, default both enabled):
  MCP_SSE=true|false          enable SSE transport  (GET /mcp/ + POST /mcp/messages)
  MCP_STREAMABLE=true|false   enable Streamable HTTP (POST /mcp/)

All db access is serialised via a single asyncio.Lock.
"""

import argparse
import asyncio
import copy
import functools
import json
import logging
import os
import random
import string
import sys

import fastapi
import uvicorn
import urllib.parse
from fastapi.responses import Response
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import contextlib
import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gdata
import gdata_oauth
import notes_web

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Robust JSON parsing (handles shell-escaped quotes)
# ---------------------------------------------------------------------------

def _parse_json_robust(text: str):
    """Parse a client-supplied JSON string, tolerating one common mistake:
    escaping apostrophes as \\' inside string values (e.g. "I\\'ll"). \\' is
    never valid JSON -- the only recognised escapes are \\" \\\\ \\/ \\b \\f
    \\n \\r \\t and \\uXXXX -- so any occurrence can be safely unescaped to a
    literal apostrophe and re-parsed.

    Mirrors gdata_server.py's _parse_json_robust -- keep in sync.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        if "\\'" not in text:
            raise
        unescaped = text.replace("\\'", "'")
        try:
            return json.loads(unescaped)
        except json.JSONDecodeError as second_error:
            raise json.JSONDecodeError(
                f"invalid JSON, and apostrophe-unescape repair did not fix it "
                f"(original error: {first_error}; after unescaping \\': {second_error})",
                second_error.doc, second_error.pos
            ) from first_error

# ---------------------------------------------------------------------------
# DB singleton + lock
# ---------------------------------------------------------------------------

_db = None
_db_lock = asyncio.Lock()


def _open_db(path: str):
    global _db
    if _db is None:
        _db = gdata.gdata_local_simple(gdbm_file=path)
    return _db


# ---------------------------------------------------------------------------
# Sidecar helpers (mirrors gdata_server.py — keep in sync)
# ---------------------------------------------------------------------------

_SIDECAR_PREFIX = '\x00'


def _sidecar_key(key: str) -> str:
    return _SIDECAR_PREFIX + key


def _new_block_id() -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _increment_rev(rev: str) -> str:
    return f'r{int(rev[1:]) + 1}'


def _get_sidecar(db, key: str) -> dict | None:
    sk = _sidecar_key(key)
    if sk not in db:
        return None
    try:
        return json.loads(db[sk])
    except (json.JSONDecodeError, KeyError):
        return None


def _save_sidecar(db, key: str, sidecar: dict):
    db[_sidecar_key(key)] = json.dumps(sidecar)


def _get_or_create_sidecar(db, key: str, content: list) -> dict:
    sidecar = _get_sidecar(db, key)
    if sidecar is None:
        sidecar = {'rev': 'r1', 'block_ids': [_new_block_id() for _ in content]}
        _save_sidecar(db, key, sidecar)
    elif len(sidecar.get('block_ids', [])) != len(content):
        sidecar['block_ids'] = [_new_block_id() for _ in content]
        _save_sidecar(db, key, sidecar)
    return sidecar


def _inline_plain_text(value) -> str:
    """Return a compact plain-text representation of a JSONHTL inline value."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(_inline_plain_text(item) for item in value)
    if isinstance(value, dict):
        if 'link' in value:
            link = value['link']
            if isinstance(link, dict):
                return str(link.get('text') or link.get('href') or '')
        for key in ('code', 'em', 'strong', 'bold', 'italic'):
            if key in value:
                return _inline_plain_text(value[key])
        return ' '.join(_inline_plain_text(v) for v in value.values())
    return str(value)


def _block_type(block) -> str:
    if isinstance(block, str):
        return 'para'
    if isinstance(block, dict) and len(block) == 1:
        return next(iter(block))
    if isinstance(block, dict):
        for key in ('heading', 'para', 'list', 'codeblock', 'pre', 'table'):
            if key in block:
                return key
    return type(block).__name__


def _block_plain_text(block) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return str(block)
    if 'heading' in block and isinstance(block['heading'], dict):
        return str(block['heading'].get('text', ''))
    if 'para' in block:
        return _inline_plain_text(block['para'])
    if 'list' in block and isinstance(block['list'], dict):
        items = block['list'].get('items', [])
        return ' '.join(_inline_plain_text(item) for item in items)
    if 'codeblock' in block and isinstance(block['codeblock'], dict):
        return str(block['codeblock'].get('body', ''))
    if 'pre' in block:
        return str(block['pre'])
    if 'table' in block and isinstance(block['table'], dict):
        table = block['table']
        caption = table.get('caption')
        columns = table.get('columns', [])
        parts = []
        if caption:
            parts.append(str(caption))
        if columns:
            parts.append(' '.join(str(c) for c in columns))
        return ' '.join(parts)
    return _inline_plain_text(block)


def _preview_text(text: str, limit: int = 80) -> str:
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + '…'


def _build_outline(doc: dict, block_ids: list, preview_chars: int = 80) -> list:
    content = doc.get('content')
    blocks = content if isinstance(content, list) else []
    return [
        {
            'id': block_ids[i],
            'index': i,
            'type': _block_type(block),
            'preview': _preview_text(_block_plain_text(block), preview_chars),
        }
        for i, block in enumerate(blocks)
    ]


# ---------------------------------------------------------------------------
# Table-op helpers (mirrors gdata_server.py — keep in sync)
# ---------------------------------------------------------------------------

def _resolve_table_block(op_body: dict, content: list, block_ids: list):
    """Return (content_index, table_dict) for the 'block' field. Raises on error."""
    block_ref = op_body.get('block')
    if block_ref is None:
        raise fastapi.HTTPException(status_code=400, detail="'block' is required for table ops")
    if isinstance(block_ref, str):
        try:
            idx = block_ids.index(block_ref)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_ref!r}")
    elif isinstance(block_ref, int):
        if block_ref < 0 or block_ref >= len(content):
            raise fastapi.HTTPException(status_code=400, detail=f"block index out of range: {block_ref}")
        idx = block_ref
    else:
        raise fastapi.HTTPException(status_code=400, detail="'block' must be a block ID string or integer index")
    blk = content[idx]
    if not isinstance(blk, dict) or 'table' not in blk:
        raise fastapi.HTTPException(status_code=400, detail=f"block at index {idx} is not a table block")
    return idx, blk['table']


def _resolve_column(columns: list, column_ref, required: bool = True):
    """Return column index from a name (str) or index (int). Raises on error."""
    if column_ref is None:
        if required:
            raise fastapi.HTTPException(status_code=400, detail="'column' is required")
        return None
    if isinstance(column_ref, int):
        if column_ref < 0 or column_ref >= len(columns):
            raise fastapi.HTTPException(status_code=400, detail=f"column index out of range: {column_ref}")
        return column_ref
    try:
        return columns.index(column_ref)
    except ValueError:
        raise fastapi.HTTPException(status_code=400, detail=f"column not found: {column_ref!r}")


def _build_row(values, columns: list, default=None) -> list:
    """Build a fixed-length row from a list (positional) or dict ({col: val})."""
    if isinstance(values, dict):
        return [values.get(col, default) for col in columns]
    if isinstance(values, list):
        row = list(values)
        while len(row) < len(columns):
            row.append(default)
        return row[:len(columns)]
    return [default] * len(columns)


def _cell_type_rank(v) -> int:
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1
    if isinstance(v, (int, float)):
        return 2
    return 3


def _apply_table_op(op_body: dict, doc: dict, block_ids: list) -> dict:
    """Handle table.* ops. Mutates doc in-place. Returns result dict."""
    op = op_body.get('op')
    table_op = op[6:]  # strip 'table.'

    content = doc.get('content')
    if not isinstance(content, list):
        raise fastapi.HTTPException(status_code=400, detail="document content is not a list")

    _idx, table = _resolve_table_block(op_body, content, block_ids)
    columns = table.setdefault('columns', [])
    rows = table.setdefault('rows', [])

    if table_op == 'rename_column':
        col_i = _resolve_column(columns, op_body.get('column'))
        new_name = op_body.get('new_name')
        if not isinstance(new_name, str):
            raise fastapi.HTTPException(status_code=400, detail="'new_name' must be a string")
        columns[col_i] = new_name
        return {'status': 'ok'}

    if table_op == 'insert_column':
        name = op_body.get('name', '')
        default = op_body.get('default', None)
        values = op_body.get('values')
        after = op_body.get('after')
        position = op_body.get('position')
        if after is not None:
            pos = _resolve_column(columns, after) + 1
        elif position is not None:
            if not isinstance(position, int) or position < 0 or position > len(columns):
                raise fastapi.HTTPException(status_code=400, detail=f"'position' must be in 0–{len(columns)}")
            pos = position
        else:
            pos = len(columns)
        columns.insert(pos, name)
        if values is not None:
            if not isinstance(values, list):
                raise fastapi.HTTPException(status_code=400, detail="'values' must be a list")
            for r_i, row in enumerate(rows):
                cell = values[r_i] if r_i < len(values) else default
                while len(row) < pos:
                    row.append(None)
                row.insert(pos, cell)
        else:
            for row in rows:
                while len(row) < pos:
                    row.append(None)
                row.insert(pos, default)
        return {'status': 'ok'}

    if table_op == 'delete_column':
        col_i = _resolve_column(columns, op_body.get('column'))
        columns.pop(col_i)
        for row in rows:
            if col_i < len(row):
                row.pop(col_i)
        return {'status': 'ok'}

    if table_op == 'move_column':
        from_i = _resolve_column(columns, op_body.get('column'))
        to = op_body.get('to')
        after = op_body.get('after')
        if to is not None:
            if not isinstance(to, int) or to < 0 or to >= len(columns):
                raise fastapi.HTTPException(status_code=400, detail=f"'to' must be in 0–{len(columns) - 1}")
            dest = to
        elif after is not None:
            after_i = _resolve_column(columns, after)
            dest = after_i + 1
            if from_i < after_i:
                dest -= 1
        else:
            raise fastapi.HTTPException(status_code=400, detail="'to' or 'after' required for move_column")
        col_val = columns.pop(from_i)
        columns.insert(dest, col_val)
        for row in rows:
            if from_i < len(row):
                cell = row.pop(from_i)
                while len(row) < dest:
                    row.append(None)
                row.insert(dest, cell)
        return {'status': 'ok'}

    if table_op == 'reorder_columns':
        order = op_body.get('order')
        if not isinstance(order, list) or len(order) != len(columns):
            raise fastapi.HTTPException(status_code=400, detail=f"'order' must be a list of {len(columns)} column names")
        if sorted(str(c) for c in order) != sorted(str(c) for c in columns):
            raise fastapi.HTTPException(status_code=400, detail="'order' must contain the same column names as the existing columns")
        new_indices = [columns.index(c) for c in order]
        columns[:] = order
        for row in rows:
            old = list(row)
            row[:] = [old[i] if i < len(old) else None for i in new_indices]
        return {'status': 'ok'}

    if table_op == 'fill_column':
        col_i = _resolve_column(columns, op_body.get('column'))
        if 'value' not in op_body:
            raise fastapi.HTTPException(status_code=400, detail="'value' is required for fill_column")
        value = op_body['value']
        for row in rows:
            while len(row) <= col_i:
                row.append(None)
            row[col_i] = value
        return {'status': 'ok'}

    if table_op == 'set_columns':
        new_cols = op_body.get('columns')
        if not isinstance(new_cols, list) or len(new_cols) != len(columns):
            raise fastapi.HTTPException(status_code=400, detail=f"'columns' must be a list of {len(columns)} names")
        columns[:] = new_cols
        return {'status': 'ok'}

    if table_op == 'insert_row':
        position = op_body.get('position')
        if not isinstance(position, int) or position < 0 or position > len(rows):
            raise fastapi.HTTPException(status_code=400, detail=f"'position' must be an integer in 0–{len(rows)}")
        row = _build_row(op_body.get('values'), columns, op_body.get('default'))
        rows.insert(position, row)
        return {'status': 'ok'}

    if table_op == 'append_row':
        row = _build_row(op_body.get('values'), columns, op_body.get('default'))
        rows.append(row)
        return {'status': 'ok'}

    if table_op == 'delete_row':
        row_ref = op_body.get('row')
        if row_ref is None:
            index_val = op_body.get('index')
            if index_val is None:
                raise fastapi.HTTPException(status_code=400, detail="'row' is required for delete_row")
            index_col = table.get('index_col')
            if index_col is None:
                raise fastapi.HTTPException(status_code=400, detail="'index' addressing requires set_index to have been called first")
            col_i = _resolve_column(columns, index_col)
            row_ref = next(
                (i for i, r in enumerate(rows) if (r[col_i] if col_i < len(r) else None) == index_val),
                None
            )
            if row_ref is None:
                raise fastapi.HTTPException(status_code=404, detail=f"no row with index value {index_val!r}")
        if not isinstance(row_ref, int) or row_ref < 0 or row_ref >= len(rows):
            raise fastapi.HTTPException(status_code=400, detail=f"'row' must be in 0–{len(rows) - 1}")
        rows.pop(row_ref)
        return {'status': 'ok'}

    if table_op == 'move_row':
        row_ref = op_body.get('row')
        to = op_body.get('to')
        if not isinstance(row_ref, int) or row_ref < 0 or row_ref >= len(rows):
            raise fastapi.HTTPException(status_code=400, detail=f"'row' must be in 0–{len(rows) - 1}")
        if not isinstance(to, int) or to < 0 or to >= len(rows):
            raise fastapi.HTTPException(status_code=400, detail=f"'to' must be in 0–{len(rows) - 1}")
        row = rows.pop(row_ref)
        rows.insert(to, row)
        return {'status': 'ok'}

    if table_op == 'sort':
        by = op_body.get('by')
        if by is None:
            raise fastapi.HTTPException(status_code=400, detail="'by' is required for sort")
        ascending = op_body.get('ascending', True)
        by_list = [by] if isinstance(by, str) else by
        if not isinstance(by_list, list):
            raise fastapi.HTTPException(status_code=400, detail="'by' must be a column name or list of names")
        if isinstance(ascending, bool):
            asc_list = [ascending] * len(by_list)
        elif isinstance(ascending, list):
            if len(ascending) != len(by_list):
                raise fastapi.HTTPException(status_code=400, detail="'ascending' list must match length of 'by' list")
            asc_list = ascending
        else:
            raise fastapi.HTTPException(status_code=400, detail="'ascending' must be a bool or list of bool")
        resolved_cols = [_resolve_column(columns, c) for c in by_list]

        def _cell_cmp(a, b):
            ra, rb = _cell_type_rank(a), _cell_type_rank(b)
            if ra != rb:
                return (ra > rb) - (ra < rb)
            if a is None:
                return 0
            if isinstance(a, bool):
                return (int(a) > int(b)) - (int(a) < int(b))
            if isinstance(a, (int, float)):
                return (a > b) - (a < b)
            return (str(a) > str(b)) - (str(a) < str(b))

        def _row_cmp(row_a, row_b):
            for col_i, asc in zip(resolved_cols, asc_list):
                ca = row_a[col_i] if col_i < len(row_a) else None
                cb = row_b[col_i] if col_i < len(row_b) else None
                c = _cell_cmp(ca, cb)
                if not asc:
                    c = -c
                if c != 0:
                    return c
            return 0

        rows.sort(key=functools.cmp_to_key(_row_cmp))
        return {'status': 'ok'}

    if table_op == 'fill_row':
        row_ref = op_body.get('row')
        if not isinstance(row_ref, int) or row_ref < 0 or row_ref >= len(rows):
            raise fastapi.HTTPException(status_code=400, detail=f"'row' must be in 0–{len(rows) - 1}")
        if 'value' not in op_body:
            raise fastapi.HTTPException(status_code=400, detail="'value' is required for fill_row")
        rows[row_ref] = [op_body['value']] * len(columns)
        return {'status': 'ok'}

    if table_op == 'set_cell':
        row_ref = op_body.get('row')
        if not isinstance(row_ref, int) or row_ref < 0 or row_ref >= len(rows):
            raise fastapi.HTTPException(status_code=400, detail=f"'row' must be in 0–{len(rows) - 1}")
        col_i = _resolve_column(columns, op_body.get('column'))
        if 'value' not in op_body:
            raise fastapi.HTTPException(status_code=400, detail="'value' is required for set_cell")
        row = rows[row_ref]
        while len(row) <= col_i:
            row.append(None)
        row[col_i] = op_body['value']
        return {'status': 'ok'}

    if table_op == 'set_caption':
        caption = op_body.get('caption')
        if caption is None:
            table.pop('caption', None)
        else:
            if not isinstance(caption, str):
                raise fastapi.HTTPException(status_code=400, detail="'caption' must be a string or null")
            table['caption'] = caption
        return {'status': 'ok'}

    if table_op == 'transpose':
        if not columns:
            return {'status': 'ok'}
        n_rows = len(rows)
        first_col_vals = [row[0] if len(row) > 0 else None for row in rows]
        new_columns = [str(columns[0])] + [str(v) if v is not None else '' for v in first_col_vals]
        new_rows = [
            [str(columns[ci])] + [rows[ri][ci] if ci < len(rows[ri]) else None for ri in range(n_rows)]
            for ci in range(1, len(columns))
        ]
        table['columns'] = new_columns
        table['rows'] = new_rows
        return {'status': 'ok'}

    if table_op == 'set_index':
        col_i = _resolve_column(columns, op_body.get('column'))
        table['index_col'] = columns[col_i]
        return {'status': 'ok'}

    if table_op == 'clear_index':
        table.pop('index_col', None)
        return {'status': 'ok'}

    if table_op == 'replace':
        col_ref = op_body.get('column', None)
        if 'old_value' not in op_body:
            raise fastapi.HTTPException(status_code=400, detail="'old_value' is required for replace")
        if 'new_value' not in op_body:
            raise fastapi.HTTPException(status_code=400, detail="'new_value' is required for replace")
        old_value = op_body['old_value']
        new_value = op_body['new_value']
        col_indices = ([_resolve_column(columns, col_ref)] if col_ref is not None
                       else list(range(len(columns))))
        count = 0
        for row in rows:
            for ci in col_indices:
                if ci < len(row) and row[ci] == old_value:
                    row[ci] = new_value
                    count += 1
        return {'status': 'ok', 'replaced': count}

    if table_op == 'deduplicate':
        subset = op_body.get('subset')
        keep = op_body.get('keep', 'first')
        if keep not in ('first', 'last', 'none'):
            raise fastapi.HTTPException(status_code=400, detail="'keep' must be 'first', 'last', or 'none'")
        col_indices = (
            [_resolve_column(columns, c) for c in subset]
            if subset is not None
            else list(range(len(columns)))
        )

        def _row_key(row):
            return tuple(row[ci] if ci < len(row) else None for ci in col_indices)

        seen = {}
        for i, row in enumerate(rows):
            k = _row_key(row)
            seen.setdefault(k, []).append(i)

        if keep == 'first':
            remove = {idxs[j] for idxs in seen.values() for j in range(1, len(idxs))}
        elif keep == 'last':
            remove = {idxs[j] for idxs in seen.values() for j in range(0, len(idxs) - 1)}
        else:  # 'none'
            remove = {i for idxs in seen.values() if len(idxs) > 1 for i in idxs}

        for i in sorted(remove, reverse=True):
            rows.pop(i)
        return {'status': 'ok', 'removed': len(remove)}

    _SUPPORTED_TABLE_OPS = (
        'rename_column, insert_column, delete_column, move_column, reorder_columns, fill_column, set_columns, '
        'insert_row, append_row, delete_row, move_row, sort, fill_row, deduplicate, '
        'set_cell, set_caption, transpose, set_index, clear_index, replace'
    )
    raise fastapi.HTTPException(
        status_code=400,
        detail=f"unknown table op: {table_op!r}. Supported: {_SUPPORTED_TABLE_OPS}"
    )


def _apply_op(op_body: dict, doc: dict, block_ids: list) -> dict:
    """Apply one patch op to doc and block_ids in-place (no DB access)."""
    op = op_body.get('op')

    if op == 'patch_meta':
        fields = op_body.get('fields')
        if not isinstance(fields, dict):
            raise fastapi.HTTPException(status_code=400, detail="'fields' must be an object")
        if 'content' in fields:
            raise fastapi.HTTPException(status_code=400, detail="'content' cannot be updated via patch_meta")
        doc.update(fields)
        return {'status': 'ok'}

    content = doc.get('content')
    if content is None:
        raise fastapi.HTTPException(status_code=409, detail="document has no 'content' key")
    if not isinstance(content, list):
        raise fastapi.HTTPException(
            status_code=400,
            detail="'content' is not a list; cannot patch block-level (PUT the full document first)"
        )

    block_id = op_body.get('block_id')

    if op == 'insert_before':
        if block_id is None:
            raise fastapi.HTTPException(status_code=400, detail="'block_id' is required for insert_before")
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for insert_before")
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        new_id = _new_block_id()
        content.insert(idx, block)
        block_ids.insert(idx, new_id)
        return {'status': 'ok', 'inserted_block_id': new_id}

    if op == 'insert_after':
        if block_id is None:
            raise fastapi.HTTPException(status_code=400, detail="'block_id' is required for insert_after")
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for insert_after")
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        new_id = _new_block_id()
        content.insert(idx + 1, block)
        block_ids.insert(idx + 1, new_id)
        return {'status': 'ok', 'inserted_block_id': new_id}

    if op == 'replace_block' and block_id is not None:
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for replace_block")
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        content[idx] = block
        return {'status': 'ok'}

    if op == 'delete_block' and block_id is not None:
        try:
            idx = block_ids.index(block_id)
        except ValueError:
            raise fastapi.HTTPException(status_code=404, detail=f"block_id not found: {block_id!r}")
        content.pop(idx)
        block_ids.pop(idx)
        return {'status': 'ok'}

    if op == 'append_block':
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail="'block' is required for append_block")
        new_id = _new_block_id()
        content.append(block)
        block_ids.append(new_id)
        return {'status': 'ok', 'inserted_block_id': new_id}

    if op == 'delete_blocks':
        indices = op_body.get('indices')
        if not isinstance(indices, list) or not indices:
            raise fastapi.HTTPException(status_code=400, detail="'indices' must be a non-empty list for delete_blocks")
        if not all(isinstance(i, int) and i >= 0 for i in indices):
            raise fastapi.HTTPException(status_code=400, detail="all indices must be non-negative integers")
        max_index = len(content) - 1
        out_of_range = [i for i in indices if i > max_index]
        if out_of_range:
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"indices out of range: {out_of_range} (content has {len(content)} block(s))"
            )
        for i in sorted(set(indices), reverse=True):
            content.pop(i)
            block_ids.pop(i)
        return {'status': 'ok'}

    if op in ('insert_block', 'replace_block', 'delete_block'):
        index = op_body.get('index')
        if index is None:
            raise fastapi.HTTPException(status_code=400, detail=f"'index' is required for {op}")
        if not isinstance(index, int) or index < 0:
            raise fastapi.HTTPException(status_code=400, detail="'index' must be a non-negative integer")
        max_index = len(content) if op == 'insert_block' else len(content) - 1
        if index > max_index:
            raise fastapi.HTTPException(
                status_code=400,
                detail=f"index {index} out of range (content has {len(content)} block(s))"
            )
        if op == 'delete_block':
            content.pop(index)
            block_ids.pop(index)
            return {'status': 'ok'}
        block = op_body.get('block')
        if block is None:
            raise fastapi.HTTPException(status_code=400, detail=f"'block' is required for {op}")
        if op == 'insert_block':
            new_id = _new_block_id()
            content.insert(index, block)
            block_ids.insert(index, new_id)
            return {'status': 'ok', 'inserted_block_id': new_id}
        else:
            content[index] = block
            return {'status': 'ok'}

    if op == 'reorder':
        order = op_body.get('order')
        if not isinstance(order, list):
            raise fastapi.HTTPException(status_code=400, detail="'order' must be a list of block IDs")

        current_ids = block_ids  # same list, just for clarity
        seen: set = set()
        duplicates = []
        for bid in order:
            if bid in seen:
                duplicates.append(bid)
            seen.add(bid)

        current_set = set(current_ids)
        unknown = [bid for bid in order if bid not in current_set]
        missing = [bid for bid in current_ids if bid not in seen]

        errors: dict = {}
        if duplicates:
            errors['duplicates'] = duplicates
        if unknown:
            errors['unknown'] = unknown
        if missing:
            errors['missing'] = missing

        if errors:
            raise fastapi.HTTPException(
                status_code=422,
                detail={
                    'error': 'reorder validation failed',
                    **errors,
                    'hint': 'order must list every current block ID exactly once',
                }
            )

        id_to_block = {bid: content[i] for i, bid in enumerate(current_ids)}
        content[:] = [id_to_block[bid] for bid in order]
        block_ids[:] = list(order)
        return {'status': 'ok'}

    if isinstance(op, str) and op.startswith('table.'):
        return _apply_table_op(op_body, doc, block_ids)

    raise fastapi.HTTPException(
        status_code=400,
        detail=(
            f"unknown op: {op!r}. Supported: append_block, insert_block, replace_block, "
            "delete_block, delete_blocks, patch_meta, insert_before, insert_after, "
            "reorder, batch, get_with_block_ids, outline, table.*"
        )
    )


# ---------------------------------------------------------------------------
# Async DB operations
# ---------------------------------------------------------------------------

async def db_get(db_path: str, key: str):
    async with _db_lock:
        db = _open_db(db_path)
        if key not in db:
            return None, False
        return db[key], True


async def db_put(db_path: str, key: str, value_json: str, if_rev: str | None = None):
    async with _db_lock:
        db = _open_db(db_path)
        old_sidecar = _get_sidecar(db, key)

        if if_rev is not None:
            current_rev = old_sidecar['rev'] if old_sidecar else None
            if current_rev != if_rev:
                raise fastapi.HTTPException(
                    status_code=412,
                    detail=f"revision mismatch: expected {if_rev!r}, current rev is {current_rev!r}"
                )

        try:
            doc = json.loads(value_json)
        except json.JSONDecodeError as e:
            raise fastapi.HTTPException(status_code=400, detail=f"invalid JSON: {e}")

        # Storing arbitrary JSON (lists, strings, numbers) is intentionally
        # supported -- this is a general KV store, not JSONHTL-only. But a
        # list shaped like an ops/patch payload (e.g. from `notes load` given
        # an ops file by mistake) would silently and permanently break this
        # key for future patch calls, so reject that specific shape.
        if isinstance(doc, list) and doc and isinstance(doc[0], dict) and 'op' in doc[0]:
            raise fastapi.HTTPException(
                status_code=400,
                detail=(
                    "value looks like an ops/patch payload (a list of objects with an 'op' key), "
                    "not a document -- storing it as-is would permanently break patch on this key. "
                    "Use patch/batch to apply ops, or store a plain document/value instead."
                ),
            )

        db[key] = value_json

        content = doc.get('content', []) if isinstance(doc, dict) else []
        if not isinstance(content, list):
            content = []

        new_sidecar = {
            'rev': _increment_rev(old_sidecar['rev'] if old_sidecar else 'r0'),
            'block_ids': [_new_block_id() for _ in content],
        }
        _save_sidecar(db, key, new_sidecar)
        return new_sidecar


async def db_delete(db_path: str, key: str) -> bool:
    async with _db_lock:
        db = _open_db(db_path)
        if key not in db:
            return False
        del db[key]
        sk = _sidecar_key(key)
        if sk in db:
            del db[sk]
        return True


async def db_keys(db_path: str) -> list[str]:
    async with _db_lock:
        return sorted(k for k in _open_db(db_path).keys() if not k.startswith(_SIDECAR_PREFIX))


async def db_dump(db_path: str) -> dict:
    async with _db_lock:
        db = _open_db(db_path)
        items = {}
        for k in sorted(k for k in db.keys() if not k.startswith(_SIDECAR_PREFIX)):
            raw = db[k]
            try:
                items[k] = json.loads(raw)
            except json.JSONDecodeError:
                items[k] = raw
        return items


async def db_flush(db_path: str):
    async with _db_lock:
        _open_db(db_path).db.sync()


async def db_patch(db_path: str, key: str, body: dict) -> dict:
    """
    Atomic read-modify-write for block-level patch operations.
    Returns a result dict; raises fastapi.HTTPException on error.
    """
    async with _db_lock:
        db = _open_db(db_path)

        if key not in db:
            raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")

        try:
            doc = json.loads(db[key])
        except json.JSONDecodeError:
            raise fastapi.HTTPException(status_code=400, detail="document is not valid JSON")

        if not isinstance(doc, dict):
            raise fastapi.HTTPException(status_code=400, detail="document is not a JSON object")

        op = body.get('op')

        # --- Read-only op ---
        if op == 'get_with_block_ids':
            content = doc.get('content')
            blocks = content if isinstance(content, list) else []
            sidecar = _get_or_create_sidecar(db, key, blocks)
            return {'document': doc, 'rev': sidecar['rev'], 'block_ids': sidecar['block_ids']}
        if op == 'outline':
            content = doc.get('content')
            blocks = content if isinstance(content, list) else []
            sidecar = _get_or_create_sidecar(db, key, blocks)
            preview_chars = body.get('preview_chars', 80)
            if not isinstance(preview_chars, int) or preview_chars < 1:
                raise fastapi.HTTPException(status_code=400, detail="'preview_chars' must be a positive integer")
            return {'rev': sidecar['rev'], 'blocks': _build_outline(doc, sidecar['block_ids'], preview_chars)}

        # --- Concurrency guard ---
        if_rev = body.get('if_rev')
        if if_rev is not None:
            sidecar = _get_sidecar(db, key)
            current_rev = sidecar['rev'] if sidecar else None
            if current_rev != if_rev:
                raise fastapi.HTTPException(
                    status_code=409,
                    detail=f"revision mismatch: expected {if_rev!r}, current rev is {current_rev!r}"
                )

        # --- Load/create sidecar ---
        content = doc.get('content')
        blocks = content if isinstance(content, list) else []
        sidecar = _get_or_create_sidecar(db, key, blocks)
        block_ids = sidecar['block_ids']

        # --- Batch op ---
        if op == 'batch':
            ops = body.get('ops')
            if isinstance(ops, str):
                try:
                    ops = _parse_json_robust(ops)
                except json.JSONDecodeError as e:
                    raise fastapi.HTTPException(status_code=400, detail=f"'ops' could not be parsed as JSON: {e}")
            if not isinstance(ops, list) or not ops:
                raise fastapi.HTTPException(status_code=400, detail="'ops' must be a non-empty list")

            doc_work = copy.deepcopy(doc)
            block_ids_work = list(block_ids)
            inserted_block_ids = []
            for op_body in ops:
                if not isinstance(op_body, dict):
                    raise fastapi.HTTPException(status_code=400, detail="each op in batch must be a JSON object")
                r = _apply_op(op_body, doc_work, block_ids_work)
                if 'inserted_block_id' in r:
                    inserted_block_ids.append(r['inserted_block_id'])

            new_rev = _increment_rev(sidecar['rev'])
            sidecar['rev'] = new_rev
            sidecar['block_ids'] = block_ids_work
            db[key] = json.dumps(doc_work)
            _save_sidecar(db, key, sidecar)
            return {'status': 'ok', 'rev': new_rev, 'inserted_block_ids': inserted_block_ids}

        # --- Single op ---
        result = _apply_op(body, doc, block_ids)
        new_rev = _increment_rev(sidecar['rev'])
        sidecar['rev'] = new_rev
        sidecar['block_ids'] = block_ids
        db[key] = json.dumps(doc)
        _save_sidecar(db, key, sidecar)
        result['rev'] = new_rev
        return result


# ---------------------------------------------------------------------------
# REST app (FastAPI) — same interface as gdata_server.py
# ---------------------------------------------------------------------------

_SOURCE_MTIME = os.path.getmtime(__file__)


def make_rest_app(db_path: str, rest_port: int = 8020, store_name: str = "default") -> fastapi.FastAPI:
    app = fastapi.FastAPI(redirect_slashes=False)
    app.include_router(gdata_oauth.router)
    if _env_bool("NOTES_WEB", True):
        app.include_router(notes_web.make_router(f"http://127.0.0.1:{rest_port}"))

    @app.middleware("http")
    async def auto_reload(request: fastapi.Request, call_next):
        response = await call_next(request)
        response.headers["X-GData-Store"] = store_name
        if os.path.getmtime(__file__) != _SOURCE_MTIME:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        return response

    def _key(path: str) -> str:
        return urllib.parse.unquote(path.lstrip('/'))

    @app.get("/{path:path}")
    async def get_item(path: str):
        key = _key(path)
        raw, found = await db_get(db_path, key)
        if not found:
            raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
        return Response(content=raw, media_type="application/json")

    @app.put("/{path:path}")
    async def put_item(path: str, request: fastapi.Request):
        key = _key(path)
        body = (await request.body()).decode('utf-8')
        sidecar = await db_put(db_path, key, body)
        return {
            'status': 'ok',
            'rev': sidecar['rev'],
            'block_ids': sidecar['block_ids'],
        }

    @app.delete("/{path:path}")
    async def delete_item(path: str):
        key = _key(path)
        deleted = await db_delete(db_path, key)
        if not deleted:
            raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
        return {'status': 'deleted'}

    @app.head("/{path:path}")
    async def head_item(path: str):
        key = _key(path)
        _, found = await db_get(db_path, key)
        return Response(status_code=200 if found else 404)

    @app.post("/")
    async def post_op(body: dict):
        op = body.get('op')
        if op == 'keys':
            return {'keys': await db_keys(db_path)}
        if op == 'dump':
            return {'items': await db_dump(db_path)}
        if op == 'flush':
            await db_flush(db_path)
            return {'status': 'flushed'}
        if op == 'stop':
            import signal, threading
            def _shutdown():
                import time; time.sleep(0.1)
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=_shutdown, daemon=True).start()
            return {'status': 'stopping'}
        return {
            'error': f'unknown op: {op!r}',
            'supported_ops': ['keys', 'flush', 'stop'],
        }

    @app.post("/{path:path}")
    async def post_patch(path: str, body: dict):
        key = _key(path)
        return await db_patch(db_path, key, body)

    @app.patch("/{path:path}")
    async def patch_item(path: str, body: dict):
        key = _key(path)
        return await db_patch(db_path, key, body)

    return app


# ---------------------------------------------------------------------------
# MCP app (SSE and/or Streamable HTTP, controlled by env vars)
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, '').lower()
    if val in ('1', 'true', 'yes'):
        return True
    if val in ('0', 'false', 'no'):
        return False
    return default


def _mcp_error(detail: str, status_code: int | None = None) -> dict:
    """Standard error dict for MCP tool responses. Includes docs pointer."""
    r = {"error": detail, "docs": "README"}
    if status_code is not None:
        r["status_code"] = status_code
    return r


def _make_tool_server(db_path: str, store_name: str = "default") -> Server:
    """Create and return a Server instance with all gdata tools registered.

    Called once per transport so each transport has its own Server instance
    with independent session state — they must not be shared.
    Both instances operate on the same database via the shared db_* functions.
    """
    server = Server("gdata")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = [
            types.Tool(
                name="get",
                description=(
                    "Read a complete document. Call with just the key to get the full JSONHTL document. "
                    "Examples: get(key='README') to read the README, get(key='notes/example') to read a note. "
                    "Optional: set include_block_ids=true if you need revision metadata and stable block IDs for batch editing — "
                    "this returns {document, rev, block_ids} instead of just the document."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "The key/path of the document to read"},
                        "include_block_ids": {
                            "type": "boolean",
                            "description": "Optional. If true, returns {document, rev, block_ids} for batch editing. Default false returns the document only.",
                        },
                    },
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="put",
                description=(
                    "Store a full document under a key. Regenerates all block IDs and returns the new revision and block_ids. "
                    "Use this before patch() or batch() operations — no need for a follow-up get(include_block_ids=true). "
                    "Use patch() or batch() for targeted edits to individual blocks without full document replacement."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"description": "Any JSON-serialisable value"},
                        "if_rev": {
                            "type": "string",
                            "description": "Optimistic concurrency token. Returns 412 if current rev differs. Optional.",
                        },
                    },
                    "required": ["key", "value"],
                },
            ),
            types.Tool(
                name="outline",
                description=(
                    "Return a compact block outline for a JSONHTL note: {rev, blocks:[{id,index,type,preview}, ...]}. "
                    "Use this to choose a block_id without reading or aligning the full document sidecar. "
                    "The preview is plain text extracted from the block and truncated to preview_chars characters."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "The key/path of the document to outline"},
                        "preview_chars": {
                            "type": "integer",
                            "description": "Maximum preview length per block. Default 80.",
                        },
                    },
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="delete",
                description="Delete a key.",
                inputSchema={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="keys",
                description="List all keys in the database.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="patch",
                description=(
                    "Apply a single block-level patch to a JSONHTL note. "
                    "For multi-step edits, use get(include_block_ids=True) then batch() instead — "
                    "index-based ops drift after each mutation. "
                    "ID-based ops (supply block_id) are preferred: insert_before, insert_after, "
                    "replace_block+block_id, delete_block+block_id. "
                    "Index-based ops are fine for simple one-shot changes."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Note key"},
                        "op": {
                            "type": "string",
                            "enum": [
                                "append_block", "insert_block", "replace_block",
                                "delete_block", "delete_blocks", "patch_meta",
                                "insert_before", "insert_after",
                            ],
                            "description": "Operation. ID-based: replace_block+block_id, delete_block+block_id, insert_before, insert_after. Index-based: append_block, insert_block, replace_block+index, delete_block+index, delete_blocks.",
                        },
                        "block_id": {
                            "type": "string",
                            "description": "Stable server-assigned block ID (from get(include_block_ids=True) or batch response). Required for insert_before/insert_after; preferred over index for replace_block/delete_block.",
                        },
                        "block": {
                            "description": "JSONHTL block — required for append_block, insert_block, replace_block, insert_before, insert_after (may be a JSON-encoded string; handler unwraps automatically)",
                        },
                        "index": {
                            "type": "integer",
                            "description": "0-based block index — for insert_block, replace_block, delete_block when not using block_id. Avoid for multi-step edits: drifts after each mutation.",
                        },
                        "indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "List of 0-based block indices — required for delete_blocks.",
                        },
                        "fields": {
                            "description": "Metadata fields to update — required for patch_meta (title, version, updated, tags; not content). May be a JSON-encoded string; handler unwraps automatically.",
                        },
                        "if_rev": {
                            "type": "string",
                            "description": "Optimistic concurrency token from get(include_block_ids=True). Returns 409 if current rev differs. Optional but recommended for coordinated edits.",
                        },
                    },
                    "required": ["key", "op"],
                },
            ),
            types.Tool(
                name="batch",
                description=(
                    "Apply multiple patch operations atomically — all succeed or none are applied. "
                    "Preferred for multi-step edits. "
                    "Workflow: (1) get(key, include_block_ids=True) to obtain block IDs and rev; "
                    "(2) batch(key, ops=[...], if_rev=rev) using block_id in each op to avoid index drift. "
                    "Returns 409 if if_rev doesn't match (concurrent modification) — re-read and retry."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Note key"},
                        "ops": {
                            "description": (
                                "List of patch ops to apply sequentially. "
                                "Each op is a dict with 'op' and relevant params (block_id, block, fields, etc.). "
                                "May be a JSON-encoded string; handler unwraps automatically."
                            ),
                        },
                        "if_rev": {
                            "type": "string",
                            "description": "Optimistic concurrency token from get(include_block_ids=True). Strongly recommended. Returns 409 on mismatch.",
                        },
                    },
                    "required": ["key", "ops"],
                },
            ),
            types.Tool(
                name="reorder",
                description=(
                    "Reorder blocks in a document by providing the complete list of block IDs in the desired sequence. "
                    "Workflow: get(key, include_block_ids=True) → permute the block_ids array → reorder(key, order=permuted_ids, if_rev=rev). "
                    "Validation: order must list every current block ID exactly once — missing, unknown, or duplicate IDs are rejected with details. "
                    "A stale if_rev causes a 409 (another writer may have inserted/deleted blocks since your get). "
                    "Only block sequence changes; no content or metadata is modified."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Note key"},
                        "order": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Complete list of all current block IDs in the desired order. Must include every block exactly once.",
                        },
                        "if_rev": {
                            "type": "string",
                            "description": "Optimistic concurrency token from get(include_block_ids=True). Strongly recommended — a concurrent insert makes the order list stale and fails with 409.",
                        },
                    },
                    "required": ["key", "order"],
                },
            ),
            types.Tool(
                name="table_op",
                description=(
                    "Apply a table editing operation to a table block within a JSONHTL note. "
                    "Block is addressed by block_id string or integer index ('block' param). "
                    "Column by name (string) or integer index ('column' param). "
                    "Row by integer index ('row' param). "
                    "Composable inside batch(). "
                    "ops: rename_column, insert_column, delete_column, move_column, reorder_columns, fill_column, set_columns, "
                    "insert_row, append_row, delete_row, move_row, sort, fill_row, deduplicate, "
                    "set_cell, set_caption, transpose, set_index, clear_index, replace."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Note key"},
                        "op": {"type": "string", "description": "Operation name, e.g. 'table.rename_column'"},
                        "block": {"description": "Block ID (string) or integer index of the table block"},
                        "column": {"description": "Column name (string) or 0-based integer index"},
                        "row": {"type": "integer", "description": "0-based data row index"},
                        "new_name": {"type": "string", "description": "New column name (rename_column)"},
                        "name": {"type": "string", "description": "Column name for insert_column"},
                        "after": {"description": "Column name/index to insert after (insert_column, move_column)"},
                        "position": {"type": "integer", "description": "Absolute insert position, 0=before first (insert_column, insert_row)"},
                        "to": {"type": "integer", "description": "Target position for move_column or move_row"},
                        "order": {"description": "Column names in desired order (reorder_columns). May be JSON-encoded string."},
                        "columns": {"description": "New column headers list (set_columns). May be JSON-encoded string."},
                        "values": {"description": "Positional array or {col:val} dict for insert_row/append_row/insert_column. May be JSON-encoded string."},
                        "value": {"description": "Cell value (JSON primitive) for set_cell, fill_column, fill_row"},
                        "default": {"description": "Default value when values list is shorter than needed"},
                        "by": {"description": "Column name or list of names to sort by (sort). May be JSON-encoded string."},
                        "ascending": {"description": "Sort direction: true/false or list of bool (sort, default true)"},
                        "caption": {"description": "Table caption string or null to clear (set_caption)"},
                        "old_value": {"description": "Value to find (replace)"},
                        "new_value": {"description": "Replacement value (replace)"},
                        "if_rev": {"type": "string", "description": "Optimistic concurrency token. Returns 409 on mismatch."},
                    },
                    "required": ["key", "op", "block"],
                },
            ),
        ]
        _prefix = f"[{store_name} store] "
        for _t in tools:
            _t.description = _prefix + _t.description
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
        try:
            if name == "get":
                key = arguments["key"]
                include_block_ids = bool(arguments.get("include_block_ids", False))
                if include_block_ids:
                    try:
                        result = await db_patch(db_path, key, {"op": "get_with_block_ids"})
                    except fastapi.HTTPException as e:
                        result = _mcp_error(e.detail, e.status_code)
                else:
                    raw, found = await db_get(db_path, key)
                    if not found:
                        result = _mcp_error(f"key not found: {key}")
                    else:
                        try:
                            result = json.loads(raw)
                        except json.JSONDecodeError:
                            result = raw
            elif name == "put":
                key = arguments["key"]
                value = arguments["value"]
                if isinstance(value, str):
                    try:
                        value = _parse_json_robust(value)
                    except json.JSONDecodeError:
                        pass
                if_rev = arguments.get("if_rev") or None
                try:
                    sidecar = await db_put(db_path, key, json.dumps(value), if_rev=if_rev)
                    result = {
                        "status": "ok",
                        "rev": sidecar["rev"],
                        "block_ids": sidecar["block_ids"],
                    }
                except fastapi.HTTPException as e:
                    result = _mcp_error(e.detail, e.status_code)
            elif name == "delete":
                key = arguments["key"]
                deleted = await db_delete(db_path, key)
                result = {"status": "deleted"} if deleted else _mcp_error(f"key not found: {key}")
            elif name == "keys":
                result = {"keys": await db_keys(db_path)}
            elif name == "outline":
                key = arguments["key"]
                body = {"op": "outline"}
                if "preview_chars" in arguments:
                    body["preview_chars"] = arguments["preview_chars"]
                try:
                    result = await db_patch(db_path, key, body)
                except fastapi.HTTPException as e:
                    result = _mcp_error(e.detail, e.status_code)
            elif name == "patch":
                key = arguments["key"]
                body = {k: arguments[k] for k in
                        ("op", "block_id", "block", "index", "indices", "fields", "if_rev", "preview_chars")
                        if k in arguments}
                for field in ("block", "fields"):
                    if isinstance(body.get(field), str):
                        try:
                            body[field] = _parse_json_robust(body[field])
                        except json.JSONDecodeError:
                            pass
                try:
                    result = await db_patch(db_path, key, body)
                except fastapi.HTTPException as e:
                    result = _mcp_error(e.detail, e.status_code)
            elif name == "batch":
                key = arguments["key"]
                ops = arguments.get("ops", [])
                if isinstance(ops, str):
                    try:
                        ops = _parse_json_robust(ops)
                    except json.JSONDecodeError as e:
                        return [types.TextContent(type="text", text=json.dumps(
                            _mcp_error(f"'ops' could not be parsed as JSON: {e}")))]
                if_rev = arguments.get("if_rev") or None
                body = {"op": "batch", "ops": ops}
                if if_rev:
                    body["if_rev"] = if_rev
                try:
                    result = await db_patch(db_path, key, body)
                except fastapi.HTTPException as e:
                    result = _mcp_error(e.detail, e.status_code)
            elif name == "reorder":
                key = arguments["key"]
                order = arguments["order"]
                if isinstance(order, str):
                    try:
                        order = _parse_json_robust(order)
                    except json.JSONDecodeError as e:
                        return [types.TextContent(type="text", text=json.dumps(
                            _mcp_error(f"'order' could not be parsed as JSON: {e}")))]
                if_rev = arguments.get("if_rev") or None
                body = {"op": "reorder", "order": order}
                if if_rev:
                    body["if_rev"] = if_rev
                try:
                    result = await db_patch(db_path, key, body)
                except fastapi.HTTPException as e:
                    result = _mcp_error(e.detail, e.status_code)
            elif name == "table_op":
                key = arguments["key"]
                body = {k: v for k, v in arguments.items() if k != "key"}
                for field in ("values", "order", "columns", "by"):
                    if isinstance(body.get(field), str):
                        try:
                            body[field] = _parse_json_robust(body[field])
                        except json.JSONDecodeError:
                            pass
                try:
                    result = await db_patch(db_path, key, body)
                except fastapi.HTTPException as e:
                    result = _mcp_error(e.detail, e.status_code)
            else:
                result = _mcp_error(f"unknown tool: {name}")
        except Exception as e:
            result = _mcp_error(str(e))

        return [types.TextContent(type="text", text=json.dumps(result))]

    return server


def make_mcp_app(db_path: str, store_name: str = "default") -> Starlette:
    enable_sse        = _env_bool('MCP_SSE',        True)
    enable_streamable = _env_bool('MCP_STREAMABLE', True)

    if not enable_sse and not enable_streamable:
        raise RuntimeError("At least one of MCP_SSE or MCP_STREAMABLE must be enabled")

    routes = []

    if enable_sse:
        sse_server = _make_tool_server(db_path, store_name)
        sse = SseServerTransport("/mcp/messages/")

        async def handle_sse(request: Request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await sse_server.run(
                    streams[0], streams[1],
                    sse_server.create_initialization_options()
                )
            from starlette.responses import Response as StarletteResponse
            return StarletteResponse()

        routes += [
            Route("/mcp/", endpoint=handle_sse, methods=["GET"]),
            Mount("/mcp/messages", app=sse.handle_post_message),
        ]

    if enable_streamable:
        streamable_server = _make_tool_server(db_path, store_name)
        session_manager = StreamableHTTPSessionManager(streamable_server, stateless=True)

        @contextlib.asynccontextmanager
        async def streamable_lifespan(app):
            async with session_manager.run():
                yield

        routes.append(Mount("/mcp", app=session_manager.handle_request))

    transports = []
    if enable_sse:        transports.append("SSE")
    if enable_streamable: transports.append("Streamable HTTP")
    logging.info(f"MCP transports enabled: {', '.join(transports)}")

    if enable_streamable:
        app = Starlette(routes=routes, lifespan=streamable_lifespan)
    else:
        app = Starlette(routes=routes)

    return gdata_oauth.BearerMiddleware(app)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    gdata_oauth.startup_check()
    parser = argparse.ArgumentParser(description="gdata REST + MCP server")
    parser.add_argument("--rest-port", type=int, default=int(os.getenv("GDATA_SERVER_PORT", 8020)))
    parser.add_argument("--mcp-port",  type=int, default=int(os.getenv("GDATA_MCP_PORT",    8022)))
    parser.add_argument("--host",      default=os.getenv("GDATA_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--db",        default=os.getenv("GDBM_PATH", ".gdbm"))
    parser.add_argument("--name",      default=os.getenv("GDATA_STORE_NAME", "default"))
    args = parser.parse_args()

    log_level = os.getenv('LOG_LEVEL', 'info').lower()

    rest_app = make_rest_app(args.db, args.rest_port, args.name)
    mcp_app  = make_mcp_app(args.db, args.name)

    rest_cfg = uvicorn.Config(rest_app, host=args.host, port=args.rest_port, log_level=log_level)
    mcp_cfg  = uvicorn.Config(mcp_app,  host=args.host, port=args.mcp_port,  log_level=log_level)

    logging.info(f"REST on {args.host}:{args.rest_port}  |  MCP on {args.host}:{args.mcp_port}  |  db={args.db}  |  name={args.name}")

    await asyncio.gather(
        uvicorn.Server(rest_cfg).serve(),
        uvicorn.Server(mcp_cfg).serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
