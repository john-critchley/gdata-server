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
import urllib.parse
from datetime import datetime, timezone

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

import gdata_oauth
from jsonhtl_canon import read_codeblock
from jsonhtl_md import note_to_markdown

# ---------------------------------------------------------------------------
# Mount + auth configuration (per-process; public and private run separately)
# ---------------------------------------------------------------------------
# PREFIX is the URL path the renderer is mounted at and the prefix every emitted
# link uses. Public defaults to /notes; the private store sets NOTES_WEB_PREFIX
# (e.g. /private) so a private page never links back into the public store.
PREFIX = "/" + os.environ.get("NOTES_WEB_PREFIX", "/notes").strip("/")

# Opt-in auth gate. Off for the public store (unauthenticated), on for private.
# When on, every render request must carry a valid token (session cookie or
# Authorization: Bearer); otherwise browsers are 302'd to {PREFIX}/login (which
# Apache guards with WebDAV Basic auth and which mints a token) and non-HTML
# clients get 401. Tokens are the shared gdata_oauth store, minted short-lived.
AUTH_REQUIRED  = os.environ.get("NOTES_WEB_AUTH", "").strip().lower() in ("1", "true", "yes", "on")
SESSION_COOKIE = os.environ.get("NOTES_WEB_SESSION_COOKIE", "notes_session")
SESSION_TTL    = int(os.environ.get("NOTES_WEB_SESSION_TTL", "3600"))  # 1 hour default
SESSION_HOME   = os.environ.get("NOTES_WEB_HOME", "CONTENTS")          # post-login default note

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
    return f'{PREFIX}/{target}'


# Dict-form inline span types → HTML tag. Mirrors the desktop browser's
# INLINE_SPAN_TAGS (notes_browser.py) so {"strong": ...}/{"em": ...}/{"italic": ...}
# render the same in both; web keeps its own <strong>/<em> tag convention.
_INLINE_SPAN_TAGS = {
    'code':   'code',
    'bold':   'strong',
    'strong': 'strong',
    'italic': 'em',
    'em':     'em',
}


def _render_inline(item) -> str:
    if isinstance(item, str):
        return _md_inline(item)
    if isinstance(item, dict):
        if 'link' in item:
            lnk = item['link']
            return f'<a href="{_html.escape(_href(lnk["href"]))}">{_html.escape(lnk.get("text", lnk["href"]))}</a>'
        key = next(iter(item), None)
        tag = _INLINE_SPAN_TAGS.get(key)
        if tag:
            return f'<{tag}>{_html.escape(str(item[key]))}</{tag}>'
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


def _render_cell(cell) -> str:
    """Render a table cell. A cell is a string (markdown inline permitted) or
    a list of inline elements (same content model as para), so links and other
    inline objects render in cells exactly as in a paragraph. A bare inline
    dict is accepted as shorthand for a single-element list. See README/format.
    """
    if isinstance(cell, list):
        return _render_inlines(cell)
    if isinstance(cell, dict):
        return _render_inline(cell)
    if isinstance(cell, str):
        return _md_inline(cell)
    return _md_inline(str(cell))


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


def _render_section(sec, level: int = 2) -> str:
    """Render a nested section container (JSONHTL 'section' block): its title as a
    heading whose level follows nesting depth (overridable via sec['level']),
    then its content recursively one level deeper. Wrapped in <section> so it is
    a real DOM container. See proposals/section-editing."""
    if not isinstance(sec, dict):
        return ''
    lvl = sec.get('level')
    if not isinstance(lvl, int):
        lvl = level
    lvl = max(1, min(6, lvl))
    title = _html.escape(str(sec.get('title', '')))
    head = f'<h{lvl}>{title}</h{lvl}>\n' if title else ''
    inner = _render_content(sec.get('content', []), level=min(lvl + 1, 6))
    return f'<section>{head}{inner}</section>'


def _render_block(block, level: int = 2) -> str:
    if not isinstance(block, dict):
        return ''

    if 'section' in block:
        return _render_section(block['section'], level)

    if 'para' in block:
        return f'<p>{_render_inlines(block["para"])}</p>'

    if 'heading' in block:
        h = block['heading']
        lvl = max(1, min(6, int(h.get('level', 2))))
        return f'<h{lvl}>{_html.escape(h.get("text", ""))}</h{lvl}>'

    if 'codeblock' in block:
        lang_raw, body_raw = read_codeblock(block['codeblock'])
        lang = _html.escape(lang_raw)
        body = _html.escape(body_raw)
        cls = f' class="language-{lang}"' if lang else ''
        return f'<pre><code{cls}>{body}</code></pre>'

    if 'table' in block:
        t = block['table']
        cols = t.get('columns', [])
        rows = t.get('rows', [])
        ths = ''.join(f'<th>{_html.escape(str(c))}</th>' for c in cols)
        sprint_col = next(
            (i for i, col in enumerate(cols)
             if str(col).strip().lower() in ('sprint', 'sprint / queue')),
            None,
        )
        sprint_colours = (
            '#cfe2f3', '#eadcf8', '#fce5cd', '#d0e0e3',
            '#f4cccc', '#ffe08a', '#b7d7ff', '#d9d2e9',
        )
        trs = ''
        for row in rows:
            row_style = ''
            if sprint_col is not None and sprint_col < len(row):
                sprint_match = re.search(
                    r'\bSprint\s+(\d+)\b', str(row[sprint_col]), re.IGNORECASE
                )
                if sprint_match:
                    sprint_number = int(sprint_match.group(1))
                    colour = sprint_colours[sprint_number % len(sprint_colours)]
                    row_style = f' style="background-color: {colour};"'
            tds = ''.join(f'<td>{_render_cell(cell)}</td>' for cell in row)
            trs += f'<tr{row_style}>{tds}</tr>'
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


