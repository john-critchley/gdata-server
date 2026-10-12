#!/usr/bin/env python3
"""Validate JSONHTL against the live schema, with recursive semantic checks."""
import argparse
import json
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import quote, urlsplit

from jsonschema import Draft7Validator


def read_json(url):
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def lint_document(document, schema, keys=None, metadata=False):
    issues = []
    for error in Draft7Validator(schema).iter_errors(document):
        path = '.'.join(map(str, error.absolute_path)) or '<document>'
        issues.append(f'{path}: {error.message}')
    if not isinstance(document, dict):
        return issues
    if metadata:
        for field in ('title', 'version', 'created', 'updated', 'tags'):
            if field not in document:
                issues.append(f'metadata: missing {field}')
    names = set()

    def walk(value, path='document'):
        if isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f'{path}[{i}]')
        elif isinstance(value, dict):
            cb = value.get('codeblock')
            if isinstance(cb, dict):
                for alias, canonical in (('text', 'body'), ('language', 'lang')):
                    if alias in cb:
                        issues.append(f'{path}.codeblock: use {canonical}, not {alias}')
                if ('exec' in cb or 'name' in cb) and document.get('runnable') is not True:
                    issues.append(f'{path}.codeblock: exec/name require runnable: true')
                name = cb.get('name')
                if isinstance(name, str):
                    if name in names:
                        issues.append(f'{path}.codeblock: duplicate name {name!r}')
                    names.add(name)
            table = value.get('table')
            if isinstance(table, dict) and isinstance(table.get('columns'), list):
                for i, row in enumerate(table.get('rows', [])):
                    if isinstance(row, list) and len(row) != len(table['columns']):
                        issues.append(f'{path}.table.rows[{i}]: expected {len(table["columns"])} cells, got {len(row)}')
            link = value.get('link')
            if keys is not None and isinstance(link, dict) and isinstance(link.get('href'), str):
                href = link['href']
                if not urlsplit(href).scheme and not href.startswith('#') and href.split('#', 1)[0] not in keys:
                    issues.append(f'{path}.link: unresolved key {href!r}')
            for field, child in value.items():
                walk(child, f'{path}.{field}')

    walk(document.get('content'))
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--key')
    source.add_argument('--file', type=Path)
    source.add_argument('--all', action='store_true')
    parser.add_argument('--schema', type=Path)
    parser.add_argument('--url', default='http://127.0.0.1:8021')
    parser.add_argument('--metadata', action='store_true')
    parser.add_argument('--links', action='store_true', help='Check public-store internal links; private-store links need separate review')
    args = parser.parse_args()
    base = args.url.rstrip('/')
    if args.schema:
        schema = json.loads(args.schema.read_text())
    else:
        note = read_json(base + '/JSONHTL_SCHEMA')
        schema = json.loads(next(b['codeblock']['body'] for b in note['content'] if b.get('codeblock', {}).get('lang') == 'json'))
    Draft7Validator.check_schema(schema)
    keys = None
    if args.links or args.all:
        request = Request(base + '/', data=json.dumps({'op': 'keys'}).encode(), headers={'Content-Type': 'application/json'})
        keys = set(read_json(request)['keys'])
    targets = sorted(keys) if args.all else [args.key or str(args.file)]
    count = 0
    for key in targets:
        doc = json.loads(args.file.read_text()) if args.file else read_json(base + '/' + quote(key, safe='/'))
        issues = lint_document(doc, schema, keys=keys if args.links else None, metadata=args.metadata)
        for issue in issues:
            print(f'ERROR {key}: {issue}. See JSONHTL_SCHEMA and README/jsonhtl-extensions.')
        count += len(issues)
    print(f'{len(targets)} documents checked; {count} issues')
    return int(count > 0)


if __name__ == '__main__':
    raise SystemExit(main())
