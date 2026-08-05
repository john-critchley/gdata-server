#!/usr/bin/env python3
"""Corpus sweep: find (and optionally migrate) non-canonical notes.

With the 203 read-contract in place a sweep is trivial: a note whose stored
form is non-canonical answers GET with 203 and a Warning header. Migrating it
is just writing the (already canonical) GET body back:

    GET key  -> 203 + canonical body   (dirty)
    PUT body -> 200                     (now clean)

Report-first by default; pass --apply to actually rewrite. Idempotent and
non-destructive (renames field names only; block count is unchanged).

Usage:
    python sweep_canonicalise.py --base http://127.0.0.1:8020            # report
    python sweep_canonicalise.py --base http://127.0.0.1:8020 --apply    # migrate
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request


def _req(method, url, data=None):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    # keep 203 out of the "error" path (urllib treats <400 as success anyway)
    resp = urllib.request.urlopen(req)
    # resp.headers is an http.client.HTTPMessage: case-insensitive .get().
    return resp.status, resp.read(), resp.headers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="REST base URL, e.g. http://127.0.0.1:8020")
    ap.add_argument("--apply", action="store_true", help="rewrite dirty notes (default: report only)")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    keys = json.loads(_req("POST", base + "/", json.dumps({"op": "keys"}).encode())[1])["keys"]
    dirty = []
    for key in keys:
        try:
            status, body, headers = _req("GET", f"{base}/{urllib.parse.quote(key)}")
        except Exception as e:                       # noqa: BLE001 -- report and continue
            print(f"  ! {key}: GET failed: {e}", file=sys.stderr)
            continue
        if status != 203:
            continue
        warnings = json.loads(headers.get("X-GData-Warnings", "[]"))
        dirty.append(key)
        print(f"DIRTY {key}")
        for w in warnings:
            print(f"        {w}")
        if args.apply:
            st, _, _ = _req("PUT", f"{base}/{urllib.parse.quote(key)}", body)
            print(f"        -> migrated (PUT {st})")

    print(f"\n{len(dirty)} non-canonical of {len(keys)} keys"
          + (" (migrated)" if args.apply else " (report only; use --apply to migrate)"))


if __name__ == "__main__":
    main()
