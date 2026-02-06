#!/usr/bin/env bash
set -euo pipefail

# Simple stdin-protocol tests for gdata_server.py
# Uses a temporary GDBM file so it won't touch your real DB.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

export LOG_LEVEL=ERROR
export GDBM_FILE="${TMP_DIR}/test.gdbm"

BOX_INNER_WIDTH="${BOX_INNER_WIDTH:-60}"

max_line_len() {
  local max=0
  local line
  while IFS= read -r line; do
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
  local req="$1"
  local resp="$2"

  local inner_width inside_width
  inner_width="${BOX_INNER_WIDTH}"
  inside_width=$((inner_width + 2))

  echo "┌$(repeat_char "${inside_width}" '─')┐" >&2

  local label="REQUEST"
  local fill_len=$((inside_width - (${#label} + 3)))
  echo "├$(repeat_char 1 '─') ${label} $(repeat_char "${fill_len}" '─')┤" >&2
  while IFS= read -r line; do
    if (( ${#line} > inner_width )); then
      printf '│ %s\n' "${line}" >&2
    else
      printf '│ %-*s │\n' "${inner_width}" "${line}" >&2
    fi
  done < <(printf '%s\n' "${req}")

  label="RESPONSE"
  fill_len=$((inside_width - (${#label} + 3)))
  echo "├$(repeat_char 1 '─') ${label} $(repeat_char "${fill_len}" '─')┤" >&2
  while IFS= read -r line; do
    if (( ${#line} > inner_width )); then
      printf '│ %s\n' "${line}" >&2
    else
      printf '│ %-*s │\n' "${inner_width}" "${line}" >&2
    fi
  done < <(printf '%s\n' "${resp}")

  echo "└$(repeat_char "${inside_width}" '─')┘" >&2
}

run_req() {
  # Usage: run_req "<request text>"
  # Prints a boxed request/response to stderr for visibility; returns response on stdout.
  local req="$1"
  local resp
  resp="$(cd "${ROOT_DIR}" && printf '%s\n' "${req}" | "${PY}" ./gdata_server.py 2>/dev/null)"
  local resp_pretty
  resp_pretty="$(
    RESP_TEXT="${resp}" "${PY}" - <<'PY'
import ast
import json
import os

s = os.environ.get('RESP_TEXT', '')
try:
    obj = json.loads(s)
    print(json.dumps(obj, indent=4, sort_keys=True))
except Exception:
    try:
        obj = ast.literal_eval(s)
        if isinstance(obj, (dict, list)):
            print(json.dumps(obj, indent=4, sort_keys=True))
        else:
            print(s)
    except Exception:
        print(s)
PY
  )"
  print_box "${req}" "${resp_pretty}"
  printf '%s\n' "${resp}"
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  if [[ "${haystack}" != *"${needle}"* ]]; then
    echo "ASSERT FAILED: expected output to contain: ${needle}" >&2
    echo "--- output ---" >&2
    echo "${haystack}" >&2
    echo "--------------" >&2
    exit 1
  fi
}

assert_not_contains() {
  local haystack="$1"
  local needle="$2"
  if [[ "${haystack}" == *"${needle}"* ]]; then
    echo "ASSERT FAILED: expected output NOT to contain: ${needle}" >&2
    echo "--- output ---" >&2
    echo "${haystack}" >&2
    echo "--------------" >&2
    exit 1
  fi
}

# 1) Empty DB: keys should be empty
out="$(run_req $'POST / HTTP/1.1\n\n{"op":"keys"}\n.')"
assert_contains "${out}" "'keys': []"

# 2) PUT alpha
out="$(run_req $'PUT /alpha HTTP/1.1\n\n{"value":{"x":1}}\n.')"
assert_contains "${out}" "'status': 'ok'"

# 3) GET alpha
out="$(run_req $'GET /alpha HTTP/1.1\n\n.')"
assert_contains "${out}" "'value': {'x': 1}"

# 4) HEAD alpha exists
out="$(run_req $'HEAD /alpha HTTP/1.1\n\n.')"
assert_contains "${out}" "'exists': True"

# 5) POST keys includes alpha
out="$(run_req $'POST / HTTP/1.1\n\n{"op":"keys"}\n.')"
assert_contains "${out}" "'keys':"
assert_contains "${out}" "alpha"

# 6) POST dump includes alpha and value
out="$(run_req $'POST / HTTP/1.1\n\n{"op":"dump"}\n.')"
assert_contains "${out}" "'items':"
assert_contains "${out}" "'alpha': {'x': 1}"

# 7) DELETE alpha
out="$(run_req $'DELETE /alpha HTTP/1.1\n\n.')"
assert_contains "${out}" "'status': 'deleted'"

# 8) HEAD alpha does not exist
out="$(run_req $'HEAD /alpha HTTP/1.1\n\n.')"
assert_contains "${out}" "'exists': False"

# 9) GET alpha should error
out="$(run_req $'GET /alpha HTTP/1.1\n\n.')"
assert_contains "${out}" "'error':"
assert_contains "${out}" "key not found"

# 10) DELETE missing key should error
out="$(run_req $'DELETE /alpha HTTP/1.1\n\n.')"
assert_contains "${out}" "'error':"
assert_contains "${out}" "key not found"

# 11) URL-decoding: PUT/GET/HEAD key with spaces via %20
out="$(run_req $'PUT /a%20b HTTP/1.1\n\n{"value":{"x":2}}\n.')"
assert_contains "${out}" "'status': 'ok'"
out="$(run_req $'GET /a%20b HTTP/1.1\n\n.')"
assert_contains "${out}" "'value': {'x': 2}"
out="$(run_req $'HEAD /a%20b HTTP/1.1\n\n.')"
assert_contains "${out}" "'exists': True"

# 12) POST keys should include decoded key
out="$(run_req $'POST / HTTP/1.1\n\n{"op":"keys"}\n.')"
assert_contains "${out}" "a b"

# 13) POST dump should include decoded key and value
out="$(run_req $'POST / HTTP/1.1\n\n{"op":"dump"}\n.')"
assert_contains "${out}" "'a b': {'x': 2}"

# 14) POST unknown op should error and list supported ops
out="$(run_req $'POST / HTTP/1.1\n\n{"op":"nope"}\n.')"
assert_contains "${out}" "'error':"
assert_contains "${out}" "unknown op"
assert_contains "${out}" "supported_ops"

# 15) POST non-object JSON body should error
out="$(run_req $'POST / HTTP/1.1\n\n[1,2,3]\n.')"
assert_contains "${out}" "'error': 'body must be a JSON object'"

# 16) POST invalid JSON body should be handled (parser falls back to {})
out="$(run_req $'POST / HTTP/1.1\n\n{this is not json}\n.')"
assert_contains "${out}" "'error':"
assert_contains "${out}" "unknown op"

echo "OK: stdin tests passed"
