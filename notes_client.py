#!/usr/bin/env python3
"""
Agent Notes Client - Python interface for the gdata notes server
Can be used as both a command-line tool and imported as a module.
"""
import requests
import json
import sys
import os

# Default URL, can be overridden via environment variable
NOTES_URL = os.getenv('NOTES_URL', "http://127.0.0.1:8021")

def read_doc(key=""):
    """Read a document by key (empty string for root)"""
    try:
        if key == "":
            response = requests.get(f"{NOTES_URL}/")
        else:
            response = requests.get(f"{NOTES_URL}/{key}")
        
        if response.status_code == 404:
            return f"Document '{key}' not found"
        response.raise_for_status()
        # HTTP mode returns raw document, not wrapped in {"value": ...}
        return response.json()
    except requests.exceptions.RequestException as e:
        return f"Error reading document: {e}"

def write_doc(key, content):
    """Write content to a document"""
    try:
        # If content is a string, try to parse it as JSON
        # If it fails, wrap it in a simple document structure
        if isinstance(content, str):
            try:
                parsed_content = json.loads(content)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Note value for '{key}' is not valid JSON: {e}. "
                    "Pass a dict directly or fix the JSON string before calling write_doc."
                ) from e
        else:
            # Content is already a dict/list — store directly
            parsed_content = content
        
        if key == "":
            response = requests.put(f"{NOTES_URL}/", json=parsed_content)
        else:
            response = requests.put(f"{NOTES_URL}/{key}", json=parsed_content)
        response.raise_for_status()
        return f"Document '{key}' saved successfully"
    except requests.exceptions.RequestException as e:
        return f"Error writing document: {e}"

def list_docs():
    """List all document keys"""
    try:
        response = requests.post(f"{NOTES_URL}/", json={"op": "keys"})
        response.raise_for_status()
        keys = response.json()['keys']
        return keys
    except requests.exceptions.RequestException as e:
        return f"Error listing documents: {e}"

def write_doc_raw(key, json_content):
    """Write raw JSON content directly to a document (for module use)"""
    try:
        headers = {'Content-Type': 'application/json'}
        if key == "":
            response = requests.put(f"{NOTES_URL}/", data=json_content, headers=headers)
        else:
            response = requests.put(f"{NOTES_URL}/{key}", data=json_content, headers=headers)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False

def delete_doc(key):
    """Delete a document"""
    try:
        if key == "":
            response = requests.delete(f"{NOTES_URL}/")
        else:
            response = requests.delete(f"{NOTES_URL}/{key}")
        response.raise_for_status()
        return f"Document '{key}' deleted successfully"
    except requests.exceptions.RequestException as e:
        return f"Error deleting document: {e}"

def patch_doc(key, op, block=None, index=None, indices=None, fields=None):
    """Apply a block-level patch operation to a document.

    ops: append_block, insert_block, replace_block, delete_block, delete_blocks, patch_meta

    Returns the server response dict, or a dict with 'error' on failure.
    """
    body = {'op': op}
    if block is not None:
        body['block'] = block
    if index is not None:
        body['index'] = index
    if indices is not None:
        body['indices'] = indices
    if fields is not None:
        body['fields'] = fields
    try:
        response = requests.post(f"{NOTES_URL}/{key}", json=body)
        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            return {'error': detail, 'status_code': response.status_code}
        return response.json()
    except requests.exceptions.RequestException as e:
        return {'error': str(e)}


