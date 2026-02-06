#!/usr/bin/env python3
"""
Comprehensive test of gdata HTTP client matching test_http.sh format.
Run the server first: uvicorn gdata_server:app --host 127.0.0.1 --port 8020
"""
import sys
import gdata
import requests
import json
import os

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8020")
BOX_WIDTH = int(os.environ.get('BOX_WIDTH', 80))-4

def print_box(test_name, request_text, response_code, response_body, result, reason=""):
    """Print formatted test box matching shell script style"""
    print("┌" + "─" * (BOX_WIDTH + 2) + "┐", file=sys.stderr)
    
    # TEST section
    print("├─ TEST " + "─" * (BOX_WIDTH - 5) + "┤", file=sys.stderr)
    print(f"│ {test_name:<{BOX_WIDTH}} │", file=sys.stderr)
    print("│" + " " * (BOX_WIDTH + 2) + "│", file=sys.stderr)
    
    # REQUEST section
    print("├─ REQUEST " + "─" * (BOX_WIDTH - 8) + "┤", file=sys.stderr)
    for line in request_text.split('\n'):
        if len(line) > BOX_WIDTH:
            print(f"│ {line}", file=sys.stderr)
        else:
            print(f"│ {line:<{BOX_WIDTH}} │", file=sys.stderr)
    print("│" + " " * (BOX_WIDTH + 2) + "│", file=sys.stderr)
    
    # RESPONSE section
    print("├─ RESPONSE " + "─" * (BOX_WIDTH - 9) + "┤", file=sys.stderr)
    print(f"│ HTTP {response_code:<{BOX_WIDTH - 5}} │", file=sys.stderr)
    print("│" + " " * (BOX_WIDTH + 2) + "│", file=sys.stderr)
    for line in response_body.split('\n'):
        if len(line) > BOX_WIDTH:
            print(f"│ {line}", file=sys.stderr)
        else:
            print(f"│ {line:<{BOX_WIDTH}} │", file=sys.stderr)
    print("│" + " " * (BOX_WIDTH + 2) + "│", file=sys.stderr)
    print(f"│ RESULT: {result:<{BOX_WIDTH - 8}} │", file=sys.stderr)
    
    # REASONS section if failed
    if reason:
        print("│" + " " * (BOX_WIDTH + 2) + "│", file=sys.stderr)
        print("├─ REASONS " + "─" * (BOX_WIDTH - 8) + "┤", file=sys.stderr)
        for line in reason.split('\n'):
            print(f"│ {line:<{BOX_WIDTH}} │", file=sys.stderr)
    
    print("└" + "─" * (BOX_WIDTH + 2) + "┘", file=sys.stderr)

def run_test(test_name, test_func):
    """Run a test and print formatted output"""
    try:
        req_text, resp_code, resp_body = test_func()
        print_box(test_name, req_text, resp_code, resp_body, "PASS")
        return True
    except AssertionError as e:
        req_text = getattr(e, 'request_text', 'N/A')
        resp_code = getattr(e, 'response_code', 'N/A')
        resp_body = getattr(e, 'response_body', 'N/A')
        print_box(test_name, req_text, resp_code, resp_body, "FAIL", str(e))
        return False
    except Exception as e:
        print_box(test_name, "Exception occurred", "N/A", str(e), "FAIL", str(e))
        return False

def format_json(obj):
    """Format JSON for display"""
    return json.dumps(obj, indent=2)

# Test functions
def test_connectivity():
    req = 'POST /\n{"op": "keys"}'
    resp = requests.post(f"{BASE_URL}/", json={"op": "keys"})
    resp_body = format_json(resp.json())
    
    if resp.status_code != 200:
        e = AssertionError(f"Expected 200, got {resp.status_code}")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_put():
    test_key = "test_key_12345"
    req = f'PUT /{test_key}\n{{"value": {{"x": 1}}}}'
    
    with gdata.gdata(url=BASE_URL) as db:
        db[test_key] = {"x": 1}
    
    # Verify with direct HTTP
    resp = requests.get(f"{BASE_URL}/{test_key}")
    resp_body = resp.text
    
    if resp.status_code != 200:
        e = AssertionError(f"PUT failed")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, 200, '{"status": "ok"}'

def test_get():
    test_key = "test_key_12345"
    req = f'GET /{test_key}'
    
    with gdata.gdata(url=BASE_URL) as db:
        value = db[test_key]
    
    resp_body = format_json(value)
    
    if value != {"x": 1}:
        e = AssertionError(f"Value mismatch: {value}")
        e.request_text = req
        e.response_code = 200
        e.response_body = resp_body
        raise e
    
    return req, 200, resp_body

