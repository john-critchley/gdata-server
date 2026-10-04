"""Runtime MCP wrapper for the tracked development-file implementation."""

from mcp_tool_plugins import mcp_tool
from dev_plugins import dev_file_tools as implementation


ROOT = implementation.ROOT


@mcp_tool(
    'dev_file',
    'Create, inspect, edit, move, list, or delete files only inside /home/john/py/mcp-tools. Line numbers are 1-based and inclusive.',
    {
        'type': 'object',
        'properties': {
            'operation': {'type': 'string', 'enum': ['create', 'read', 'insert_lines', 'replace_lines', 'delete_lines', 'list_directory', 'mkdir', 'delete', 'rename']},
            'path': {'type': 'string', 'description': 'Path relative to /home/john/py/mcp-tools.'},
            'new_path': {'type': 'string', 'description': 'Destination path relative to the plugin root for rename.'},
            'content': {'type': 'string', 'description': 'File content or replacement text; not needed for delete_lines.'},
            'start_line': {'type': 'integer', 'minimum': 1, 'description': '1-based inclusive start line.'},
            'end_line': {'type': 'integer', 'minimum': 1, 'description': '1-based inclusive end line.'},
            'parents': {'type': 'boolean', 'default': True},
            'recursive': {'type': 'boolean', 'default': False},
        },
        'required': ['operation'],
    },
)
async def dev_file(operation, **arguments):
    implementation.ROOT = ROOT
    return await implementation.dispatch(operation, arguments)
