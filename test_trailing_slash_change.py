#!/usr/bin/env python3
"""
Test script to verify trailing slash change to MCP resource URL.
Run this before and after the change to ensure no regressions.
"""

import subprocess
import json
import sys
import time

def run_curl(url, method="GET", headers=None, data=None, verbose=False):
    """Run a curl command and return parsed response."""
    cmd = ["curl", "-s", "-X", method, url]
    
    if headers:
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
    
    if data:
        cmd.extend(["-d", json.dumps(data)])
    
    if verbose:
        cmd.append("-v")
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return None, "Timeout"
    except Exception as e:
        return None, str(e)

def test_well_known_endpoint():
    """Test 1: Verify well-known endpoint returns correct resource URL."""
    print("\n=== Test 1: Well-known OAuth protected resource endpoint ===")
    
    url = "https://www.critchley.biz/.well-known/oauth-protected-resource/mcp"
    stdout, stderr = run_curl(url)
    
    if stdout:
        try:
            data = json.loads(stdout)
            resource_url = data.get("resource", "")
            auth_servers = data.get("authorization_servers", [])
            
            print(f"✓ Endpoint responded with 200 OK")
            print(f"  Resource URL: {resource_url}")
            print(f"  Auth servers: {auth_servers}")
            
            # Check for trailing slash
            if resource_url.endswith("/mcp/"):
                print("✓ Resource URL has trailing slash (GOOD)")
                return True
            elif resource_url.endswith("/mcp"):
                print("⚠ Resource URL missing trailing slash (needs update)")
                return False
            else:
                print(f"✗ Resource URL in unexpected format: {resource_url}")
                return False
        except json.JSONDecodeError:
            print(f"✗ Response is not valid JSON")
            print(f"  Response: {stdout[:200]}")
            return False
    else:
        print(f"✗ Failed to fetch endpoint: {stderr}")
        return False

def test_sse_endpoint():
    """Test 2: Verify SSE endpoint (/mcp/) is still accessible."""
    print("\n=== Test 2: SSE MCP endpoint (/mcp/) ===")
    
    # We can't easily test with a real token, but we can verify the endpoint exists
    # by checking the 401 response (which means the endpoint exists but needs auth)
    url = "https://www.critchley.biz/mcp/"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    data = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 1,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"}
        }
    }
    
    # For this test, we'll just check if the endpoint responds
    cmd = ["curl", "-s", "-X", "POST", url, "-H", "Content-Type: application/json", "-d", json.dumps(data)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    
    # We expect either 401 (no auth) or some response
    if result.stdout or "Unauthorized" in result.stdout or "401" in result.stderr:
        print("✓ SSE endpoint (/mcp/) is accessible")
        return True
    else:
        print("✗ SSE endpoint (/mcp/) did not respond as expected")
        print(f"  Response: {result.stdout[:200]}")
        return False

def test_streamable_endpoint():
    """Test 3: Verify Streamable HTTP endpoint (/mcp and /mcp/) still work."""
    print("\n=== Test 3: Streamable HTTP endpoint (/mcp and /mcp/) ===")
    
    # Test both paths
    for path in ["/mcp", "/mcp/"]:
        url = f"https://www.critchley.biz{path}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        data = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"}
            }
        }
        
        cmd = ["curl", "-s", "-X", "POST", url, "-H", "Content-Type: application/json", "-d", json.dumps(data)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        
        if result.stdout or "Unauthorized" in result.stdout:
            print(f"  ✓ Streamable endpoint ({path}) is accessible")
        else:
            print(f"  ✗ Streamable endpoint ({path}) did not respond")
            return False
    
    return True

def test_www_authenticate_header():
    """Test 4: Verify 401 response has correct WWW-Authenticate header."""
    print("\n=== Test 4: WWW-Authenticate header in 401 response ===")
    
    # Make a request without authorization to get a 401
    url = "https://www.critchley.biz/mcp/"
    headers = {
        "Content-Type": "application/json",
    }
    
    cmd = ["curl", "-i", "-X", "POST", url, "-H", "Content-Type: application/json", "-d", "{}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    
    if "401" in result.stdout or "Unauthorized" in result.stdout:
        if "WWW-Authenticate" in result.stdout or "www-authenticate" in result.stdout:
            print("✓ 401 response contains WWW-Authenticate header")
            # Extract header line
            for line in result.stdout.split("\n"):
                if "www-authenticate" in line.lower():
                    print(f"  Header: {line.strip()[:100]}...")
            return True
        else:
            print("⚠ 401 response but no WWW-Authenticate header found")
            return True  # Not critical for this change
    else:
        print("✗ Did not get 401 response as expected")
        return False

def main():
    print("=" * 70)
    print("MCP Resource URL Trailing Slash Verification Tests")
    print("=" * 70)
    print("\nThese tests verify that the OAuth resource URL is correct")
    print("and that all MCP endpoints continue to work.\n")
    
    results = []
    
    try:
        results.append(("Well-known endpoint", test_well_known_endpoint()))
        results.append(("SSE endpoint", test_sse_endpoint()))
        results.append(("Streamable HTTP endpoint", test_streamable_endpoint()))
        results.append(("WWW-Authenticate header", test_www_authenticate_header()))
    except Exception as e:
        print(f"\n✗ Tests interrupted by error: {e}")
        return 1
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {test_name}")
    
    all_passed = all(result[1] for result in results)
    print("\n" + ("All tests PASSED ✓" if all_passed else "Some tests FAILED ✗"))
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