def _render_content(content, skip_h1: bool = False, level: int = 2) -> str:
    if isinstance(content, str):
        return f'<p>{_md_inline(content)}</p>'
    if isinstance(content, list):
        parts = []
        for b in content:
            if skip_h1 and isinstance(b, dict) and 'heading' in b and b['heading'].get('level') == 1:
                continue
            parts.append(_render_block(b, level))
        return '\n'.join(parts)
    return ''


def _nav_html(key: str) -> str:
    if not key:
        return '<nav><b>Notes</b></nav>'
    trailing = key.endswith('/')
    parts = key.rstrip('/').split('/')
    crumbs = [f'<a href="{PREFIX}/">Notes</a>']
    for i, part in enumerate(parts[:-1]):
        ancestor = '/'.join(parts[:i + 1])
        crumbs.append(f'<a href="{PREFIX}/{ancestor}">{_html.escape(part)}</a>')
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


def _coerce_doc(doc):
    """Best-effort normalise a fetched document to a dict.

    Recovers from the two known corruption shapes (see
    gdata-server/troubleshooting) so a bad note degrades gracefully rather than
    500-ing the renderer:
      • double-encoded — the value is JSON serialised to a string (possibly more
        than once) instead of an object: json.loads until it is no longer a str.
      • list-wrapped — a single-element ops list [{...}]: unwrap to the element.
    Anything that still isn't a dict is returned unchanged for the caller to
    handle (e.g. a malformed-note page)."""
    for _ in range(5):
        if not isinstance(doc, str):
            break
        try:
            doc = json.loads(doc)
        except (ValueError, TypeError):
            break
    if isinstance(doc, list) and len(doc) == 1 and isinstance(doc[0], dict):
        doc = doc[0]
    return doc


def _malformed_page(key: str) -> str:
    ek = _html.escape(key)
    return (
        f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<title>Malformed note</title></head><body>'
        f'<h1>Note could not be rendered</h1>'
        f'<p>The stored document for <code>{ek}</code> is not a valid JSONHTL '
        f'object (it may be double-encoded or otherwise corrupted). '
        f'See <code>gdata-server/troubleshooting</code>.</p>'
        f'<p><a href="{PREFIX}/">Notes index</a></p>'
        f'</body></html>'
    )


def _render_page(key: str, doc: dict) -> str:
    if not isinstance(doc, dict):
        return _malformed_page(key)
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
        f'<p><a href="{PREFIX}/">Notes index</a></p>'
        f'</body></html>'
    )


# ---------------------------------------------------------------------------
# Auth gate (opt-in via NOTES_WEB_AUTH; tokens shared with gdata_oauth store)
# ---------------------------------------------------------------------------

def _authed(request: Request) -> bool:
    """True if the request carries a valid token — Authorization: Bearer for
    scripts/curl, or the session cookie for browsers. Both validate against the
    shared gdata_oauth token store."""
    auth = request.headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        if gdata_oauth.validate_token(auth[7:].strip()):
            return True
    cookie = request.cookies.get(SESSION_COOKIE)
    return bool(cookie) and gdata_oauth.validate_token(cookie)


def _safe_next(nxt: str) -> str:
    """Resolve the post-login redirect target, refusing open redirects: only a
    local path under our own PREFIX is allowed; anything else falls back to the
    configured home note."""
    if nxt:
        dec = urllib.parse.unquote(nxt)
        if (dec.startswith(f'{PREFIX}/') and not dec.startswith('//')
                and '://' not in dec and '\\' not in dec):
            return dec
    return f'{PREFIX}/{SESSION_HOME}'


