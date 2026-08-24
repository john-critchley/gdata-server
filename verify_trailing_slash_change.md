# Verification Plan: Add Trailing Slash to MCP Resource URL

## Problem
The OAuth resource metadata currently returns:
- `"resource":"https://www.critchley.biz/mcp"` (no trailing slash)

But the actual endpoints are:
- SSE: `https://www.critchley.biz/mcp/` (with trailing slash)
- Streamable HTTP: `https://www.critchley.biz/mcp` (without trailing slash)

OAuth clients may treat these as different resources, causing authentication issues.

## Solution
Change the resource URL to include a trailing slash: `https://www.critchley.biz/mcp/`

This makes it consistent with the SSE endpoint and is the more conservative/safer choice for OAuth.

## Files to Change
1. **gdata_oauth.py line 276**: Protected resource metadata endpoint
   - From: `"resource": f"{ISSUER}/mcp"`
   - To:   `"resource": f"{ISSUER}/mcp/"`

2. **gdata_oauth.py line 203**: WWW-Authenticate header
   - From: `f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'`
   - To:   `f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'` (NO CHANGE - already points to the metadata endpoint correctly)

## Tests to Run (Verification)

### 1. Well-known endpoint returns correct resource URL
```bash
curl -i https://www.critchley.biz/.well-known/oauth-protected-resource/mcp
# Should return: "resource":"https://www.critchley.biz/mcp/"
```

### 2. Both MCP transports still work
```bash
# Test SSE transport with trailing slash
curl -i -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  https://www.critchley.biz/mcp/

# Test Streamable HTTP (should work with both /mcp and /mcp/)
curl -i -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  https://www.critchley.biz/mcp
```

### 3. OAuth 401 response still contains correct header
```bash
curl -i -X POST \
  -H "Content-Type: application/json" \
  -d '{}' \
  https://www.critchley.biz/mcp/
# Check WWW-Authenticate header contains correct resource metadata endpoint
```

### 4. Run pytest tests locally
```bash
python -m pytest test_mcp_transports.py -v
# All existing tests should still pass
```

## Risk Assessment
- **Low Risk**: The change is backwards compatible
  - OAuth clients that normalize URLs will accept both
  - Streamable HTTP handles both `/mcp` and `/mcp/` via Starlette routing
  - SSE already expects `/mcp/` with trailing slash
- **Benefits**: 
  - Consistency across resource declarations
  - Fixes potential OAuth client incompatibilities
  - Makes resource URL match actual endpoint
