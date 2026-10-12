"""Small, dependency-light MCP tool plugin contract for the misc server."""

import hashlib
import importlib.util
import inspect
import json
import logging
import os
import tokenize
import traceback
from dataclasses import dataclass


logger = logging.getLogger(__name__)
_PLUGIN_CACHE = {}


def mcp_tool(name, description, input_schema):
    """Mark a public function as an MCP tool exported by its module."""
    def decorate(function):
        function.__mcp_tool__ = {
            'name': name,
            'description': description,
            'inputSchema': input_schema,
        }
        return function
    return decorate


@dataclass(frozen=True)
class PluginTool:
    name: str
    description: str
    input_schema: dict
    handler: object
    module_path: str

    async def invoke(self, arguments):
        try:
            result = self.handler(**(arguments or {}))
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str):
                return result
            return json.dumps(result, default=str)
        except Exception as exc:
            logger.exception('MCP plugin tool failed: %s', self.name)
            error = {
                'status': 'error',
                'error': 'MCP plugin tool failed',
                'tool': self.name,
                'exception_type': type(exc).__name__,
                'message': str(exc),
            }
            if os.getenv('MISC_MCP_DEBUG_TRACEBACKS', '').lower() in ('1', 'true', 'yes'):
                error['module'] = self.module_path
                error['traceback'] = traceback.format_exc()
            return json.dumps(error)


def _search_paths():
    """Return built-in tools plus MISC_MCP_TOOL_PATH directories.

    MISC_MCP_TOOL_PATH follows PYTHONPATH-style path-list syntax: directories
    are separated by os.pathsep (':' on Linux, ';' on Windows).
    """
    paths = [os.path.dirname(os.path.abspath(__file__))]
    if os.getenv('MISC_MCP_DEV_MODE', '').lower() not in ('1', 'true', 'yes'):
        return paths
    configured = os.getenv('MISC_MCP_TOOL_PATH', '')
    paths.extend(os.path.expanduser(path.strip()) for path in configured.split(os.pathsep) if path.strip())
    unique_paths = []
    seen = set()
    for path in paths:
        resolved = os.path.abspath(path)
        if resolved not in seen:
            seen.add(resolved)
            unique_paths.append(resolved)
    return unique_paths


def discover_tools(search_paths=None):
    """Load decorated Python modules, retaining the last good version on errors."""
    tools = {}
    server_directory = os.path.dirname(os.path.abspath(__file__))
    for directory in _search_paths() if search_paths is None else search_paths:
        if not os.path.isdir(directory):
            logger.warning('MCP tool plugin path does not exist: %s', directory)
            continue
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith('.py') or filename.startswith('_'):
                continue
            if filename.startswith('test_'):
                continue
            if os.path.abspath(directory) == server_directory and not filename.endswith('_tools.py'):
                continue
            path = os.path.join(directory, filename)
            try:
                with tokenize.open(path) as source_file:
                    source = source_file.read()
            except Exception as exc:
                logger.warning('Could not read MCP tool plugin %s: %s', path, exc)
                continue
            source_id = hashlib.sha1(source.encode('utf-8')).hexdigest()
            cached = _PLUGIN_CACHE.get(path)
            if cached and cached[0] == source_id:
                module_tools = cached[1]
                for plugin in module_tools:
                    if plugin.name not in tools:
                        tools[plugin.name] = plugin
                continue
            module_id = hashlib.sha1(f'{path}:{source_id}'.encode('utf-8')).hexdigest()
            module_name = f'_misc_mcp_plugin_{module_id}'
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                module = importlib.util.module_from_spec(spec)
                # Compile the source directly so reloads do not depend on .pyc
                # timestamp granularity or a stale bytecode cache.
                exec(compile(source, path, 'exec'), module.__dict__)
            except Exception as exc:
                logger.warning('Could not load MCP tool plugin %s: %s', path, exc)
                if cached:
                    for plugin in cached[1]:
                        if plugin.name not in tools:
                            tools[plugin.name] = plugin
                continue

            module_tools = []
            for _, handler in inspect.getmembers(module, inspect.isfunction):
                metadata = getattr(handler, '__mcp_tool__', None)
                if metadata is None or handler.__module__ != module.__name__:
                    continue
                name = metadata['name']
                if name in tools:
                    logger.warning('Ignoring duplicate MCP plugin tool %s from %s', name, path)
                    continue
                plugin = PluginTool(
                    name=name,
                    description=metadata['description'],
                    input_schema=metadata['inputSchema'],
                    handler=handler,
                    module_path=path,
                )
                module_tools.append(plugin)
                tools[name] = plugin
            _PLUGIN_CACHE[path] = (source_id, module_tools)
    return [tools[name] for name in sorted(tools)]
