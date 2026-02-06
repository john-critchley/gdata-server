#!/usr/bin/python
import os
import yaml
import gdata
import logging    
import json
import fastapi
import urllib.parse
from fastapi.responses import Response

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
            'gdata_server_port' : 8020
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
db = gdata.gdata_local_simple(gdbm_file=config['gdbm_file'])


def _decode_key(path: str) -> str:
    return urllib.parse.unquote(path.strip('/'))


def _list_keys() -> list[str]:
    return sorted(list(db.keys()))


def handle_GET_request(path: str, body: dict) -> Response:
    """Return raw JSON string from database."""
    key = _decode_key(path)
    if key not in db:
        raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
    # db[key] returns a JSON string - return it directly
    json_string = db[key]
    return Response(content=json_string, media_type="application/json")


def handle_PUT_request(path: str, body_text: str) -> dict:
    """Store raw JSON string in database."""
    key = _decode_key(path)
    # Store the JSON string directly (no parsing needed)
    db[key] = body_text
    return {'status': 'ok'}


def handle_HEAD_request(path: str, body: dict) -> Response:
    """Check if key exists, return 200 or 404."""
    key = _decode_key(path)
    status_code = 200 if key in db else 404
    return Response(status_code=status_code)


def handle_DELETE_request(path: str, body: dict) -> dict:
    """Delete a key from database."""
    key = _decode_key(path)
    if key not in db:
        raise fastapi.HTTPException(status_code=404, detail=f"key not found: {key}")
    del db[key]
    return {'status': 'deleted'}


def handle_POST_request(path: str, body: dict) -> dict:
    """
    POST accepts a JSON document describing the request.

    Supported operations (via `op`):
    - `keys`: list all keys
    - `dump`: return all key/value pairs as one document
    """
    if not isinstance(body, dict):
        return {'error': 'body must be a JSON object'}
    op = body.get('op')
    if op == 'keys':
        return {'keys': _list_keys()}
    if op == 'dump':
        # Build items dict with parsed JSON values
        items = {}
        for k in _list_keys():
            json_string = db[k]
            try:
                items[k] = json.loads(json_string)
            except json.JSONDecodeError:
                items[k] = json_string
        return {'items': items}
    return {
        'error': f'unknown op: {op!r}',
        'supported_ops': ['keys', 'dump'],
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
            db[key] = json.dumps(value)
            return {'status': 'ok'}
        
        elif method == 'DELETE':
            key = _decode_key(path)
            if key not in db:
                return {'error': f'key not found: {key}'}
            del db[key]
            return {'status': 'deleted'}
        
        elif method == 'HEAD':
            key = _decode_key(path)
            return {'exists': key in db}
        
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
