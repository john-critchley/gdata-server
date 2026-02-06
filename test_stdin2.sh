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

  local req_max resp_max inner_width inside_width
  req_max="$(printf '%s\n' "${req}" | max_line_len)"
  resp_max="$(printf '%s\n' "${resp}" | max_line_len)"
  inner_width=60
  if (( req_max > inner_width )); then inner_width=${req_max}; fi
  if (( resp_max > inner_width )); then inner_width=${resp_max}; fi
  inside_width=$((inner_width + 2))

  echo "┌$(repeat_char "${inside_width}" '─')┐" >&2

  local label="REQUEST"
  local fill_len=$((inside_width - (${#label} + 3)))
  echo "├$(repeat_char 1 '─') ${label} $(repeat_char "${fill_len}" '─')┤" >&2
  while IFS= read -r line; do
    printf '│ %-*s │\n' "${inner_width}" "${line}" >&2
  done < <(printf '%s\n' "${req}")

  label="RESPONSE"
  fill_len=$((inside_width - (${#label} + 3)))
  echo "├$(repeat_char 1 '─') ${label} $(repeat_char "${fill_len}" '─')┤" >&2
  while IFS= read -r line; do
    printf '│ %-*s │\n' "${inner_width}" "${line}" >&2
  done < <(printf '%s\n' "${resp}")

  echo "└$(repeat_char "${inside_width}" '─')┘" >&2
}

run_req() {
  # Usage: run_req "<request text>"
  # Prints a boxed request/response to stderr for visibility; returns response on stdout.
  local req="$1"
  local resp
  resp="$(cd "${ROOT_DIR}" && printf '%s\n' "${req}" | "${PY}" ./gdata_server.py 2>/dev/null)"
  print_box "${req}" "${resp}"
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

echo "OK: stdin tests passed"
