#!/usr/bin/env python3
"""Convert a markdown file to a JSONHTL note and write it to the notes server.

Usage: md2jsonhtl.py <key> <file.md> [title]
"""
import sys
import json
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notes_client

def md_to_blocks(md):
    blocks = []
    lines = md.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]

        # Headings
        if line.startswith('#'):
            level = len(line) - len(line.lstrip('#'))
            text = line.lstrip('#').strip()
            blocks.append({'heading': {'level': level, 'text': text}})
            i += 1
            continue

        # Fenced codeblocks
        if line.startswith('```'):
            lang = line[3:].strip() or None
            body_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                body_lines.append(lines[i])
                i += 1
            cb = {'body': '\n'.join(body_lines)}
            if lang:
                cb['lang'] = lang
            blocks.append({'codeblock': cb})
            i += 1
            continue

        # Horizontal rule
        if line.strip() == '---':
            i += 1
            continue

        # Blockquote
        if line.startswith('> '):
            blocks.append({'para': [line[2:].strip()]})
            i += 1
            continue

        # Empty line
        if line.strip() == '':
            i += 1
            continue

        # Table (markdown pipe tables)
        if '|' in line and line.strip().startswith('|'):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                stripped = lines[i].strip()
                if not all(c in '|-: ' for c in stripped):
                    cells = [c.strip() for c in stripped.strip('|').split('|')]
                    table_lines.append(cells)
                i += 1
            if len(table_lines) >= 2:
                blocks.append({'table': {
                    'columns': table_lines[0],
                    'rows': table_lines[1:]
                }})
            elif table_lines:
                for row in table_lines:
                    blocks.append({'para': ['| ' + ' | '.join(row) + ' |']})
            continue

        # Bare JSON object (unfenced)
        if line.strip().startswith('{'):
            json_lines = []
            while i < len(lines) and lines[i].strip() != '' and not lines[i].startswith('#') and lines[i].strip() != '---':
                json_lines.append(lines[i])
                i += 1
            blocks.append({'codeblock': {'lang': 'json', 'body': '\n'.join(json_lines)}})
            continue

        # Regular paragraph / list items
        para_parts = []
        while (i < len(lines) and lines[i].strip() != ''
               and not lines[i].startswith('#')
               and not lines[i].startswith('```')
               and lines[i].strip() != '---'
               and not (lines[i].strip().startswith('|') and '|' in lines[i])
               and not lines[i].strip().startswith('{')):
            para_parts.append(lines[i])
            i += 1
        if para_parts:
            if any(p.startswith('- ') or p.startswith('  - ') for p in para_parts):
                for p in para_parts:
                    blocks.append({'para': [p]})
            else:
                blocks.append({'para': [' '.join(para_parts)]})

    return blocks

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <key> <file.md> [title]", file=sys.stderr)
        sys.exit(1)

    key = sys.argv[1]
    filepath = sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else None

    with open(filepath) as f:
        md = f.read()

    blocks = md_to_blocks(md)

    # Derive title from first heading if not given
    if not title:
        for b in blocks:
            if 'heading' in b:
                title = b['heading']['text']
                break
        if not title:
            title = key

    doc = {
        'title': title,
        'version': 1,
        'created': '2026-02-09',
        'content': blocks
    }

    json_str = json.dumps(doc)
    ok = notes_client.write_doc_raw(key, json_str)
    if ok:
        print(f"Wrote '{key}' ({len(blocks)} blocks)")
    else:
        print(f"Failed to write '{key}'", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
