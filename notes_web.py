"""
notes_web.py — JSONHTL notes renderer with writable_note support

Routes added to the REST app:
  GET /notes/            → redirect to /notes/README
  GET /notes/{key:path}  → render note as HTML
  POST /writeback/       → handle writable_note form submissions
"""

import base64
import html as _html
import json
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

_CSS = """\
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
.bks { width: 100%; }
.bks th { background: #222; color: #fff; }
.bks tr:nth-child(even) { background: #f9f9f9; }
.bks th, .bks td { border: 1px solid #ddd; padding: 8px; }
figure { margin: 1em 0; }
figcaption { font-family: sans-serif; font-size: 0.85em; color: #666; margin-top: 0.3em; }
details {
    border: 1px solid #ddd;
    border-radius: 4px;
    padding: 0.4em 0.9em;
    margin: 0.8em 0;
    background: #fafafa;
}
details summary {
    font-family: sans-serif;
    font-size: 0.9em;
    color: #444;
    cursor: pointer;
    padding: 0.2em 0;
}
details[open] summary { margin-bottom: 0.4em; border-bottom: 1px solid #eee; }
.expand-all-row { font-family: sans-serif; font-size: 0.85em; }
.expand-all-row button {
    font-family: sans-serif;
    font-size: 0.85em;
    color: #444;
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 4px;
    padding: 0.3em 0.8em;
    cursor: pointer;
}
.expand-all-row button:hover { background: #f0f0f0; }
.meta {
    font-family: sans-serif;
    font-size: 0.8em;
    color: #888;
    border-top: 1px solid #eee;
    margin-top: 2.5em;
    padding-top: 0.6em;
}
.meta-key { font-weight: 600; color: #666; }
.writable-note {
    margin: 1.5em 0;
    padding: 1em;
    background: #f9f9f9;
    border: 1px solid #ddd;
    border-radius: 4px;
}
.writable-note textarea {
    display: block;
    width: 100%;
    font-family: 'Courier New', monospace;
    font-size: 0.9em;
    padding: 0.7em;
    border: 1px solid #bbb;
    border-radius: 2px;
    min-height: 200px;
    resize: vertical;
}
.writable-note button {
    padding: 0.5em 1.2em;
    font-size: 0.95em;
    background: #0066cc;
    color: white;
    border: none;
    border-radius: 2px;
    cursor: pointer;
    font-family: sans-serif;
    margin-top: 0.8em;
}
.writable-note button:hover {
    background: #0052a3;
}
"""


# ---------------------------------------------------------------------------
# JSONHTL → HTML renderer
# ---------------------------------------------------------------------------

def _md_inline(text: str) -> str:
    """Convert **bold**, `code`, *italic* in a plain string to HTML."""
    text = _html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = text.replace('\n', '<br>')
    return text


def _href(target: str) -> str:
    if target.startswith('http://') or target.startswith('https://'):
        return target
    return f'/notes/{target}'


def _render_inline(item) -> str:
    if isinstance(item, str):
        return _md_inline(item)
    if isinstance(item, dict):
        if 'link' in item:
            lnk = item['link']
            return f'<a href="{_html.escape(_href(lnk["href"]))}">{_html.escape(lnk.get("text", lnk["href"]))}</a>'
        if 'code' in item:
            return f'<code>{_html.escape(item["code"])}</code>'
        if 'bold' in item:
            return f'<strong>{_html.escape(item["bold"])}</strong>'
    return ''


def _render_inlines(items) -> str:
    if isinstance(items, str):
        items = [items]
    joined = ''.join(_render_inline(i) for i in items)
    # A **bold**/*italic* span can be split across separate string items by an
    # intervening link/code element (e.g. "**foo " + {code} + " bar.**"). Each
    # item is converted independently above, so any markers that didn't find
    # their pair inside their own item are left as literal text here. Do a
    # second pass over the joined string to catch those spanning cases.
    joined = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', joined, flags=re.DOTALL)
    joined = re.sub(r'\*(.+?)\*', r'<em>\1</em>', joined, flags=re.DOTALL)
    return joined