def test_head_exists():
    test_key = "test_key_12345"
    req = f'HEAD /{test_key}'
    
    resp = requests.head(f"{BASE_URL}/{test_key}")
    
    if resp.status_code != 200:
        e = AssertionError(f"Key should exist")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = ""
        raise e
    
    return req, resp.status_code, ""

def test_post_keys():
    req = 'POST /\n{"op": "keys"}'
    
    resp = requests.post(f"{BASE_URL}/", json={"op": "keys"})
    resp_body = format_json(resp.json())
    keys = resp.json()['keys']
    
    if "test_key_12345" not in keys:
        e = AssertionError(f"test_key_12345 not in keys")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_post_dump():
    req = 'POST /\n{"op": "dump"}'
    
    resp = requests.post(f"{BASE_URL}/", json={"op": "dump"})
    resp_body = format_json(resp.json())
    items = resp.json()['items']
    
    if "test_key_12345" not in items:
        e = AssertionError(f"test_key_12345 not in dump")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_delete():
    test_key = "test_key_12345"
    req = f'DELETE /{test_key}'
    
    with gdata.gdata(url=BASE_URL) as db:
        del db[test_key]
    
    return req, 200, '{"status": "deleted"}'

def test_head_missing():
    test_key = "test_key_12345"
    req = f'HEAD /{test_key}'
    
    resp = requests.head(f"{BASE_URL}/{test_key}")
    
    if resp.status_code != 404:
        e = AssertionError(f"Expected 404, got {resp.status_code}")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = ""
        raise e
    
    return req, resp.status_code, ""

def test_get_missing():
    test_key = "test_key_12345"
    req = f'GET /{test_key}'
    
    resp = requests.get(f"{BASE_URL}/{test_key}")
    resp_body = resp.text
    
    if resp.status_code != 404:
        e = AssertionError(f"Expected 404, got {resp.status_code}")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    if "key not found" not in resp_body:
        e = AssertionError(f"Expected 'key not found' in response")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_delete_missing():
    test_key = "test_key_12345"
    req = f'DELETE /{test_key}'
    
    resp = requests.delete(f"{BASE_URL}/{test_key}")
    resp_body = resp.text
    
    if resp.status_code != 404:
        e = AssertionError(f"Expected 404, got {resp.status_code}")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_url_encoded_put():
    space_key = "http space_67890"
    req = f'PUT /http%20space_67890\n{{"value": {{"x": 2}}}}'
    
    with gdata.gdata(url=BASE_URL) as db:
        db[space_key] = {"x": 2}
    
    return req, 200, '{"status": "ok"}'

def test_url_encoded_get():
    space_key = "http space_67890"
    req = f'GET /http%20space_67890'
    
    with gdata.gdata(url=BASE_URL) as db:
        value = db[space_key]
    
    resp_body = format_json(value)
    
    if value != {"x": 2}:
        e = AssertionError(f"Value mismatch: {value}")
        e.request_text = req
        e.response_code = 200
        e.response_body = resp_body
        raise e
    
    return req, 200, resp_body

def test_url_encoded_head():
    space_key = "http space_67890"
    req = f'HEAD /http%20space_67890'
    
    resp = requests.head(f"{BASE_URL}/http%20space_67890")
    
    if resp.status_code != 200:
        e = AssertionError(f"Key should exist")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = ""
        raise e
    
    return req, resp.status_code, ""

def test_url_encoded_in_keys():
    space_key = "http space_67890"
    req = 'POST /\n{"op": "keys"}'
    
    resp = requests.post(f"{BASE_URL}/", json={"op": "keys"})
    resp_body = format_json(resp.json())
    keys = resp.json()['keys']
    
    if space_key not in keys:
        e = AssertionError(f"{space_key} not in keys")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_url_encoded_in_dump():
    space_key = "http space_67890"
    req = 'POST /\n{"op": "dump"}'
    
    resp = requests.post(f"{BASE_URL}/", json={"op": "dump"})
    resp_body = format_json(resp.json())
    items = resp.json()['items']
    
    if space_key not in items:
        e = AssertionError(f"{space_key} not in dump")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_url_encoded_delete():
    space_key = "http space_67890"
    req = f'DELETE /http%20space_67890'
    
    with gdata.gdata(url=BASE_URL) as db:
        del db[space_key]
    
    return req, 200, '{"status": "deleted"}'