def load_doc(key, filepath, delete_after=False):
    """Load a JSON file into a document.
    
    Args:
        key: Document key to store under
        filepath: Path to JSON file to load
        delete_after: If True, delete the file after successful load
    
    Returns:
        Success/error message string.
        On error, raises SystemExit(1) for CLI use.
    """
    # Read and validate the file
    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except IOError as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Validate JSON
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {filepath}: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Write to server
    result = write_doc(key, content)
    if 'Error' in result:
        print(result, file=sys.stderr)
        sys.exit(1)
    
    # Delete file if requested
    if delete_after:
        os.unlink(filepath)
    
    return result

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ('--help', '-h'):
        prog = os.path.basename(sys.argv[0])
        print(f"Usage: {prog} <command> [options] [args...]")
        print()
        print("Bootstrap (start here):")
        print(f"  {prog} read              System overview (root entry point)")
        print(f"  {prog} read README       Notes format and conventions")
        print(f"  {prog} read CONTENTS     Index of all notes")
        print(f"  {prog} read memory/envoy Envoy project session context")
        print()
        print("Commands:")
        print(f"  {prog} load [-d] <key> <file>")
        print(f"        Load a JSON file into a document.")
        print(f"        -d   Delete the file after it has been safely loaded.")
        print()
        print(f"  {prog} patch <key> append_block <block_json>")
        print(f"  {prog} patch <key> insert_block <index> <block_json>")
        print(f"  {prog} patch <key> replace_block <index> <block_json>")
        print(f"  {prog} patch <key> delete_block <index>")
        print(f"  {prog} patch <key> delete_blocks <index> [<index> ...]")
        print(f"  {prog} patch <key> patch_meta <fields_json>")
        print(f"        Apply a block-level patch to a document.")
        print()
        print(f"  {prog} read [key]              Read a document (default: root)")
        print(f"  {prog} write <key> <content>   Write content to a document")
        print(f"  {prog} write-stdin <key>        Write content from stdin")
        print(f"  {prog} list                     List all document keys")
        print(f"  {prog} delete <key>             Delete a document")
        print()
        print("Examples:")
        print(f"  {prog} load myconfig config.json")
        print(f"  {prog} patch mytodo append_block '{{\"para\": [\"New item.\"]}}'")
        print(f"  {prog} patch mytodo delete_block 3")
        print(f"  {prog} patch mytodo delete_blocks 3 5 7")
        print(f"  {prog} patch mytodo patch_meta '{{\"version\": 5, \"updated\": \"2026-03-24\"}}'")
        print()
        print("Environment:")
        print(f"  NOTES_URL   Server URL (default: {NOTES_URL})")
        sys.exit(0 if '--help' in sys.argv or '-h' in sys.argv else 1)
    
    command = sys.argv[1]
    
    if command == "read":
        key = sys.argv[2] if len(sys.argv) > 2 else ""
        result = read_doc(key)
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2))
        else:
            print(result)
    elif command == "write":
        if len(sys.argv) < 4:
            print("Usage: python notes_client.py write <key> <content>")
            sys.exit(1)
        key = sys.argv[2]
        content = " ".join(sys.argv[3:])  # Join all remaining args as content
        print(write_doc(key, content))
    elif command == "write-stdin":
        if len(sys.argv) < 3:
            print("Usage: python notes_client.py write-stdin <key>")
            sys.exit(1)
        key = sys.argv[2]
        content = sys.stdin.read()
        print(write_doc(key, content))
    elif command == "load":
        args = sys.argv[2:]
        delete_after = False
        if args and args[0] == '-d':
            delete_after = True
            args = args[1:]
        if len(args) < 2:
            prog = os.path.basename(sys.argv[0])
            print(f"Usage: {prog} load [-d] <key> <file>")
            print()
            print("Load a JSON file into a document.")
            print("  -d   Delete the file after it has been safely loaded.")
            sys.exit(1)
        key, filepath = args[0], args[1]
        print(load_doc(key, filepath, delete_after=delete_after))
    elif command == "list":
        keys = list_docs()
        if isinstance(keys, list):
            print("Documents:")
            for key in keys:
                display_key = '""' if key == "" else key
                print(f"  {display_key}")
        else:
            print(keys)
    elif command == "patch":
        if len(sys.argv) < 4:
            prog = os.path.basename(sys.argv[0])
            print(f"Usage: {prog} patch <key> <op> [args...]")
            sys.exit(1)
        key = sys.argv[2]
        op = sys.argv[3]
        rest = sys.argv[4:]
        kwargs = {}
        try:
            if op == 'append_block':
                if not rest:
                    print("Error: append_block requires <block_json>", file=sys.stderr)
                    sys.exit(1)
                kwargs['block'] = json.loads(rest[0])
            elif op in ('insert_block', 'replace_block'):
                if len(rest) < 2:
                    print(f"Error: {op} requires <index> <block_json>", file=sys.stderr)
                    sys.exit(1)
                kwargs['index'] = int(rest[0])
                kwargs['block'] = json.loads(rest[1])
            elif op == 'delete_block':
                if not rest:
                    print("Error: delete_block requires <index>", file=sys.stderr)
                    sys.exit(1)
                kwargs['index'] = int(rest[0])
            elif op == 'delete_blocks':
                if not rest:
                    print("Error: delete_blocks requires at least one <index>", file=sys.stderr)
                    sys.exit(1)
                kwargs['indices'] = [int(i) for i in rest]
            elif op == 'patch_meta':
                if not rest:
                    print("Error: patch_meta requires <fields_json>", file=sys.stderr)
                    sys.exit(1)
                kwargs['fields'] = json.loads(rest[0])
            else:
                print(f"Error: unknown op '{op}'", file=sys.stderr)
                sys.exit(1)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error parsing arguments: {e}", file=sys.stderr)
            sys.exit(1)
        result = patch_doc(key, op, **kwargs)
        if isinstance(result, dict) and 'error' in result:
            print(json.dumps(result), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result))
    elif command == "delete":
        if len(sys.argv) < 3:
            print("Usage: python notes_client.py delete <key>")
            sys.exit(1)
        key = sys.argv[2]
        print(delete_doc(key))
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)