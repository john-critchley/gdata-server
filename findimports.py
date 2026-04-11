#!/usr/bin/env python3
"""List imports for Python files.

Usage: find . -name '*.py' -print0 | xargs -r0 ./findimports.py
       ./findimports.py file1.py file2.py ...

Output:
  file1.py: os sys json
  file2.py: requests json os sys
"""
import ast
import sys


def find_imports(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=filepath)
    except SyntaxError as e:
        print(f"{filepath}: SYNTAX ERROR: {e}", file=sys.stderr)
        return None
    except (IOError, OSError) as e:
        print(f"{filepath}: {e}", file=sys.stderr)
        return None

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split('.')[0])
    return sorted(imports)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} file1.py [file2.py ...]", file=sys.stderr)
        sys.exit(1)
    for path in sys.argv[1:]:
        imports = find_imports(path)
        if imports is not None:
            print(f"{path}: {' '.join(imports)}")