def _render_list_item(item) -> str:
    if isinstance(item, str):
        return f'<li>{_md_inline(item)}</li>'
    if isinstance(item, list):
        return f'<li>{_render_inlines(item)}</li>'
    if isinstance(item, dict):
        if 'para' in item:
            return f'<li>{_render_inlines(item["para"])}</li>'
        if any(k in item for k in ('link', 'code', 'bold', 'href')):
            return f'<li>{_render_inline(item)}</li>'
        pairs = ' '.join(
            f'<strong>{_html.escape(str(k))}:</strong> {_html.escape(str(v))}'
            for k, v in item.items()
        )
        return f'<li>{pairs}</li>'
    return ''


def _render_svg(sv) -> str:
    """Render an svg block as a data-URI <img>, never inline <svg> markup.

    Inline SVG can carry <script>, on*= handlers, or <foreignObject> — and
    this page is public. Browsers treat img-loaded SVG as a static,
    non-scriptable image regardless of what the markup contains, so this
    sidesteps sanitization entirely rather than trying to allowlist it.
    """
    if not isinstance(sv, dict):
        return ''
    body = sv.get('body', '')
    if not isinstance(body, str) or not body.strip():
        return ''
    b64 = base64.b64encode(body.encode('utf-8')).decode('ascii')
    alt = _html.escape(sv.get('alt', '') or '')
    img = f'<img src="data:image/svg+xml;base64,{b64}" alt="{alt}" style="max-width:100%;height:auto;">'
    caption = sv.get('caption')
    if caption:
        return f'<figure>{img}<figcaption>{_html.escape(str(caption))}</figcaption></figure>'
    return f'<figure>{img}</figure>'


def _render_image(im) -> str:
    """Render an image block (raster, e.g. a fixed-note matplotlib plot)
    as a data-URI <img>. Unlike svg.body this has no un-encoded form —
    raster bytes aren't text — so base64 isn't a choice here, just the
    only option.
    """
    if not isinstance(im, dict):
        return ''
    data = im.get('data', '')
    if not isinstance(data, str) or not data.strip():
        return ''
    fmt = _html.escape(im.get('format', 'png') or 'png')
    alt = _html.escape(im.get('alt', '') or '')
    img = f'<img src="data:image/{fmt};base64,{_html.escape(data)}" alt="{alt}" style="max-width:100%;height:auto;">'
    caption = im.get('caption')
    if caption:
        return f'<figure>{img}<figcaption>{_html.escape(str(caption))}</figcaption></figure>'
    return f'<figure>{img}</figure>'


def _render_details(d) -> str:
    """Render a details block as native <details>/<summary> — collapsed by
    default, expandable per-item with zero JavaScript (browser-native), and
    see _render_page for the pure-CSS "expand all" control.
    """
    if not isinstance(d, dict):
        return ''
    summary = _html.escape(str(d.get('summary', '') or ''))
    nested = _render_content(d.get('content', []))
    return f'<details><summary>{summary}</summary>{nested}</details>'


def _render_block(block) -> str:
    if not isinstance(block, dict):
        return ''

    if 'para' in block:
        return f'<p>{_render_inlines(block["para"])}</p>'

    if 'heading' in block:
        h = block['heading']
        lvl = max(1, min(6, int(h.get('level', 2))))
        return f'<h{lvl}>{_html.escape(h.get("text", ""))}</h{lvl}>'

    if 'codeblock' in block:
        cb = block['codeblock']
        lang = _html.escape(cb.get('lang', '') or '')
        body = _html.escape(cb.get('body', ''))
        cls = f' class="language-{lang}"' if lang else ''
        return f'<pre><code{cls}>{body}</code></pre>'

    if 'table' in block:
        t = block['table']
        cols = t.get('columns', [])
        rows = t.get('rows', [])
        ths = ''.join(f'<th>{_html.escape(str(c))}</th>' for c in cols)
        trs = ''
        for row in rows:
            tds = ''.join(f'<td>{_md_inline(str(cell))}</td>' for cell in row)
            trs += f'<tr>{tds}</tr>'
        return f'<table class="bks"><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>'

    if 'list' in block:
        lst = block['list']
        tag = 'ol' if lst.get('ordered') else 'ul'
        label = lst.get('label', '')
        label_html = f'<strong>{_html.escape(label)}</strong> ' if label else ''
        items_html = ''.join(_render_list_item(i) for i in lst.get('items', []))
        return f'{label_html}<{tag}>{items_html}</{tag}>'

    if 'bullet_list' in block:
        # bullet_list format: [[item1_content], [item2_content]]
        # Each item can contain text strings and/or link dicts
        items_html = ''.join(_render_list_item(i) for i in block.get('bullet_list', []))
        return f'<ul>{items_html}</ul>'

    if 'svg' in block:
        return _render_svg(block['svg'])

    if 'image' in block:
        return _render_image(block['image'])

    if 'details' in block:
        return _render_details(block['details'])

    if 'writable_note' in block:
        return _render_writable_note(block['writable_note'])

    return ''


