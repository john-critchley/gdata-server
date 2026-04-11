#!/usr/bin/python
import os
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
    return sorted(list(get_db().keys()))


def handle_GET_request(path: str, body: dict) -> Response:
    """Return raw JSON string from database."""
    key = _decode_key(path)
    db = get_db()
    if key not in db:
        raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
    # db[key] returns a JSON string - return it directly
    json_string = db[key]
    return Response(content=json_string, media_type="application/json")


def handle_PUT_request(path: str, body_text: str) -> dict:
    """Store raw JSON string in database."""
    key = _decode_key(path)
    # Store the JSON string directly (no parsing needed)
    get_db()[key] = body_text
    return {'status': 'ok'}


def handle_HEAD_request(path: str, body: dict) -> Response:
    """Check if key exists, return 200 or 404."""
    key = _decode_key(path)
    status_code = 200 if key in get_db() else 404
    return Response(status_code=status_code)


def handle_DELETE_request(path: str, body: dict) -> dict:
    """Delete a key from database."""
    key = _decode_key(path)
    db = get_db()
    if key not in db:
        raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
    del db[key]
    return {'status': 'deleted'}


def handle_PATCH_DOC_request(path: str, body: dict) -> dict:
    """
    POST /{key} — block-level patch operations on a JSONHTL document.

    Supported ops: append_block, insert_block, replace_block, delete_block, patch_meta.
    Read-modify-write is atomic under _patch_lock.
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

        if op == 'patch_meta':
            fields = body.get('fields')
            if not isinstance(fields, dict):
                raise fastapi.HTTPException(status_code=400, detail="'fields' must be an object")
            if 'content' in fields:
                raise fastapi.HTTPException(status_code=400, detail="'content' cannot be updated via patch_meta")
            doc.update(fields)
            db[key] = json.dumps(doc)
            return {'status': 'ok'}

        content = doc.get('content')
        if content is None:
            raise fastapi.HTTPException(status_code=409, detail="document has no 'content' key")
        if not isinstance(content, list):
            raise fastapi.HTTPException(
                status_code=400,
                detail="'content' is not a list; cannot patch block-level (PUT the full document first)"
            )

        if op == 'append_block':
            block = body.get('block')
            if block is None:
                raise fastapi.HTTPException(status_code=400, detail="'block' is required for append_block")
            content.append(block)
            db[key] = json.dumps(doc)
            return {'status': 'ok'}

        if op == 'delete_blocks':
            indices = body.get('indices')
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
            db[key] = json.dumps(doc)
            return {'status': 'ok'}

        if op in ('insert_block', 'replace_block', 'delete_block'):
            index = body.get('index')
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
                db[key] = json.dumps(doc)
                return {'status': 'ok'}
            block = body.get('block')
            if block is None:
                raise fastapi.HTTPException(status_code=400, detail=f"'block' is required for {op}")
            if op == 'insert_block':
                content.insert(index, block)
            else:
                content[index] = block
            db[key] = json.dumps(doc)
            return {'status': 'ok'}

        raise fastapi.HTTPException(
            status_code=400,
            detail=f"unknown op: {op!r}. Supported: append_block, insert_block, replace_block, delete_block, delete_blocks, patch_meta"
        )


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


@app.get("/{path:path}")
def get_item(path: str):
    return handle_GET_request(f"/{path}", {})


@app.put("/{path:path}")
async def put_item(path: str, request: fastapi.Request):
    # Get raw body as text (JSON string)
    body_text = await request.body()
    body_text = body_text.decode('utf-8')
    return handle_PUT_request(f"/{path}", body_text)


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
def post_patch(path: str, body: dict):
    return handle_PATCH_DOC_request(f"/{path}", body)
