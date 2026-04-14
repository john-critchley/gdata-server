#!/usr/bin/python
import copy
import os
import random
import string
import sys
import threading
import yaml
import logging
import json
import fastapi
import urllib.parse
import uvicorn
from fastapi.responses import Response

_patch_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Sidecar storage — block IDs and internal revision token
#
# For each document key K, the sidecar is stored at "\x00" + K in the same
# GDBM database.  Null-byte prefix cannot appear in URL-decoded path keys so
# there is no collision risk.  Sidecar format:
#   {"rev": "r5", "block_ids": ["a1b2c3", "d4e5f6", ...]}
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
    """Return existing sidecar or create one.  Regenerates block_ids if count mismatches."""
    sidecar = _get_sidecar(db, key)
    if sidecar is None:
        sidecar = {'rev': 'r1', 'block_ids': [_new_block_id() for _ in content]}
        _save_sidecar(db, key, sidecar)
    elif len(sidecar.get('block_ids', [])) != len(content):
        sidecar['block_ids'] = [_new_block_id() for _ in content]
        _save_sidecar(db, key, sidecar)
    return sidecar


# ---------------------------------------------------------------------------
# Pure-logic patch helper — no DB access
# ---------------------------------------------------------------------------

def _apply_op(op_body: dict, doc: dict, block_ids: list) -> dict:
    """Apply one patch op to doc and block_ids in-place.

    Returns {'status': 'ok', ...}.  May include 'inserted_block_id'.
    Raises fastapi.HTTPException on any validation error.
    Does NOT write to the database.
    """
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

    # --- ID-based insert ops (always require block_id) ---
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

    # --- ID-based variants of replace/delete (block_id takes precedence over index) ---
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

    # --- Index-based ops ---
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
        else:  # replace_block by index
            content[index] = block
            return {'status': 'ok'}

    raise fastapi.HTTPException(
        status_code=400,
        detail=(
            f"unknown op: {op!r}. Supported: append_block, insert_block, replace_block, "
            "delete_block, delete_blocks, patch_meta, insert_before, insert_after, "
            "batch, get_with_block_ids"
        )
    )

# Force import of gdata from current directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gdata

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def setup() -> dict:
    """
    Load configuration from environment and/or config file.
    
    Precedence:
    1. GDATA_SERVER_CONFIG env var (path to YAML config file)
    2. .gdata_server.yaml in current directory (if exists)
    3. GDBM_PATH env var
    4. Default: .gdbm in current directory
    
    Returns dict with at least {"gdbm_file": "..."}
    No config file is required—all env vars is fine.
    """
    GDATA_SERVER_CONFIG_default='.gdata_server.yaml'
    config_file = os.environ.get('GDATA_SERVER_CONFIG', GDATA_SERVER_CONFIG_default)
    try:
        with open(config_file) as f:
            config = yaml.load(f, Loader=yaml.SafeLoader)
    except FileNotFoundError:
        config = {}
    CONFIG_defaults={
            'gdbm_file': '.gdbm',
            'gdata_server_port' : 8020,
            'gdata_server_host': '127.0.0.1'
    }
    for vr in CONFIG_defaults:
        env_var_name=vr.upper()
        if env_var_name in os.environ:
            config[vr]=os.environ[env_var_name]
    for vr,vval in CONFIG_defaults.items():
        if vr not in config:
            config[vr]=vval
    if not isinstance(config['gdata_server_port'], int):
        config['gdata_server_port']=int(config['gdata_server_port'])
    
    assert isinstance(config, dict)
    return config

config = setup()
# Use gdata_local_simple - stores/retrieves JSON strings directly
# Delay database opening until server starts to avoid import-time locking
db = None

def get_db():
    global db
    if db is None:
        db = gdata.gdata_local_simple(gdbm_file=config['gdbm_file'])
    return db


def _decode_key(path: str) -> str:
    return urllib.parse.unquote(path.strip('/'))


def _list_keys() -> list[str]:
    return sorted(k for k in get_db().keys() if not k.startswith(_SIDECAR_PREFIX))


def handle_GET_request(path: str, body: dict) -> Response:
    """Return raw JSON string from database, with ETag header if sidecar exists."""
    key = _decode_key(path)
    db = get_db()
    if key not in db:
        raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
    json_string = db[key]
    headers = {}
    sidecar = _get_sidecar(db, key)
    if sidecar:
        headers['ETag'] = f'"{sidecar["rev"]}"'
    return Response(content=json_string, media_type="application/json", headers=headers)


