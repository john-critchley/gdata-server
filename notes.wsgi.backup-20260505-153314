#!/usr/bin/env python3
"""WSGI application: browse notes as HTML, mounted at /notes.

Reads directly from the GDBM file (read-only).  No dependency on the
running gdata server.

Apache config needed (in the mod_wsgi block):
    WSGIScriptAlias /notes /usr/local/www/wsgi-scripts/notes.wsgi
"""

import json
import re
import urllib.request
import urllib.error
import urllib.parse
from html import escape
from urllib.parse import quote, unquote

# The gdata notes server (must be running)
GDATA_URL = 'http://127.0.0.1:8021'


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def read_doc(key):
    """Return parsed JSON for *key*, or None if not found."""
    try:
        if key:
            url = GDATA_URL + '/' + urllib.parse.quote(key, safe='')
        else:
            url = GDATA_URL + '/'
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def key_to_url(key, script_name):
    """Note key → absolute URL path (for use in href attributes)."""
    if key == '':
        return script_name + '/'
    return script_name + '/' + quote(key, safe='/')


def _markup(text):
    """HTML-escape then apply minimal markdown inline markup."""
    s = escape(text)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', s)
    return s


def _render_link(link, script_name):
    if isinstance(link, dict):
        href = link.get('href', '')
        text = link.get('text', href)
        if href.startswith(('http://', 'https://')):
            return f'<a href="{escape(href)}">{escape(text)}</a>'
        url = key_to_url(href, script_name)
        return f'<a href="{escape(url)}">{escape(text)}</a>'
    return escape(str(link))


def _render_inlines(items, script_name):
    parts = []
    for item in items:
        if isinstance(item, str):
            parts.append(_markup(item))
        elif isinstance(item, dict):
            if 'link' in item:
                parts.append(_render_link(item['link'], script_name))
            elif 'code' in item:
                parts.append(f'<code>{escape(str(item["code"]))}</code>')
            elif 'href' in item:
                parts.append(_render_link(item, script_name))
            else:
                parts.append(escape(str(item)))
        else:
            parts.append(escape(str(item)))
    return ''.join(parts)


def _render_para(para, script_name):
    if isinstance(para, str):
        return f'<p>{_markup(para)}</p>'
    if isinstance(para, list):
        return f'<p>{_render_inlines(para, script_name)}</p>'
    return f'<p>{escape(str(para))}</p>'


def _render_heading(h):
    if isinstance(h, dict):
        level = max(1, min(6, int(h.get('level', 2))))
        return f'<h{level}>{escape(h.get("text", ""))}</h{level}>'
    return f'<h2>{escape(str(h))}</h2>'


def _render_codeblock(cb):
    if isinstance(cb, dict):
        body = cb.get('body', cb.get('text', ''))
        lang = cb.get('lang', '')
        cls = f' class="language-{escape(lang)}"' if lang else ''
        return f'<pre><code{cls}>{escape(body)}</code></pre>'
    return f'<pre><code>{escape(str(cb))}</code></pre>'


def _render_list(lst, script_name):
    if not isinstance(lst, dict):
        return f'<ul><li>{escape(str(lst))}</li></ul>'
    label = lst.get('label', '')
    tag = 'ol' if lst.get('ordered') else 'ul'
    rows = []
    if label:
        rows.append(f'<p><b>{escape(label)}</b></p>')
    rows.append(f'<{tag}>')
    for item in lst.get('items', []):
        if isinstance(item, str):
            rows.append(f'<li>{_markup(item)}</li>')
        elif isinstance(item, list):
            rows.append(f'<li>{_render_inlines(item, script_name)}</li>')
        elif isinstance(item, dict):
            pairs = ', '.join(
                f'<b>{escape(str(k))}:</b> {escape(str(v))}'
                for k, v in item.items()
            )
            rows.append(f'<li>{pairs}</li>')
        else:
            rows.append(f'<li>{escape(str(item))}</li>')
    rows.append(f'</{tag}>')
    return '\n'.join(rows)


def _render_table(tbl):
    if not isinstance(tbl, dict):
        return f'<p>{escape(str(tbl))}</p>'
    cols = tbl.get('columns', [])
    rows = []
    rows.append('<table>')
    if cols:
        rows.append('<tr>' + ''.join(f'<th>{escape(str(c))}</th>' for c in cols) + '</tr>')
    for row in tbl.get('rows', []):
        cells = row if isinstance(row, list) else [row]
        rows.append('<tr>' + ''.join(f'<td>{escape(str(c))}</td>' for c in cells) + '</tr>')
    rows.append('</table>')
    return '\n'.join(rows)


