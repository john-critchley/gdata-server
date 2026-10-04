import asyncio
import dbm.gnu as gdbm
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import mailbox_tools
from gdata import gdata
from mcp_tool_plugins import discover_tools


def run(coroutine):
    return asyncio.run(coroutine)


class PluginArchitectureTests(unittest.TestCase):

    def test_ignores_test_modules_even_when_name_ends_in_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'test_example_tools.py').write_text(
                'raise RuntimeError("test module must not be imported")\n',
                encoding='utf-8',
            )
            self.assertEqual([], discover_tools([directory]))

    def write_plugin(self, directory, filename, source):
        path = Path(directory, filename)
        path.write_text(source, encoding='utf-8')
        return path

    def test_discovers_sync_async_and_wrapper_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'example_tools.py', '''
from math import factorial
from mcp_tool_plugins import mcp_tool

@mcp_tool("factorial", "Calculate a factorial", {"type": "object"})
def factorial_tool(number):
    return {"value": factorial(number)}

@mcp_tool("async_echo", "Echo asynchronously", {"type": "object"})
async def async_echo(value):
    return {"value": value}
''')
            tools = {tool.name: tool for tool in discover_tools([directory])}

            self.assertEqual({'async_echo', 'factorial'}, set(tools))
            self.assertEqual({'value': 120}, json.loads(run(tools['factorial'].invoke({'number': 5}))))
            self.assertEqual({'value': 'hello'}, json.loads(run(tools['async_echo'].invoke({'value': 'hello'}))))
            self.assertEqual('example_tools.py', Path(tools['factorial'].module_path).name)

    def test_hot_reload_replaces_exported_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_plugin(directory, 'reload_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("reloadable", "First version", {"type": "object"})
def handler():
    return {"version": 1}
''')
            first = discover_tools([directory])[0]
            self.assertEqual({'version': 1}, json.loads(run(first.invoke({}))))

            path.write_text('''
from mcp_tool_plugins import mcp_tool
@mcp_tool("reloadable", "Second version", {"type": "object"})
def handler():
    return {"version": 2}
''', encoding='utf-8')
            second = discover_tools([directory])[0]
            self.assertEqual({'version': 2}, json.loads(run(second.invoke({}))))
            self.assertEqual('Second version', second.description)
            self.assertIsNot(first.handler, second.handler)

    def test_first_search_directory_wins_duplicate(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            source = '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("same_name", "Duplicate", {"type": "object"})
def handler():
    return {"source": SOURCE}
'''
            self.write_plugin(first, 'first_tools.py', source.replace('SOURCE', '"first"'))
            self.write_plugin(second, 'second_tools.py', source.replace('SOURCE', '"second"'))
            tools = discover_tools([first, second])
            self.assertEqual(1, len(tools))
            self.assertEqual({'source': 'first'}, json.loads(run(tools[0].invoke({}))))

    def test_bad_plugin_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'broken_tools.py', 'raise RuntimeError("broken plugin")\n')
            self.write_plugin(directory, 'good_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("good", "Good", {"type": "object"})
def handler():
    return "ok"
''')
            tools = discover_tools([directory])
            self.assertEqual(['good'], [tool.name for tool in tools])
            self.assertEqual('ok', run(tools[0].invoke({})))

    def test_crashing_plugin_returns_llm_readable_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'crash_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("crash", "Deliberately fail", {"type": "object"})
def handler():
    raise ValueError("deliberate plugin failure")
''')
            tool = discover_tools([directory])[0]
            with mock.patch.dict(os.environ, {'MISC_MCP_DEBUG_TRACEBACKS': '1'}):
                result = json.loads(run(tool.invoke({})))

            self.assertEqual('error', result['status'])
            self.assertEqual('MCP plugin tool failed', result['error'])
            self.assertEqual('crash', result['tool'])
            self.assertEqual('ValueError', result['exception_type'])
            self.assertEqual('deliberate plugin failure', result['message'])
            self.assertIn('raise ValueError', result['traceback'])
            self.assertIn('crash_tools.py', result['traceback'])

    def test_crashing_plugin_hides_traceback_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'private_crash_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("private_crash", "Private failure", {"type": "object"})
def handler():
    raise ValueError("secret-value")
