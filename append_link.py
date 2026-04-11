#!/usr/bin/env python3
"""Append a link to an existing note's content.

Usage: append_link.py <key> <target_key> <link_text> [surrounding_text_before] [surrounding_text_after]
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notes_client

if len(sys.argv) < 4:
    print(f"Usage: {sys.argv[0]} <key> <target_key> <link_text> [before] [after]", file=sys.stderr)
    sys.exit(1)

key = sys.argv[1]
target = sys.argv[2]
link_text = sys.argv[3]
before = sys.argv[4] if len(sys.argv) > 4 else "See also the "
after = sys.argv[5] if len(sys.argv) > 5 else "."

doc = notes_client.read_doc(key)
if isinstance(doc, str):
    print(f"Error: {doc}", file=sys.stderr)
    sys.exit(1)

para = [before, {"link": {"href": target, "text": link_text}}, after]
doc['content'].append({"para": para})

ok = notes_client.write_doc_raw(key, json.dumps(doc))
if ok:
    print(f"Appended link to '{target}' in '{key}'")
else:
    print("Failed", file=sys.stderr)
    sys.exit(1)
