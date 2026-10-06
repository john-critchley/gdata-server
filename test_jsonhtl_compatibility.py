"""Check the same recursive document through every JSONHTL consumer."""
import ast
import html
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import notes_web
from lint_jsonhtl import lint_document

ROOT = Path(__file__).parent
SCHEMA = json.loads((ROOT / 'jsonhtl_schema.json').read_text())
DOC = {'content': [{'section': {'title': 'Outer', 'content': [
    {'section': {'title': 'Inner', 'content': [{'para': ['nested']}] }},
    {'table': {'caption': 'Caption', 'columns': ['Reference'], 'rows': [
        [['See ', {'link': {'href': 'target', 'text': 'Target'}}, {'bold': 'Bold'}]]]}},
    {'details': {'summary': 'More', 'content': [{'para': ['detail text']}]}},
    {'list': {'ordered': True, 'items': [[{'code': 'item'}]]}},
    {'image': {'format': 'png', 'data': 'AA==', 'alt': 'Raster'}},
    {'svg': {'body': '<svg xmlns="http://www.w3.org/2000/svg"/>', 'alt': 'Vector'}},
]}}]}


def desktop_renderer(filename):
    # Extract the pure renderer class; importing the GUI would require a display.
    tree = ast.parse((ROOT / 'notes-browser' / filename).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'NotesHTMLRenderer')
    import re
    namespace = {'re': re, 'quote': quote, 'INLINE_SPAN_TAGS': {'code':'code','bold':'b','strong':'b','em':'i','italic':'i'}}
    spec = importlib.util.spec_from_file_location('svg_render', ROOT / 'notes-browser' / 'svg_render.py')
    svg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svg)
    namespace['svg_render'] = svg
    exec(compile(ast.Module(body=[cls], type_ignores=[]), filename, 'exec'), namespace)
    return namespace['NotesHTMLRenderer']()


@pytest.mark.parametrize('filename', ['notes_browser.py'])
def test_desktop_recursive_sections_and_inline_cells(filename):
    renderer = desktop_renderer(filename)
    content = DOC['content'][0]['section']['content'][:4]
    rendered = renderer.render('test', {'content': [{'section': {'title': 'Outer', 'content': content}}]})
    assert '<h2>Outer</h2>' in rendered and '<h3>Inner</h3>' in rendered
    assert 'navigate://target' in rendered and 'Caption' in rendered
    assert 'detail text' in rendered and '<ol>' in rendered
    assert "&#x27;link&#x27;" not in rendered


def test_web_recursive_document():
    rendered = notes_web._render_content(DOC['content'])
    assert '<h2>Outer</h2>' in rendered and '<h3>Inner</h3>' in rendered
    assert 'href="/notes/target"' in rendered
    assert '<caption>Caption</caption>' in rendered
    assert '<details>' in rendered and 'data:image/svg+xml;base64,' in rendered


def test_schema_and_semantic_checks():
    assert lint_document(DOC, SCHEMA, keys={'target'}) == []
    bad = {'content': [{'details': {'summary': 'x', 'content': [
        {'codeblock': {'language': 'python', 'text': 'bad', 'name': 'duplicate'}},
        {'codeblock': {'lang': 'python', 'body': 'ok', 'name': 'duplicate'}},
        {'table': {'columns': ['a'], 'rows': [['a', 'b']]}}
    ]}}]}
    issues = '\n'.join(lint_document(bad, SCHEMA))
    for phrase in ('use body', 'use lang', 'require runnable', 'duplicate name', 'expected 1 cells'):
        assert phrase in issues
    assert lint_document({'content': [['bare array']]}, SCHEMA)
    assert lint_document({'content': [{'para': [{'invalid': 'inline'}]}]}, SCHEMA)
    assert lint_document(DOC, SCHEMA, keys=set())


def test_shared_renderer_and_static_exporter():
    import sys
    sys.path.insert(0, str(Path('/home/john/py/envoy')))
    from jsonhtl_renderer import JSONHTLRenderer
    from jsonhtl_to_html import render_document
    for rendered in (JSONHTLRenderer().render_blocks(DOC['content']), render_document(DOC)):
        assert '<h2>Outer</h2>' in rendered and '<h3>Inner</h3>' in rendered
        assert 'href="target.html"' in rendered and '<strong>Bold</strong>' in rendered
        assert '<details>' in rendered and 'data:image/png;base64,' in rendered
        assert 'data:image/svg+xml;base64,' in rendered


@pytest.mark.parametrize('render', [notes_web._render_content])
def test_details_preserves_section_depth(render):
    blocks = [{'section': {'title': 'Outer', 'content': [
        {'details': {'summary': 'More', 'content': [
            {'section': {'title': 'Inner', 'content': []}}
        ]}}
    ]}}]
    assert '<h3>Inner</h3>' in render(blocks)