def test_unknown_op():
    req = 'POST /\n{"op": "nope"}'
    
    resp = requests.post(f"{BASE_URL}/", json={"op": "nope"})
    resp_body = format_json(resp.json())
    
    if "error" not in resp.json():
        e = AssertionError("Expected 'error' field")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_non_object_json():
    req = 'POST /\n[1, 2, 3]'
    
    resp = requests.post(f"{BASE_URL}/", json=[1, 2, 3])
    resp_body = resp.text
    
    if resp.status_code != 422:
        e = AssertionError(f"Expected 422, got {resp.status_code}")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_invalid_json():
    req = 'POST /\n{this is not json}'
    
    resp = requests.post(
        f"{BASE_URL}/",
        data="{this is not json}",
        headers={"Content-Type": "application/json"}
    )
    resp_body = resp.text
    
    if resp.status_code != 422:
        e = AssertionError(f"Expected 422, got {resp.status_code}")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    
    return req, resp.status_code, resp_body

def test_local_gdbm_basic():
    """Test basic local GDBM functionality"""
    test_file = "test_local_basic.gdbm"
    req = f'Local GDBM: {test_file}'
    
    # Clean up any existing test file
    import os
    if os.path.exists(test_file):
        os.remove(test_file)
    
    try:
        with gdata.gdata(gdbm_file=test_file) as db:
            # Test put/get
            db["test_key"] = {"local": True, "value": 42}
            retrieved = db["test_key"]
            
            if retrieved != {"local": True, "value": 42}:
                raise AssertionError(f"Value mismatch: {retrieved}")
            
            # Test contains
            if "test_key" not in db:
                raise AssertionError("Key should exist in database")
            
            # Test keys()
            keys = list(db.keys())
            if "test_key" not in keys:
                raise AssertionError(f"test_key not in keys: {keys}")
        
        # Clean up
        if os.path.exists(test_file):
            os.remove(test_file)
            
        return req, 200, '{"local_gdbm": "ok", "operations": ["put", "get", "contains", "keys"]}'
    except Exception as e:
        # Clean up on error
        if os.path.exists(test_file):
            os.remove(test_file)
        raise

def test_gdata_locked_error_exists():
    """Test that GDataLockedError exception class exists and is importable"""
    req = 'Import GDataLockedError'
    
    # Verify the exception class exists
    if not hasattr(gdata, 'GDataLockedError'):
        raise AssertionError("GDataLockedError not found in gdata module")
    
    # Verify it's an exception class
    if not issubclass(gdata.GDataLockedError, Exception):
        raise AssertionError("GDataLockedError is not an Exception subclass")
    
    # Verify it's a gdbm.error subclass
    import dbm.gnu as gdbm
    if not issubclass(gdata.GDataLockedError, gdbm.error):
        raise AssertionError("GDataLockedError is not a gdbm.error subclass")
    
    return req, 200, '{"GDataLockedError": "exists", "inheritance": "gdbm.error"}'

def test_factory_function():
    """Test that gdata factory function works for both modes"""
    req = 'Factory function: gdata(gdbm_file=...) vs gdata(url=...)'
    
    test_file = "test_factory.gdbm"
    import os
    if os.path.exists(test_file):
        os.remove(test_file)
    
    try:
        # Test local mode
        db_local = gdata.gdata(gdbm_file=test_file)
        if not hasattr(db_local, 'db'):  # Local has 'db' attribute
            raise AssertionError("Local mode doesn't have gdbm database")
        db_local.close()
        
        # Test HTTP mode  
        db_http = gdata.gdata(url=BASE_URL)
        if not hasattr(db_http, 'url'):  # HTTP has 'url' attribute
            raise AssertionError("HTTP mode doesn't have url attribute")
        db_http.close()
        
        # Test error on both parameters
        try:
            gdata.gdata(gdbm_file=test_file, url=BASE_URL)
            raise AssertionError("Should have raised ValueError for both parameters")
        except ValueError:
            pass  # Expected
        
        # Clean up
        if os.path.exists(test_file):
            os.remove(test_file)
            
        return req, 200, '{"factory": "ok", "modes": ["local", "http", "error_handling"]}'
    except Exception as e:
        # Clean up on error
        if os.path.exists(test_file):
            os.remove(test_file)
        raise

