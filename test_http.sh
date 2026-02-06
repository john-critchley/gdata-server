#!/usr/bin/env bash
set -euo pipefail

# HTTP tests for gdata_server FastAPI app.
# Requires the server to already be running (e.g. `uvicorn gdata_server:app ...`).
#
# Usage:
#   BASE_URL=http://127.0.0.1:8020 ./test_http.sh

BASE_URL="${BASE_URL:-http://127.0.0.1:8020}"
BOX_INNER_WIDTH="${BOX_INNER_WIDTH:-72}"

require_cmd() {
  local cmd="$1"
  command -v "${cmd}" >/dev/null 2>&1 || {
    echo "Missing required command: ${cmd}" >&2
    exit 2
  }
}

require_cmd curl
require_cmd python
TEST_NAME=""
TEST_OK=1
TEST_FAILS=""

max_line_len() {
  local max=0
  local line
  while IFS= read -r line; do
    # Ignore carriage returns (e.g. from CRLF) when computing visual width.
    line="${line//$'\r'/}"
    # Ignore trailing whitespace when computing width (it isn't visible).
    while [[ "${line}" == *[[:space:]] ]]; do
      line="${line%[[:space:]]}"
    done
    if (( ${#line} > max )); then
      max=${#line}
    fi
  done
  printf '%s' "${max}"
}

repeat_char() {
  local n="$1"
  local ch="$2"
  if (( n <= 0 )); then
    return 0
  fi
  local i
  for ((i=0; i<n; i++)); do
    printf '%s' "${ch}"
  done
}

print_box() {
  local title="$1"
  local content="$2"

  local content_max inner_width inside_width
  content_max="$(printf '%s\n' "${content}" | max_line_len)"
  inner_width=72
  if (( content_max > inner_width )); then inner_width=${content_max}; fi
  inside_width=$((inner_width + 2))

  echo "┌$(repeat_char "${inside_width}" '─')┐" >&2
  local fill_len=$((inside_width - (${#title} + 3)))
  echo "├─ ${title} $(repeat_char "${fill_len}" '─')┤" >&2
  while IFS= read -r line; do
    printf '│ %-*s │\n' "${inner_width}" "${line}" >&2
  done < <(printf '%s\n' "${content}")
  echo "└$(repeat_char "${inside_width}" '─')┘" >&2
}

print_box_header() {
  local title="$1"
  local inside_width="$2"
  local fill_len=$((inside_width - (${#title} + 3)))
  echo "├─ ${title} $(repeat_char "${fill_len}" '─')┤" >&2
}

print_box_lines() {
  local inner_width="$1"
  local lines="$2"
  local line
  while IFS= read -r line; do
    line="${line//$'\r'/}"
    if (( ${#line} > inner_width )); then
      printf '│ %s\n' "${line}" >&2
    else
      printf '│ %-*s │\n' "${inner_width}" "${line}" >&2
    fi
  done < <(printf '%b\n' "${lines}")
}

print_test_box() {
  local test_name="$1"
  local req_text="$2"
  local http_code="$3"
  local resp_body_pretty="$4"
  local result="$5"
  local reasons_text="${6:-}"

  local resp_body_lines
  if [[ -n "${resp_body_pretty}" ]]; then
    resp_body_lines="${resp_body_pretty}"
  else
    resp_body_lines="(empty body)"
  fi

  local test_lines req_lines resp_lines all_lines
  test_lines=$(printf 'TEST: %s\n\n' "${test_name}")
  req_lines=$(printf '%b\n\n' "${req_text}")
  resp_lines=$(printf 'HTTP %s\n\n%b\n\nRESULT: %s\n' "${http_code}" "${resp_body_lines}" "${result}")

  all_lines=$(printf '%b%b%b' "${test_lines}" "${req_lines}" "${resp_lines}")
  if [[ -n "${reasons_text}" ]]; then
    all_lines+=$(printf '\n%b' "${reasons_text}")
  fi

  local inner_width inside_width
  inner_width="${BOX_INNER_WIDTH}"
  inside_width=$((inner_width + 2))

  echo "┌$(repeat_char "${inside_width}" '─')┐" >&2

  print_box_header "TEST" "${inside_width}"
  print_box_lines "${inner_width}" "${test_lines}"

  print_box_header "REQUEST" "${inside_width}"
  print_box_lines "${inner_width}" "${req_lines}"

  print_box_header "RESPONSE" "${inside_width}"
  print_box_lines "${inner_width}" "${resp_lines}"

  if [[ -n "${reasons_text}" ]]; then
    print_box_header "REASONS" "${inside_width}"
    print_box_lines "${inner_width}" "${reasons_text}"
  fi

  echo "└$(repeat_char "${inside_width}" '─')┘" >&2
}

fail_msg() {
  printf '%s\n' "$*"
  return 1
}

check_eq() {
  local got="$1"
  local expected="$2"
  local label="${3:-}"
  [[ "${got}" == "${expected}" ]] || fail_msg "${label} expected=${expected} got=${got}"
}

check_contains() {
  local haystack="$1"
  local needle="$2"
  local label="${3:-}"
  [[ "${haystack}" == *"${needle}"* ]] || fail_msg "${label} missing=${needle}"
}

json_check_field_equals() {
  local json_text="$1"
  local field="$2"
  local expected_repr="$3"

  local got
  got="$(
    JSON_TEXT="${json_text}" python - "$field" <<'PY'
import json
import os
import sys

field = sys.argv[1]
obj = json.loads(os.environ.get('JSON_TEXT', ''))
print(repr(obj.get(field)))
PY
  )"
  [[ "${got}" == "${expected_repr}" ]] || fail_msg "json field ${field} expected ${expected_repr} got ${got}"
}

json_check_path_contains_key() {
  local json_text="$1"
  local path_field="$2"   # top-level field holding a dict
  local key="$3"

  if ! JSON_TEXT="${json_text}" python - "$path_field" "$key" <<'PY'
import json
import os
import sys

path_field = sys.argv[1]
key = sys.argv[2]
obj = json.loads(os.environ.get('JSON_TEXT', ''))
d = obj.get(path_field)
if not isinstance(d, dict) or key not in d:
    raise SystemExit(1)
PY
  then
    fail_msg "json ${path_field} missing key ${key}"
  fi
}

json_check_list_contains() {
  local json_text="$1"
  local field="$2"
  local item="$3"

  if ! JSON_TEXT="${json_text}" python - "$field" "$item" <<'PY'
import json
import os
import sys

field = sys.argv[1]
item = sys.argv[2]
obj = json.loads(os.environ.get('JSON_TEXT', ''))
xs = obj.get(field)
if not isinstance(xs, list) or item not in xs:
    raise SystemExit(1)
PY
  then
    fail_msg "json list ${field} missing ${item}"
  fi
}

http_request() {
  # Sets globals: HTTP_CODE, HTTP_BODY, REQ_TEXT, RESP_TEXT.
  local method="$1"
  local path="$2"
  local body="${3:-}"

  local url="${BASE_URL}${path}"
  if [[ -n "${body}" ]]; then
    REQ_TEXT="$(printf '%s\n%s\n\n%s' "${method} ${url}" 'Content-Type: application/json' "${body}")"
  else
    REQ_TEXT="$(printf '%s' "${method} ${url}")"
  fi

  if [[ "${method}" == "HEAD" ]]; then
    local code
    code="$(curl -sS -o /dev/null -X HEAD -w '%{http_code}' "${url}")"
    HTTP_CODE="${code//$'\r'/}"
    HTTP_BODY=""
    HTTP_BODY_PRETTY=""
    RESP_TEXT=""
    return 0
  fi

  local out
  if [[ -n "${body}" ]]; then
    out="$(curl -sS -X "${method}" -H 'Content-Type: application/json' -d "${body}" -w '\nCODE:%{http_code}\n' "${url}")"
  else
    out="$(curl -sS -X "${method}" -w '\nCODE:%{http_code}\n' "${url}")"
  fi

  local resp_body resp_code
  resp_code="$(printf '%s' "${out}" | tail -n 1 | sed 's/^CODE://')"
  resp_body="$(printf '%s' "${out}" | sed '$d')"

  resp_code="${resp_code//$'\r'/}"
  resp_body="${resp_body//$'\r'/}"

  HTTP_CODE="${resp_code}"
  HTTP_BODY="${resp_body}"

  # Keep response readable; if JSON, pretty-print it.
  local resp_pretty="${resp_body}"
  if python - <<'PY' >/dev/null 2>&1
import json,sys
json.loads(sys.stdin.read())
PY
<<<"${resp_body}"; then
    resp_pretty="$(python -m json.tool <<<"${resp_body}")"
  fi

  HTTP_BODY_PRETTY="${resp_pretty}"
  RESP_TEXT="${resp_pretty}"
}

test_begin() {
  TEST_NAME="$1"
  TEST_OK=1
  TEST_FAILS=""
}

test_fail() {
  TEST_OK=0
  if [[ -n "${TEST_FAILS}" ]]; then
    TEST_FAILS+=$'\n'
  fi
  TEST_FAILS+="$1"
}

test_end() {
  local result="PASS"
  if (( TEST_OK == 0 )); then
    result="FAIL"
  fi

  local reasons_text=""
  if (( TEST_OK == 0 )); then
    reasons_text="${TEST_FAILS}"
  fi
  print_test_box "${TEST_NAME}" "${REQ_TEXT}" "${HTTP_CODE}" "${HTTP_BODY_PRETTY}" "${result}" "${reasons_text}"

  if (( TEST_OK == 0 )); then
    exit 1
  fi
}

expect_http() {
  local expected="$1"
  local msg
  msg="$(check_eq "${HTTP_CODE}" "${expected}" 'HTTP status' 2>/dev/null)" || true
  if [[ -n "${msg}" ]]; then
    test_fail "${msg}"
  fi
}

expect_json_field_equals() {
  local field="$1"
  local expected_repr="$2"
  local msg
  msg="$(json_check_field_equals "${HTTP_BODY}" "${field}" "${expected_repr}" 2>/dev/null)" || true
  if [[ -n "${msg}" ]]; then
    test_fail "${msg}"
  fi
}

expect_json_list_contains() {
  local field="$1"
  local item="$2"
  local msg
  msg="$(json_check_list_contains "${HTTP_BODY}" "${field}" "${item}" 2>/dev/null)" || true
  if [[ -n "${msg}" ]]; then
    test_fail "${msg}"
  fi
}

expect_json_path_contains_key() {
  local field="$1"
  local key="$2"
  local msg
  msg="$(json_check_path_contains_key "${HTTP_BODY}" "${field}" "${key}" 2>/dev/null)" || true
  if [[ -n "${msg}" ]]; then
    test_fail "${msg}"
  fi
}

expect_body_contains() {
  local needle="$1"
  local label="${2:-body}"
  local msg
  msg="$(check_contains "${HTTP_BODY}" "${needle}" "${label}" 2>/dev/null)" || true
  if [[ -n "${msg}" ]]; then
    test_fail "${msg}"
  fi
}

KEY="http_test_${RANDOM}_$$"
URL_SUFFIX="${RANDOM}_$$"
KEY_URL_ENC="http%20space_${URL_SUFFIX}"
KEY_URL_DEC="http space_${URL_SUFFIX}"

# 1) Connectivity (POST keys)
test_begin "Connectivity (POST keys)"
http_request POST / '{"op":"keys"}' || true
if ! [[ "${HTTP_CODE}" =~ ^[0-9]{3}$ ]]; then
  test_fail "could not contact server at ${BASE_URL}"
else
  expect_http 200
fi
test_end

# 2) PUT key
test_begin "PUT"
http_request PUT "/${KEY}" '{"value":{"x":1}}'
expect_http 200
expect_json_field_equals "status" "'ok'"
test_end

# 3) GET key
test_begin "GET"
http_request GET "/${KEY}"
expect_http 200
msg="$(check_contains "${HTTP_BODY}" '"value"' 'GET response' 2>/dev/null)" || true
if [[ -n "${msg}" ]]; then test_fail "${msg}"; fi
test_end

# 4) HEAD key exists
test_begin "HEAD (exists)"
http_request HEAD "/${KEY}"
expect_http 200
test_end

# 5) POST keys includes KEY
test_begin "POST keys"
http_request POST / '{"op":"keys"}'
expect_http 200
expect_json_list_contains "keys" "${KEY}"
test_end

# 6) POST dump includes KEY
test_begin "POST dump"
http_request POST / '{"op":"dump"}'
expect_http 200
expect_json_path_contains_key "items" "${KEY}"
test_end

# 7) DELETE key
test_begin "DELETE"
http_request DELETE "/${KEY}"
expect_http 200
expect_json_field_equals "status" "'deleted'"
test_end

# 8) HEAD key missing
test_begin "HEAD (missing)"
http_request HEAD "/${KEY}"
expect_http 404
test_end

# 9) GET missing key should return error payload
test_begin "GET (missing)"
http_request GET "/${KEY}"
expect_http 404
expect_body_contains '"detail"' "GET missing"
expect_body_contains 'key not found' "GET missing"
test_end

# 10) DELETE missing key should return error payload
test_begin "DELETE (missing)"
http_request DELETE "/${KEY}"
expect_http 404
expect_body_contains '"detail"' "DELETE missing"
expect_body_contains 'key not found' "DELETE missing"
test_end

# 11) URL-decoding: PUT/GET/HEAD key with spaces via %20
test_begin "PUT (url-encoded key)"
http_request PUT "/${KEY_URL_ENC}" '{"value":{"x":2}}'
expect_http 200
expect_json_field_equals "status" "'ok'"
test_end

test_begin "GET (url-encoded key)"
http_request GET "/${KEY_URL_ENC}"
expect_http 200
expect_body_contains '"value"' "GET url-encoded"
test_end

test_begin "HEAD (url-encoded key exists)"
http_request HEAD "/${KEY_URL_ENC}"
expect_http 200
test_end

# 12) POST keys should include decoded key
test_begin "POST keys (includes decoded key)"
http_request POST / '{"op":"keys"}'
expect_http 200
expect_json_list_contains "keys" "${KEY_URL_DEC}"
test_end

# 13) POST dump should include decoded key
test_begin "POST dump (includes decoded key)"
http_request POST / '{"op":"dump"}'
expect_http 200
expect_json_path_contains_key "items" "${KEY_URL_DEC}"
test_end

# 14) POST unknown op should return error + supported ops
test_begin "POST (unknown op)"
http_request POST / '{"op":"nope"}'
expect_http 200
expect_body_contains '"error"' "POST unknown op"
expect_body_contains 'supported_ops' "POST unknown op"
test_end

# 15) POST non-object JSON body should be rejected by FastAPI validation
test_begin "POST (non-object body)"
http_request POST / '[1,2,3]'
expect_http 422
expect_body_contains '"detail"' "POST non-object"
test_end

# 16) POST invalid JSON should be rejected by FastAPI validation
test_begin "POST (invalid JSON)"
http_request POST / '{this is not json}'
expect_http 422
expect_body_contains '"detail"' "POST invalid JSON"
test_end

# 17) Cleanup url-encoded key
test_begin "DELETE (url-encoded key)"
http_request DELETE "/${KEY_URL_ENC}"
expect_http 200
expect_json_field_equals "status" "'deleted'"
test_end

print_box "SUMMARY" "OK: HTTP tests passed (BASE_URL=${BASE_URL})"