''')
            with mock.patch.dict(os.environ, {}, clear=True):
                result = json.loads(run(discover_tools([directory])[0].invoke({})))
            self.assertEqual('ValueError', result['exception_type'])
            self.assertEqual('secret-value', result['message'])
            self.assertNotIn('traceback', result)
            self.assertNotIn('module', result)

    def test_external_paths_are_disabled_without_dev_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'external_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("external", "External", {"type": "object"})
def handler():
    return "ok"
''')
            with mock.patch.dict(os.environ, {
                'MISC_MCP_TOOL_PATH': directory,
                'MISC_MCP_DEV_MODE': '0',
            }, clear=True):
                self.assertNotIn('external', [tool.name for tool in discover_tools()])
            with mock.patch.dict(os.environ, {
                'MISC_MCP_TOOL_PATH': directory,
                'MISC_MCP_DEV_MODE': '1',
            }, clear=True):
                self.assertIn('external', [tool.name for tool in discover_tools()])

    def test_empty_search_path_does_not_use_defaults(self):
        self.assertEqual([], discover_tools([]))

    def test_missing_directory_and_non_python_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, '_hidden.py', 'raise RuntimeError("ignored")\n')
            self.write_plugin(directory, 'notes.txt', 'not a Python module')
            tools = discover_tools([directory, str(Path(directory, 'missing'))])
            self.assertEqual([], tools)

    def test_plugin_schema_and_defaults_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'schema_tools.py', '''
from mcp_tool_plugins import mcp_tool
SCHEMA = {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}}
@mcp_tool("schema", "Schema test", SCHEMA)
def handler(limit=10):
    return {"limit": limit}
''')
            tool = discover_tools([directory])[0]
            self.assertEqual('Schema test', tool.description)
            self.assertEqual('integer', tool.input_schema['properties']['limit']['type'])
            self.assertEqual({'limit': 10}, json.loads(run(tool.invoke({}))))
            self.assertEqual({'limit': 3}, json.loads(run(tool.invoke({'limit': 3}))))

    def test_primitive_and_null_results_are_json_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'values_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("number", "Number", {"type": "object"})
def number():
    return 7
@mcp_tool("nothing", "Nothing", {"type": "object"})
def nothing():
    return None