def _render_writable_note(wn: dict) -> str:
    """Render a writable_note element as textarea + submit button."""
    if not isinstance(wn, dict):
        return ''
    
    notename = _html.escape(wn.get('notename', ''))
    placeholder = _html.escape(wn.get('placeholder', 'Enter text here...'))
    
    # Generate a unique form ID based on notename
    form_id = re.sub(r'[^a-z0-9_-]', '_', wn.get('notename', 'form').lower())
    
    html = f'''<div class="writable-note" id="wn-{form_id}">
<form onsubmit="submitWritableNote(event, '{notename}')">
    <textarea 
        name="text" 
        placeholder="{placeholder}"
        style="width: 100%; font-family: 'Courier New', monospace; font-size: 0.9em; padding: 0.7em; border: 1px solid #bbb; border-radius: 2px; min-height: 200px;"
    ></textarea>
    <div style="margin-top: 0.8em;">
        <button type="submit" style="padding: 0.5em 1.2em; font-size: 0.95em; background: #0066cc; color: white; border: none; border-radius: 2px; cursor: pointer;">Save</button>
        <span id="wn-status-{form_id}" style="margin-left: 1em; font-size: 0.9em; color: #666;"></span>
    </div>
</form>
</div>

<script>
function submitWritableNote(event, notename) {{
    event.preventDefault();
    const form = event.target;
    const textarea = form.querySelector('textarea');
    const text = textarea.value;
    const formId = notename.replace(/[^a-z0-9_-]/gi, '_').toLowerCase();
    const statusSpan = document.getElementById('wn-status-' + formId);
    
    statusSpan.textContent = 'Saving...';
    statusSpan.style.color = '#666';
    
    fetch('/writeback/' + encodeURIComponent(notename), {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{text: text}})
    }})
    .then(r => r.json())
    .then(data => {{
        if (data.status === 'ok') {{
            statusSpan.textContent = 'Saved ✓';
            statusSpan.style.color = '#080';
            textarea.value = '';
            setTimeout(() => {{
                statusSpan.textContent = '';
            }}, 3000);
        }} else {{
            statusSpan.textContent = 'Error: ' + (data.message || 'unknown error');
            statusSpan.style.color = '#c00';
        }}
    }})
    .catch(err => {{
        statusSpan.textContent = 'Error: ' + err.message;
        statusSpan.style.color = '#c00';
    }});
}}
</script>
'''
    return html


def _render_content(content, skip_h1: bool = False) -> str:
    if isinstance(content, str):
        return f'<p>{_md_inline(content)}</p>'
    if isinstance(content, list):
        parts = []
        for b in content:
            if skip_h1 and isinstance(b, dict) and 'heading' in b and b['heading'].get('level') == 1:
                continue
            parts.append(_render_block(b))
        return '\n'.join(parts)
    return ''


def _nav_html(key: str) -> str:
    if not key:
        return '<nav><b>Notes</b></nav>'
    trailing = key.endswith('/')
    parts = key.rstrip('/').split('/')
    crumbs = ['<a href="/notes/">Notes</a>']
    for i, part in enumerate(parts[:-1]):
        ancestor = '/'.join(parts[:i + 1])
        crumbs.append(f'<a href="/notes/{ancestor}">{_html.escape(part)}</a>')
    last = parts[-1] + ('/' if trailing else '')
    crumbs.append(f'<b>{_html.escape(last)}</b>')
    return '<nav>' + ' › '.join(crumbs) + '</nav>'


def _format_meta_value(value) -> str:
    if isinstance(value, list):
        return ', '.join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return ''
    return str(value)