def handle_PUT_request(path: str, body_text: str, if_match: str | None = None) -> dict:
    """Store raw JSON string in database and regenerate sidecar."""
    key = _decode_key(path)
    with _patch_lock:
        db = get_db()
        old_sidecar = _get_sidecar(db, key)

        if if_match is not None:
            current_rev = old_sidecar['rev'] if old_sidecar else None
            if current_rev != if_match:
                raise fastapi.HTTPException(
                    status_code=412,
                    detail=f"revision mismatch: expected {if_match!r}, current rev is {current_rev!r}"
                )

        db[key] = body_text

        try:
            doc = json.loads(body_text)
            content = doc.get('content', []) if isinstance(doc, dict) else []
            if not isinstance(content, list):
                content = []
        except json.JSONDecodeError:
            content = []

        new_sidecar = {
            'rev': _increment_rev(old_sidecar['rev'] if old_sidecar else 'r0'),
            'block_ids': [_new_block_id() for _ in content],
        }
        _save_sidecar(db, key, new_sidecar)

    return {'status': 'ok'}


def handle_HEAD_request(path: str, body: dict) -> Response:
    """Check if key exists, return 200 or 404."""
    key = _decode_key(path)
    status_code = 200 if key in get_db() else 404
    return Response(status_code=status_code)


def handle_DELETE_request(path: str, body: dict) -> dict:
    """Delete a key and its sidecar from database."""
    key = _decode_key(path)
    db = get_db()
    if key not in db:
        raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
    del db[key]
    sk = _sidecar_key(key)
    if sk in db:
        del db[sk]
    return {'status': 'deleted'}


def handle_PATCH_DOC_request(path: str, body: dict, if_match: str | None = None) -> dict:
    """
    POST /{key} — block-level patch operations on a JSONHTL document.

    Supported ops: append_block, insert_block, replace_block, delete_block, delete_blocks,
    patch_meta, insert_before, insert_after, batch, get_with_block_ids.

    if_match: value of If-Match header (rev token, e.g. "r5"), stripped of quotes.
    body.get('if_rev') takes precedence over if_match.
    """
    key = _decode_key(path)
    db = get_db()

    with _patch_lock:
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
            return {
                'document': doc,
                'rev': sidecar['rev'],
                'block_ids': sidecar['block_ids'],
            }

        # --- Concurrency guard (if_rev in body takes precedence over If-Match header) ---
        if_rev = body.get('if_rev') or if_match
        if if_rev is not None:
            sidecar = _get_sidecar(db, key)
            current_rev = sidecar['rev'] if sidecar else None
            if current_rev != if_rev:
                raise fastapi.HTTPException(
                    status_code=409,
                    detail=f"revision mismatch: expected {if_rev!r}, current rev is {current_rev!r}"
                )

        # --- Load/create sidecar for all mutating ops ---
        content = doc.get('content')
        blocks = content if isinstance(content, list) else []
        sidecar = _get_or_create_sidecar(db, key, blocks)
        block_ids = sidecar['block_ids']

        # --- Batch op: atomic all-or-nothing ---
        if op == 'batch':
            ops = body.get('ops')
            if isinstance(ops, str):
                try:
                    ops = json.loads(ops)
                except json.JSONDecodeError:
                    raise fastapi.HTTPException(status_code=400, detail="'ops' could not be parsed as JSON")
            if not isinstance(ops, list) or not ops:
                raise fastapi.HTTPException(status_code=400, detail="'ops' must be a non-empty list")

            # Work on deep copies so any failure leaves the DB unchanged
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


def handle_POST_request(path: str, body: dict) -> dict:
    """
    POST accepts a JSON document describing the request.

    Supported operations (via `op`):
    - `keys`: list all keys
    - `dump`: return all key/value pairs as one document
    - `flush`: sync database to disk
    - `stop`: shutdown the server
    """
    if not isinstance(body, dict):
        return {'error': 'body must be a JSON object'}
    op = body.get('op')
    if op == 'keys':
        return {'keys': _list_keys()}
    if op == 'dump':
        # Build items dict with parsed JSON values
        items = {}
        db = get_db()
        for k in _list_keys():
            json_string = db[k]
            try:
                items[k] = json.loads(json_string)
            except json.JSONDecodeError:
                items[k] = json_string
        return {'items': items}
    if op == 'flush':
        # Sync database to disk
        get_db().db.sync()
        return {'status': 'flushed'}
    if op == 'stop':
        # Shutdown server
        import signal
        import threading
        def shutdown():
            import time
            time.sleep(0.1)  # Brief delay to allow response to be sent
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=shutdown, daemon=True).start()
        return {'status': 'stopping'}
    return {
        'error': f'unknown op: {op!r}',
        'supported_ops': ['keys', 'dump', 'flush', 'stop'],
    }