def _render_blocks(blocks, script_name):
    html = []
    for block in blocks:
        if isinstance(block, str):
            html.append(f'<p>{escape(block)}</p>')
        elif isinstance(block, dict):
            if 'para' in block:
                html.append(_render_para(block['para'], script_name))
            elif 'heading' in block:
                html.append(_render_heading(block['heading']))
            elif 'codeblock' in block:
                html.append(_render_codeblock(block['codeblock']))
            elif 'list' in block:
                html.append(_render_list(block['list'], script_name))
            elif 'table' in block:
                html.append(_render_table(block['table']))
            # unknown block types silently ignored per spec
    return '\n'.join(html)


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; }
body {
    font-family: Georgia, serif;
    max-width: 860px;
    margin: 2em auto;
    padding: 0 1.2em;
    color: #222;
    line-height: 1.6;
}
nav {
    font-family: sans-serif;
    font-size: 0.85em;
    color: #666;
    margin-bottom: 1.8em;
    padding-bottom: 0.5em;
    border-bottom: 1px solid #ddd;
}
nav a { color: #0066cc; text-decoration: none; }
nav a:hover { text-decoration: underline; }
h1 { font-family: sans-serif; font-size: 1.7em; margin-top: 0.2em; }
h2, h3, h4, h5, h6 { font-family: sans-serif; }
a { color: #0066cc; }
pre {
    background: #f5f5f5;
    padding: 0.9em 1.1em;
    overflow-x: auto;
    border-left: 3px solid #bbb;
    border-radius: 2px;
}
code { font-family: 'Courier New', monospace; font-size: 0.88em; }
pre code { font-size: 0.85em; }
table { border-collapse: collapse; margin: 1em 0; }
th, td { border: 1px solid #bbb; padding: 5px 11px; text-align: left; }
th { background: #eee; font-family: sans-serif; }
.meta {
    font-family: sans-serif;
    font-size: 0.82em;
    color: #555;
    border-top: 1px solid #ddd;
    margin-top: 2.5em;
    padding-top: 0.8em;
}
.meta table { font-size: 1em; }
.meta th { font-weight: normal; color: #777; }
"""


def _page(key, doc, script_name):
    """Render a JSONHTL document as a complete HTML page."""
    if isinstance(doc, dict):
        title = doc.get('title') or key or 'Notes'
    else:
        title = key or 'Notes'

    root_url = key_to_url('', script_name)

    parts = [
        '<!DOCTYPE html>',
        '<html lang="en">',
        '<head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{escape(title)} — Notes</title>',
        f'<style>{_CSS}</style>',
        '</head>',
        '<body>',
        '<nav>',
    ]

    if key == '':
        parts.append('<b>Notes</b>')
    else:
        # Breadcrumb: Notes › segment1 › segment2 …
        parts.append(f'<a href="{escape(root_url)}">Notes</a>')
        segments = key.split('/')
        for i, seg in enumerate(segments):
            ancestor_key = '/'.join(segments[:i + 1])
            if i < len(segments) - 1:
                ancestor_url = key_to_url(ancestor_key, script_name)
                parts.append(f' › <a href="{escape(ancestor_url)}">{escape(seg)}</a>')
            else:
                parts.append(f' › <b>{escape(seg)}</b>')

    parts.append('</nav>')

    if isinstance(doc, dict):
        content = doc.get('content', [])
        if isinstance(content, str):
            parts.append(f'<p>{escape(content)}</p>')
        elif isinstance(content, list):
            parts.append(_render_blocks(content, script_name))

        # Metadata table (everything except title and content)
        meta = {k: v for k, v in doc.items() if k not in ('title', 'content')}
        if meta:
            parts.append('<div class="meta"><table>')
            for field, value in meta.items():
                val_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                parts.append(
                    f'<tr><th>{escape(field)}</th>'
                    f'<td><code>{escape(val_str)}</code></td></tr>'
                )
            parts.append('</table></div>')

    elif isinstance(doc, list):
        parts.append(_render_blocks(doc, script_name))
    else:
        parts.append(f'<p>{escape(str(doc))}</p>')

    parts.append('</body></html>')
    return '\n'.join(parts)


def _not_found(key, script_name):
    root_url = key_to_url('', script_name)
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<title>Not found — Notes</title>'
        f'<style>{_CSS}</style>'
        '</head>\n<body>'
        f'<nav><a href="{escape(root_url)}">Notes</a></nav>'
        f'<h1>Not found</h1>'
        f'<p>No document with key <code>{escape(key or "(root)")}</code>.</p>'
        '</body></html>'
    )


# ---------------------------------------------------------------------------
# WSGI entry point
# ---------------------------------------------------------------------------

def application(environ, start_response):
    path_info = environ.get('PATH_INFO', '')
    script_name = environ.get('SCRIPT_NAME', '/notes')

    # Decode the note key from the URL path
    key = unquote(path_info.lstrip('/'))

    doc = read_doc(key)

    if doc is None:
        body = _not_found(key, script_name).encode('utf-8')
        start_response('404 Not Found', [
            ('Content-Type', 'text/html; charset=utf-8'),
            ('Content-Length', str(len(body))),
        ])
        return [body]

    body = _page(key, doc, script_name).encode('utf-8')
    start_response('200 OK', [
        ('Content-Type', 'text/html; charset=utf-8'),
        ('Content-Length', str(len(body))),
    ])
    return [body]