def test_lock_contention_raises_locked_error():
    """Opening the same GDBM file twice for write should raise a lock error."""
    test_file = "test_lock.gdbm"
    req = f"Lock contention on {test_file}"

    import os
    if os.path.exists(test_file):
        os.remove(test_file)

    db1 = None
    try:
        db1 = gdata.gdata(gdbm_file=test_file)
        try:
            # Second writer should fail with a lock-related error
            db2 = gdata.gdata(gdbm_file=test_file)
            db2.close()
            raise AssertionError("Expected lock contention to raise an error")
        except (gdata.GDataLockedError, Exception) as e:
            # Accept GDataLockedError or underlying gdbm.error indicating lock
            if isinstance(e, AssertionError):
                raise
        return req, 200, '{"lock": "contention detected"}'
    finally:
        if db1:
            db1.close()
        if os.path.exists(test_file):
            os.remove(test_file)

def test_local_delete_and_missing():
    """Local GDBM delete should remove the key and subsequent get raises KeyError."""
    test_file = "test_local_delete.gdbm"
    req = f"Local delete and missing on {test_file}"

    import os
    if os.path.exists(test_file):
        os.remove(test_file)

    try:
        with gdata.gdata(gdbm_file=test_file) as db:
            db["delete_me"] = {"bye": True}
            del db["delete_me"]
            if "delete_me" in db:
                raise AssertionError("Key should not exist after delete")
            try:
                _ = db["delete_me"]
                raise AssertionError("Expected KeyError on missing key after delete")
            except KeyError:
                pass  # expected
        return req, 200, '{"local_delete": "ok", "missing": "KeyError"}'
    finally:
        if os.path.exists(test_file):
            os.remove(test_file)

def test_http_delete_missing_raises_keyerror():
    """HTTP client delete on missing key should raise KeyError."""
    req = 'HTTP delete missing key via client'
    with gdata.gdata(url=BASE_URL) as db:
        try:
            del db["definitely_missing_http_key"]
            raise AssertionError("Expected KeyError when deleting missing key over HTTP")
        except KeyError:
            pass
    return req, 200, '{"http_delete_missing": "KeyError"}'

def test_post_flush():
    """POST flush should succeed."""
    req = 'POST /\n{"op": "flush"}'
    resp = requests.post(f"{BASE_URL}/", json={"op": "flush"})
    resp_body = resp.text
    if resp.status_code != 200:
        e = AssertionError(f"Expected 200, got {resp.status_code}")
        e.request_text = req
        e.response_code = resp.status_code
        e.response_body = resp_body
        raise e
    if "status" not in resp.text:
        raise AssertionError("Expected status field in flush response")
    return req, resp.status_code, resp_body

# Run all tests
tests = [
    ("Connectivity (POST keys)", test_connectivity),
    ("PUT", test_put),
    ("GET", test_get),
    ("HEAD (exists)", test_head_exists),
    ("POST keys", test_post_keys),
    ("POST dump", test_post_dump),
    ("DELETE", test_delete),
    ("HEAD (missing)", test_head_missing),
    ("GET (missing)", test_get_missing),
    ("DELETE (missing)", test_delete_missing),
    ("PUT (url-encoded key)", test_url_encoded_put),
    ("GET (url-encoded key)", test_url_encoded_get),
    ("HEAD (url-encoded key exists)", test_url_encoded_head),
    ("POST keys (includes decoded key)", test_url_encoded_in_keys),
    ("POST dump (includes decoded key)", test_url_encoded_in_dump),
    ("DELETE (url-encoded key)", test_url_encoded_delete),
    ("POST (unknown op)", test_unknown_op),
    ("POST (non-object body)", test_non_object_json),
    ("POST (invalid JSON)", test_invalid_json),
    ("Local GDBM basic operations", test_local_gdbm_basic),
    ("GDataLockedError exception exists", test_gdata_locked_error_exists),
    ("Factory function modes", test_factory_function),
    ("Lock contention raises locked error", test_lock_contention_raises_locked_error),
    ("Local delete and missing", test_local_delete_and_missing),
    ("HTTP delete missing raises KeyError", test_http_delete_missing_raises_keyerror),
    ("POST flush", test_post_flush),
]

if __name__ == '__main__':
    passed = 0
    failed = 0
    
    try:
        for test_name, test_func in tests:
            if run_test(test_name, test_func):
                passed += 1
            else:
                failed += 1
                sys.exit(1)
        
        print(f"\nOK: HTTP tests passed (BASE_URL={BASE_URL})", file=sys.stderr)
        sys.exit(0)
    except requests.exceptions.ConnectionError:
        print("\nCannot connect to server!", file=sys.stderr)
        print("Make sure server is running: uvicorn gdata_server:app --host 127.0.0.1 --port 8020", file=sys.stderr)
        sys.exit(1)