def handle_request(method: str, path: str, body) -> dict:
    """
    Handle a gdata request for stdin/stdout mode.
    
    Args:
        method: HTTP verb (GET, POST, DELETE, HEAD)
        path: URL path (e.g., "/get/mykey", "/keys", etc.)
        body: Request body as dict (for POST)
    
    Returns:
        dict with response data or error
    """
    assert isinstance(method, str) and isinstance(path, str)
    
    logging.log(logging.INFO, f'Got {method} request')
    
    try:
        if method == 'GET':
            key = _decode_key(path)
            db = get_db()
            if key not in db:
                return {'error': f'key not found: {key}'}
            json_string = db[key]
            try:
                value = json.loads(json_string)
            except json.JSONDecodeError:
                value = json_string
            return {'value': value}
        
        elif method == 'PUT':
            key = _decode_key(path)
            value = body.get('value')
            # Convert value to JSON string and store
            get_db()[key] = json.dumps(value)
            return {'status': 'ok'}
        
        elif method == 'DELETE':
            key = _decode_key(path)
            db = get_db()
            if key not in db:
                return {'error': f'key not found: {key}'}
            del db[key]
            return {'status': 'deleted'}
        
        elif method == 'HEAD':
            key = _decode_key(path)
            return {'exists': key in get_db()}
        
        elif method == 'POST':
            return handle_POST_request(path, body)
        
        else:
            return {'error': f'unknown method: {method}'}
    
    except fastapi.HTTPException as e:
        return {'error': str(getattr(e, 'detail', e))}
    except Exception as e:
        return {'error': str(e)}


def read_request():
    """Read HTTP-like request from stdin until '.' is entered"""
    while True:
        line = input().rstrip()
        if line == '.':
            break
        yield line


if __name__=="__main__":
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO'),
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Check if running in stdin mode (for pipe/redirect) or HTTP server mode
    if os.getenv('GDATA_SERVER_STDIN') or (not sys.stdin.isatty() and not os.getenv('GDATA_SERVER_HTTP')):
        # Stdin/stdout mode for piped requests
        hdrs=[]
        for l in read_request():
            if l=="":
                break
            hdrs.append(l)
        # Parse request line
        method, path, *vers = hdrs.pop(0).split()
        
        # Parse body as JSON if present
        body_text='\n'.join(read_request())
        if len(body_text):
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse body as JSON:\n{body_text}")
                body = {}
        else:
            body={}
        
        logging.info(f"Request: {method} {path}")
        result = handle_request(method, path, body)
        print(result)
    else:
        # HTTP server mode using uvicorn
        uvicorn.run(
            "gdata_server:app",
            host=config['gdata_server_host'],
            port=config['gdata_server_port'],
            log_level=os.getenv('LOG_LEVEL', 'info').lower()
        )


# FastAPI application
app = fastapi.FastAPI()

_SOURCE_MTIME = os.path.getmtime(__file__)

@app.middleware("http")
async def auto_reload(request: fastapi.Request, call_next):
    response = await call_next(request)
    if os.path.getmtime(__file__) != _SOURCE_MTIME:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return response


@app.get("/{path:path}")
def get_item(path: str):
    return handle_GET_request(f"/{path}", {})


@app.put("/{path:path}")
async def put_item(path: str, request: fastapi.Request):
    body_text = (await request.body()).decode('utf-8')
    if_match = request.headers.get('If-Match', '').strip('"') or None
    return handle_PUT_request(f"/{path}", body_text, if_match=if_match)


@app.delete("/{path:path}")
def delete_item(path: str):
    return handle_DELETE_request(f"/{path}", {})


@app.head("/{path:path}")
def head_item(path: str):
    return handle_HEAD_request(f"/{path}", {})


@app.post("/")
def post_op(body: dict):
    return handle_POST_request("/", body)


@app.post("/{path:path}")
def post_patch(path: str, body: dict, request: fastapi.Request):
    if_match = request.headers.get('If-Match', '').strip('"') or None
    return handle_PATCH_DOC_request(f"/{path}", body, if_match=if_match)