''')
            tools = {tool.name: tool for tool in discover_tools([directory])}
            self.assertEqual(7, json.loads(run(tools['number'].invoke({}))))
            self.assertIsNone(json.loads(run(tools['nothing'].invoke({}))))

    def test_missing_argument_returns_structured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'arguments_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("needs_argument", "Needs an argument", {"type": "object"})
def handler(required):
    return required
''')
            result = json.loads(run(discover_tools([directory])[0].invoke({})))
            self.assertEqual('error', result['status'])
            self.assertEqual('TypeError', result['exception_type'])
            self.assertIn('required', result['message'])

    def test_async_plugin_failure_has_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_plugin(directory, 'async_crash_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("async_crash", "Async failure", {"type": "object"})
async def handler():
    raise RuntimeError("async failure")
''')
            with mock.patch.dict(os.environ, {'MISC_MCP_DEBUG_TRACEBACKS': '1'}):
                result = json.loads(run(discover_tools([directory])[0].invoke({})))
            self.assertEqual('RuntimeError', result['exception_type'])
            self.assertIn('async failure', result['traceback'])

    def test_syntax_error_on_reload_removes_old_export_until_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_plugin(directory, 'syntax_tools.py', '''
from mcp_tool_plugins import mcp_tool
@mcp_tool("syntax_reload", "Valid", {"type": "object"})
def handler():
    return {"version": 1}
''')
            self.assertEqual(['syntax_reload'], [tool.name for tool in discover_tools([directory])])
            path.write_text('def broken(:\n', encoding='utf-8')
            retained = discover_tools([directory])
            self.assertEqual(['syntax_reload'], [tool.name for tool in retained])
            self.assertEqual({'version': 1}, json.loads(run(retained[0].invoke({}))))
            path.write_text('''
from mcp_tool_plugins import mcp_tool
@mcp_tool("syntax_reload", "Repaired", {"type": "object"})
def handler():
    return {"version": 3}
''', encoding='utf-8')
            tool = discover_tools([directory])[0]
            self.assertEqual('Repaired', tool.description)
            self.assertEqual({'version': 3}, json.loads(run(tool.invoke({}))))


class MailboxPluginTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.paths = {
            'envoy': str(root / 'email3.meta.gdbm'),
            'other': str(root / 'email3-other.meta.gdbm'),
            'mail': str(root / 'email3.mail.gdbm'),
            'jobs': str(root / 'jobserve.gdbm'),
            'bookings': str(root / 'dl.gdbm'),
        }
        with gdata(gdbm_file=self.paths['envoy']) as db:
            db['envoy-1'] = {'to': 'john@example.com', 'subject': 'Envoy notice'}
        with gdata(gdbm_file=self.paths['other']) as db:
            db['other-1'] = {'to': 'other@example.com', 'subject': 'Other notice'}
        with gdata(gdbm_file=self.paths['jobs']) as db:
            db['job-1'] = {'subject': 'Python role', 'job_type': 'development'}
            db['job-2'] = {'subject': 'Another role', 'job_type': 'testing'}
        with gdata(gdbm_file=self.paths['bookings']) as db:
            db['booking-1'] = {'date': '2026-09-06', 'venue': 'David Lloyd'}
        with gdbm.open(self.paths['mail'], 'c') as db:
            db[b'raw-1'] = (
                b'\xef\xbb\xbfFrom: sender@example.com\r\n'
                b'To: john@example.com\r\nSubject: Raw test\r\n'
                b'Content-Type: text/plain; charset=utf-8\r\n\r\nHello local mail\r\n'
            )

        self.old_paths = (
            mailbox_tools.MAILBOX_PATHS,
            mailbox_tools.MAIL_PATH,
            mailbox_tools.JOBS_PATH,
            mailbox_tools.BOOKINGS_PATH,
        )
        mailbox_tools.MAILBOX_PATHS = {
            'envoy': self.paths['envoy'],
            'other': self.paths['other'],
        }
        mailbox_tools.MAIL_PATH = self.paths['mail']
        mailbox_tools.JOBS_PATH = self.paths['jobs']
        mailbox_tools.BOOKINGS_PATH = self.paths['bookings']

    def tearDown(self):
        mailbox_tools.MAILBOX_PATHS, mailbox_tools.MAIL_PATH, mailbox_tools.JOBS_PATH, mailbox_tools.BOOKINGS_PATH = self.old_paths
        self.tempdir.cleanup()

    def test_mailbox_search_supports_types_filters_and_limits(self):
        envoy = json.loads(run(mailbox_tools.mailbox_search('envoy', to='john@example.com')))
        self.assertEqual('ok', envoy['status'])
        self.assertEqual('envoy-1', envoy['results'][0]['uidl'])

        all_mail = json.loads(run(mailbox_tools.mailbox_search('all', limit=1)))
        self.assertEqual(1, all_mail['count'])

        invalid = json.loads(run(mailbox_tools.mailbox_search('invalid')))
        self.assertEqual('error', invalid['status'])

    def test_jobs_bookings_and_raw_email_exports(self):
        jobs = json.loads(run(mailbox_tools.jobs_search(query='python')))
        bookings = json.loads(run(mailbox_tools.bookings_list(query='lloyd')))
        email = json.loads(run(mailbox_tools.email_read_local('raw-1')))
        self.assertEqual('ok', jobs['status'])
        self.assertEqual('ok', bookings['status'])
        self.assertEqual('ok', email['status'])
        self.assertEqual('Raw test', email['headers']['Subject'])
        self.assertIn('Hello local mail', email['body'])

    def test_case_insensitive_queries_and_limit_coercion(self):
        jobs = json.loads(run(mailbox_tools.jobs_search(query='PYTHON', limit='1')))
        self.assertEqual('ok', jobs['status'])
        self.assertEqual(1, jobs['count'])
        bookings = json.loads(run(mailbox_tools.bookings_list(query='NO MATCH')))
        self.assertEqual(0, bookings['count'])

    def test_missing_database_and_missing_email_are_structured_errors(self):
        missing = Path(self.tempdir.name, 'missing.gdbm')
        mailbox_tools.BOOKINGS_PATH = str(missing)
        result = json.loads(run(mailbox_tools.bookings_list()))
        self.assertEqual('error', result['status'])
        result = json.loads(run(mailbox_tools.email_read_local('not-there')))
        self.assertEqual('error', result['status'])
        self.assertIn('not-there', result['error'])

    def test_empty_uidl_is_rejected_without_opening_database(self):
        result = json.loads(run(mailbox_tools.email_read_local('')))
        self.assertEqual({'status': 'error', 'error': 'uidl is required'}, result)

    def test_corrupt_json_record_is_skipped(self):
        with gdbm.open(self.paths['jobs'], 'w') as db:
            db[b'corrupt'] = b'not-json'
        result = json.loads(run(mailbox_tools.jobs_search()))
        self.assertEqual('ok', result['status'])
        self.assertEqual(2, result['count'])
        self.assertEqual('job-1', result['jobs'][0]['key'])

    def test_local_reads_do_not_change_gdbm_values(self):
        with gdbm.open(self.paths['mail'], 'r') as db:
            before = db[b'raw-1']
        result = json.loads(run(mailbox_tools.email_read_local('raw-1')))
        self.assertEqual('ok', result['status'])
        with gdbm.open(self.paths['mail'], 'r') as db:
            self.assertEqual(before, db[b'raw-1'])

    def test_scan_limit_reports_truncation(self):
        old_limit = mailbox_tools.MAX_SCAN_RECORDS
        mailbox_tools.MAX_SCAN_RECORDS = 1
        try:
            result = json.loads(run(mailbox_tools.jobs_search()))
        finally:
            mailbox_tools.MAX_SCAN_RECORDS = old_limit
        self.assertTrue(result['truncated'])

    def test_large_raw_email_is_rejected(self):
        old_limit = mailbox_tools.MAX_EMAIL_BYTES
        mailbox_tools.MAX_EMAIL_BYTES = 10
        try:
            result = json.loads(run(mailbox_tools.email_read_local('raw-1')))
        finally:
            mailbox_tools.MAX_EMAIL_BYTES = old_limit
        self.assertEqual('error', result['status'])
        self.assertIn('read limit', result['error'])


class DeploymentScriptTests(unittest.TestCase):

    def test_startup_and_rc_scripts_are_production_safe(self):
        startup = Path('/home/john/py/gdata-server/start_misc_server.sh').read_text(encoding='utf-8')
        rc_script = Path('/home/john/py/gdata-server/misc_mcp_rc.sh').read_text(encoding='utf-8')
        self.assertIn('MISC_MCP_DEV_MODE="${MISC_MCP_DEV_MODE:-0}"', startup)
        self.assertIn('MISC_MCP_DEV_MODE=0', rc_script)
        self.assertIn('runuser -u john', rc_script)
        self.assertIn('/home/john/py/gdata-server/start_misc_server.sh', rc_script)
        self.assertIn('/home/john/py/gdata-server/misc_mcp_server.pid', rc_script)


class DevFilePluginTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.tool = discover_tools(['/home/john/py/mcp-tools'])[0]
        self.tool.handler.__globals__['ROOT'] = Path(self.tempdir.name).resolve()

    def tearDown(self):
        self.tempdir.cleanup()

    def call(self, **arguments):
        return json.loads(run(self.tool.invoke(arguments)))

    def test_create_read_line_edits_and_append(self):
        self.assertEqual('ok', self.call(operation='create', path='main.py', content='one\ntwo\n')['status'])
        self.assertEqual('two\n', self.call(operation='read', path='main.py', start_line=2, end_line=2)['content'])
        self.call(operation='replace_lines', path='main.py', start_line=2, end_line=2, content='TWO\n')
        self.call(operation='insert_lines', path='main.py', start_line=3, content='three\n')
        self.call(operation='insert_lines', path='main.py', start_line=4, content='four\n')
        self.call(operation='delete_lines', path='main.py', start_line=1, end_line=1)
        self.assertEqual('TWO\nthree\nfour\n', self.call(operation='read', path='main.py')['content'])

    def test_directory_operations_and_duplicate_create(self):
        self.call(operation='mkdir', path='pkg')
        self.call(operation='create', path='pkg/__init__.py', content='')
        listing = self.call(operation='list_directory', path='')
        self.assertEqual([{'name': 'pkg', 'type': 'directory'}], listing['entries'])
        duplicate = self.call(operation='create', path='pkg/__init__.py', content='again')
        self.assertEqual('error', duplicate['status'])
        self.call(operation='rename', path='pkg/__init__.py', new_path='pkg/module.py')
        self.call(operation='delete', path='pkg', recursive=True)

    def test_traversal_and_root_delete_are_rejected(self):
        traversal = self.call(operation='read', path='../outside')
        self.assertEqual('error', traversal['status'])
        root_delete = self.call(operation='delete', path='')
        self.assertEqual('error', root_delete['status'])

    def test_mailbox_exports_are_discoverable_and_read_only(self):
        tools = {tool.name: tool for tool in discover_tools([str(Path(mailbox_tools.__file__).parent)])}
        self.assertTrue({'mailbox_search', 'bookings_list', 'jobs_search', 'email_read_local'} <= set(tools))
        self.assertIn("mode='r'", Path(mailbox_tools.__file__).read_text(encoding='utf-8'))


class RealStoreSmokeTests(unittest.TestCase):

    def test_real_jobserve_store_is_readable_when_present(self):
        if not os.path.exists(mailbox_tools.JOBS_PATH):
            self.skipTest('real JobServe store is not present')
        result = json.loads(run(mailbox_tools.jobs_search(limit=1)))
        self.assertEqual('ok', result['status'])
        self.assertLessEqual(result['count'], 1)

    def test_real_raw_mail_store_is_readable_when_present(self):
        if not os.path.exists(mailbox_tools.MAIL_PATH):
            self.skipTest('real raw mail store is not present')
        with gdbm.open(mailbox_tools.MAIL_PATH, 'r') as db:
            uidl = db.firstkey().decode('utf-8')
        result = json.loads(run(mailbox_tools.email_read_local(uidl)))
        self.assertEqual('ok', result['status'])
        self.assertTrue(result['headers'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