def _meta_html(doc: dict) -> str:
    skip = {'title', 'content', 'runnable'}
    rows = [(k, v) for k, v in doc.items() if k not in skip]
    if not rows:
        return ''
    parts = ' &nbsp;·&nbsp; '.join(
        f'<span class="meta-key">{_html.escape(str(k))}</span> {_html.escape(_format_meta_value(v))}'
        for k, v in rows
    )
    return f'<div class="meta">{parts}</div>'


def _render_page(key: str, doc: dict) -> str:
    title = _html.escape(doc.get('title', key))
    content_html = _render_content(doc.get('content', ''), skip_h1='title' in doc)
    body_parts = [
        _nav_html(key),
        f'<h1>{title}</h1>',
    ]
    if '<details' in content_html:
        # Per-item toggle is genuinely zero-JS (native <details>/<summary>).
        # "Expand all" is not: tried a pure-CSS checkbox + :checked +
        # general-sibling rule first, but confirmed empirically it doesn't
        # work — browsers suppress collapsed <details> content via native
        # rendering suppression tied to the `open` DOM attribute, not an
        # overridable UA stylesheet `display` rule, so no amount of author
        # `!important` reaches it. This one control needs the DOM attribute
        # itself changed, which means a (tiny, inline) script. See
        # JSONHTL_SPEC "details" for the full story.
        body_parts.append(
            '<p class="expand-all-row">'
            '<button type="button" class="expand-all-btn" onclick="'
            "document.querySelectorAll('.note-body details').forEach(d => d.open = true)"
            '">Expand all sections</button>'
            '</p>'
        )
    body_parts.append(f'<div class="note-body">{content_html}</div>')
    body_parts.append(_meta_html(doc))
    body = '\n'.join(body_parts)
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{title} — Notes</title>\n'
        f'<style>\n{_CSS}</style>\n'
        f'</head>\n<body>\n{body}\n</body>\n</html>\n'
    )


def _not_found_page(key: str) -> str:
    ek = _html.escape(key)
    return (
        f'<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f'<title>Not found</title></head><body>'
        f'<p>Note not found: <code>{ek}</code></p>'
        f'<p><a href="/notes/">Notes index</a></p>'
        f'</body></html>'
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def make_router(api_base: str) -> APIRouter:
    """Return an APIRouter that fetches notes from api_base (e.g. http://127.0.0.1:8020)."""

    router = APIRouter()

    async def _fetch(key: str):
        async with httpx.AsyncClient() as client:
            r = await client.get(f'{api_base}/{key}')
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    @router.get('/notes/', response_class=HTMLResponse)
    async def notes_index():
        doc = await _fetch('')
        if doc is None:
            return RedirectResponse('/notes/README')
        return HTMLResponse(_render_page('', doc))

    @router.get('/notes/{key:path}', response_class=HTMLResponse)
    async def notes_view(key: str):
        doc = await _fetch(key)
        if doc is None:
            return HTMLResponse(_not_found_page(key), status_code=404)
        return HTMLResponse(_render_page(key, doc))

    @router.post('/writeback/{notename}')
    async def writeback_post(notename: str, request: Request):
        """Handle POST /writeback/<notename> for writable_note submissions."""
        
        # Validate notename: regex ^[a-zA-Z0-9_-]+$
        if not re.match(r'^[a-zA-Z0-9_\-]+$', notename):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "notename contains invalid characters (allowed: a-z A-Z 0-9 _ -)"
                }
            )
        
        # Parse request body
        try:
            body = await request.json()
            text = body.get('text', '')
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "invalid request body"}
            )
        
        # Generate timestamp and safe filename
        timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        safe_name = notename.replace('/', '__')
        filename = f"{safe_name}_{timestamp}.txt"
        
        # Ensure writeback directory exists
        writeback_dir = '/var/www/webdav/writeback'
        try:
            os.makedirs(writeback_dir, exist_ok=True)
        except OSError as e:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": f"cannot create directory: {e}"}
            )
        
        # Write file
        filepath = os.path.join(writeback_dir, filename)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(text)
        except OSError as e:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": f"cannot write file: {e}"}
            )
        
        # Return success
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "saved": True,
                "notename": notename,
                "timestamp": timestamp,
                "path": f"writeback/{filename}"
            }
        )

    return router