def _auth_challenge(request: Request, key: str):
    """No valid token: send browsers to the Basic-auth login, others a 401."""
    if negotiate_format(request.headers.get('accept')) == 'html':
        nxt = f'{PREFIX}/{key}' if key else f'{PREFIX}/'
        return RedirectResponse(
            f'{PREFIX}/login?next={urllib.parse.quote(nxt, safe="")}',
            status_code=302,
        )
    return Response(
        content='Unauthorized\n', status_code=401,
        media_type='text/plain; charset=utf-8',
        headers={'WWW-Authenticate': f'Bearer realm="{PREFIX.strip("/")}"'},
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

# --- Content negotiation -------------------------------------------------
# The whole URL tail after /notes/ is the note key, so the response format is
# chosen purely from the Accept header — no ?query or .suffix, which would
# collide with keys that legitimately contain '?', '.', etc. text/plain is an
# alias for Markdown (JSONHTL Markdown is meant to be human-readable too).
_FORMAT_MEDIA = [
    ("html", "text/html"),
    ("json", "application/json"),
    ("json", "application/jsonhtl+json"),
    ("markdown", "text/markdown"),
    ("yaml", "application/yaml"),
    ("yaml", "text/yaml"),
    ("yaml", "application/x-yaml"),
    ("plain", "text/plain"),
]


def _parse_accept(accept: str):
    """Parse an Accept header into (type, subtype, q, order), sorted best-first."""
    items = []
    for order, part in enumerate(accept.split(",")):
        part = part.strip()
        if not part:
            continue
        tokens = part.split(";")
        media = tokens[0].strip().lower()
        q = 1.0
        for tok in tokens[1:]:
            tok = tok.strip()
            if tok.startswith("q="):
                try:
                    q = float(tok[2:])
                except ValueError:
                    q = 0.0
        typ, _, sub = media.partition("/")
        items.append((typ, sub or "*", q, order))
    items.sort(key=lambda it: (-it[2], it[3]))
    return items


def negotiate_format(accept):
    """Pick a response format name from an Accept header. Returns None if the
    client explicitly accepts nothing we can produce (-> 406). Absent/empty
    Accept, or */*, yields 'html' (browser-friendly default)."""
    if not accept or not accept.strip():
        return "html"
    ranges = _parse_accept(accept)
    if not ranges:
        return "html"
    for typ, sub, q, _ in ranges:
        if q <= 0:
            continue
        if typ == "*" and sub == "*":
            return "html"
        for fmt, media in _FORMAT_MEDIA:
            mtyp, _, msub = media.partition("/")
            if typ in (mtyp, "*") and sub in (msub, "*"):
                return fmt
    return None


def _serve_note(key: str, doc, accept):
    """Render a fetched note in the format negotiated from the Accept header."""
    fmt = negotiate_format(accept)
    headers = {"Vary": "Accept"}
    if fmt is None:
        return Response(
            content="Not Acceptable: available types are text/html, "
                    "application/json, application/jsonhtl+json, text/markdown, "
                    "text/plain, application/yaml.\n",
            status_code=406, media_type="text/plain; charset=utf-8", headers=headers,
        )
    # Auto-recover the known corruption shapes so no format 500s on a bad note.
    doc = _coerce_doc(doc)
    if fmt == "json":
        return Response(json.dumps(doc, ensure_ascii=False, indent=2),
                        media_type="application/json", headers=headers)
    if fmt == "yaml":
        return Response(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                        media_type="application/yaml", headers=headers)
    if fmt in ("markdown", "plain"):
        media = "text/markdown" if fmt == "markdown" else "text/plain"
        return Response(note_to_markdown(doc, key),
                        media_type=f"{media}; charset=utf-8", headers=headers)
    if not isinstance(doc, dict):
        # Unrecoverable: render a clean error page, flagged 500 (no traceback).
        return HTMLResponse(_malformed_page(key), status_code=500, headers=headers)
    return HTMLResponse(_render_page(key, doc), headers=headers)


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

    @router.get(f'{PREFIX}/', response_class=HTMLResponse)
    async def notes_index(request: Request):
        if AUTH_REQUIRED and not _authed(request):
            return _auth_challenge(request, '')
        doc = await _fetch('')
        if doc is None:
            return RedirectResponse(f'{PREFIX}/README')
        return _serve_note('', doc, request.headers.get('accept'))

    if AUTH_REQUIRED:
        @router.get(f'{PREFIX}/login')
        async def login(request: Request, next: str = ''):
            # Apache enforces WebDAV Basic auth in front of this path, so merely
            # reaching this handler means the credentials were accepted. Mint a
            # short-lived token in the shared gdata_oauth store and hand it back
            # as an HttpOnly session cookie; the token never appears in a URL.
            token = gdata_oauth.issue_token(ttl=SESSION_TTL)
            resp = RedirectResponse(_safe_next(next), status_code=302)
            resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL,
                            httponly=True, secure=True, samesite='lax', path=PREFIX)
            return resp

        @router.get(f'{PREFIX}/logout')
        async def logout(request: Request):
            cookie = request.cookies.get(SESSION_COOKIE)
            if cookie:
                gdata_oauth.revoke_token(cookie)
            resp = RedirectResponse(f'{PREFIX}/login', status_code=302)
            resp.delete_cookie(SESSION_COOKIE, path=PREFIX)
            return resp

    @router.get(f'{PREFIX}/{{key:path}}', response_class=HTMLResponse)
    async def notes_view(key: str, request: Request):
        if AUTH_REQUIRED and not _authed(request):
            return _auth_challenge(request, key)
        doc = await _fetch(key)
        if doc is None:
            return HTMLResponse(_not_found_page(key), status_code=404)
        return _serve_note(key, doc, request.headers.get('accept'))

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
