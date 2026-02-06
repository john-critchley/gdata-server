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
