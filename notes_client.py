#!/usr/bin/env python3
"""
Agent Notes Client - Python interface for the gdata notes server
"""
import requests
import json
import sys

NOTES_URL = "http://127.0.0.1:8021"

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
                # Try to parse as JSON first
                parsed_content = json.loads(content)
            except json.JSONDecodeError:
                # If not JSON, create a simple document
                parsed_content = {
                    "content": content,
                    "type": "text",
                    "created": "2026-02-05"
                }
        else:
            # Content is already structured
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

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python notes_client.py read [key]")
        print("  python notes_client.py write <key> <content>")
        print("  python notes_client.py write-stdin <key>  # read content from stdin")
        print("  python notes_client.py list")
        print("  python notes_client.py delete <key>")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "read":
        key = sys.argv[2] if len(sys.argv) > 2 else ""
        print(read_doc(key))
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
    elif command == "list":
        keys = list_docs()
        if isinstance(keys, list):
            print("Documents:")
            for key in keys:
                display_key = '""' if key == "" else key
                print(f"  {display_key}")
        else:
            print(keys)
    elif command == "delete":
        if len(sys.argv) < 3:
            print("Usage: python notes_client.py delete <key>")
            sys.exit(1)
        key = sys.argv[2]
        print(delete_doc(key))
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)