#!/usr/bin/env python3
"""
Notes Browser - wxPython hypertext browser for the gdata notes system.

Can read from:
1. Running gdata server via HTTP (NOTES_URL env var or command-line arg)
2. Local GDBM file (--gdbm-file arg)

Features:
- Hypertext navigation: links are styled differently and navigate when clicked
- Markdown and JSON headings rendered as headings
- Metadata fields displayed separately
- Read-only (for now)
- File menu to open any page
"""

import wx
import wx.html
import json
import os
import sys
import argparse
import re
import math
import socket
import threading
import traceback
import datetime
from pathlib import Path
from urllib.parse import quote, unquote

try:
    import yaml
except Exception:
    yaml = None

# Add parent directory to path to import gdata
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gdata
import notes_client
import svg_render


def _load_settings_file(config_path=None):
    """Load settings YAML from explicit path or ~/.notes_browser.yaml."""
    if config_path:
        path = Path(config_path).expanduser()
        source = str(path)
    else:
        path = Path.home() / '.notes_browser.yaml'
        source = str(path)

    if not path.exists():
        return {}, source
    if yaml is None:
        raise RuntimeError("PyYAML is required to read browser settings files")

    with path.open('r', encoding='utf-8') as f:
        loaded = yaml.load(f, Loader=yaml.SafeLoader)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Settings file must contain a YAML mapping: {path}")
    return loaded, source


def _to_bool(value):
    assert value is not None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def load_browser_config(args):
    """Load effective browser config from CLI/env/settings/defaults."""
    settings, settings_source = _load_settings_file(args.config)

    defaults = {
        'notes_url': None,
        'gdbm_file': None,
        'html_base_url': 'http://127.0.0.1/notes',
        'html_path': '/notes',
        'render_font_family': "'Courier New', Courier, monospace",
        'render_font_size_pt': 10,
        'control_tcp_enabled': False,
        'control_udp_enabled': False,
        'control_unix_enabled': False,
        'control_host': '127.0.0.1',
        'control_tcp_port': 8711,
        'control_udp_port': 8711,
        'control_unix_socket': '',
        'control_token': '',
    }

    cfg = dict(defaults)
    for key in cfg.keys():
        if key in settings:
            cfg[key] = settings[key]

    env_map = {
        'notes_url': 'NOTES_URL',
        'gdbm_file': 'GDBM_FILE',
        'html_base_url': 'NOTES_HTML_BASE_URL',
        'html_path': 'NOTES_HTML_PATH',
        'render_font_family': 'NOTES_BROWSER_RENDER_FONT_FAMILY',
        'render_font_size_pt': 'NOTES_BROWSER_RENDER_FONT_SIZE_PT',
        'control_tcp_enabled': 'NOTES_BROWSER_CONTROL_TCP_ENABLED',
        'control_udp_enabled': 'NOTES_BROWSER_CONTROL_UDP_ENABLED',
        'control_unix_enabled': 'NOTES_BROWSER_CONTROL_UNIX_ENABLED',
        'control_host': 'NOTES_BROWSER_CONTROL_HOST',
        'control_tcp_port': 'NOTES_BROWSER_CONTROL_TCP_PORT',
        'control_udp_port': 'NOTES_BROWSER_CONTROL_UDP_PORT',
        'control_unix_socket': 'NOTES_BROWSER_CONTROL_UNIX_SOCKET',
        'control_token': 'NOTES_BROWSER_CONTROL_TOKEN',
    }
    for key, env_name in env_map.items():
        if env_name in os.environ:
            cfg[key] = os.environ[env_name]

    # CLI has highest precedence.
    if args.url is not None:
        cfg['notes_url'] = args.url
    if args.gdbm_file is not None:
        cfg['gdbm_file'] = args.gdbm_file
    if args.html_base_url is not None:
        cfg['html_base_url'] = args.html_base_url
    if args.html_path is not None:
        cfg['html_path'] = args.html_path
    if args.render_font_family is not None:
        cfg['render_font_family'] = args.render_font_family
    if args.render_font_size_pt is not None:
        cfg['render_font_size_pt'] = args.render_font_size_pt
    if args.control_host is not None:
        cfg['control_host'] = args.control_host
    if args.control_tcp_port is not None:
        cfg['control_tcp_port'] = args.control_tcp_port
    if args.control_udp_port is not None:
        cfg['control_udp_port'] = args.control_udp_port
    if args.control_unix_socket is not None:
        cfg['control_unix_socket'] = args.control_unix_socket
    if args.control_token is not None:
        cfg['control_token'] = args.control_token
    if args.control_tcp_enabled is not None:
        cfg['control_tcp_enabled'] = bool(args.control_tcp_enabled)
    if args.control_udp_enabled is not None:
        cfg['control_udp_enabled'] = bool(args.control_udp_enabled)
    if args.control_unix_enabled is not None:
        cfg['control_unix_enabled'] = bool(args.control_unix_enabled)

    # Coerce types.
    cfg['control_tcp_enabled'] = _to_bool(cfg['control_tcp_enabled'])
    cfg['control_udp_enabled'] = _to_bool(cfg['control_udp_enabled'])
    cfg['control_unix_enabled'] = _to_bool(cfg['control_unix_enabled'])
    cfg['control_tcp_port'] = int(cfg['control_tcp_port'])
    cfg['control_udp_port'] = int(cfg['control_udp_port'])
    cfg['render_font_family'] = str(cfg.get('render_font_family') or "'Courier New', Courier, monospace")
    cfg['render_font_size_pt'] = int(cfg.get('render_font_size_pt') or 10)
    if cfg['render_font_size_pt'] <= 0:
        cfg['render_font_size_pt'] = 10
    cfg['control_unix_socket'] = str(cfg.get('control_unix_socket') or '')
    cfg['control_token'] = str(cfg['control_token'] or '')

    # Convenience: providing a Unix socket path enables Unix control unless explicitly disabled.
    if cfg['control_unix_socket'] and args.control_unix_enabled is None:
        cfg['control_unix_enabled'] = True

    # Keep host local by default for safety.
    if not cfg['control_host']:
        cfg['control_host'] = '127.0.0.1'

    # Track where html_base_url came from for status bar clarity.
    html_source = 'fallback'
    if 'html_base_url' in settings:
        html_source = f'settings:{settings_source}'
    if 'NOTES_HTML_BASE_URL' in os.environ:
        html_source = 'env:NOTES_HTML_BASE_URL'
    if args.html_base_url is not None:
        html_source = 'cli:--html-base-url'
    cfg['html_base_url_source'] = html_source

    cfg['settings_source'] = settings_source
    return cfg

class NotesDataSource:
    """Abstraction for reading notes from either HTTP server or GDBM file."""
    
    def __init__(self, url=None, gdbm_file=None):
        """Try HTTP first, fall back to GDBM file if HTTP fails."""
        self.url = url
        self.gdbm_file = gdbm_file
        self.db = None

        self.use_http = False
        # Try HTTP first if URL is provided
        if url:
            try:
                # Try a simple GET to check if server is up
                import requests
                requests.get(url + "/", timeout=3)
                self.use_http = True
            except Exception as e:
                print(f"Warning: HTTP server not available ({e}), trying GDBM file...")
                self.use_http = False
        if not self.use_http and gdbm_file:
            try:
                self.db = gdata.gdata_local(gdbm_file=gdbm_file, mode='r')
            except Exception as e:
                raise RuntimeError(f"Failed to open GDBM file {gdbm_file} in read-only mode: {e}")
        elif not self.use_http:
            raise ValueError("Must provide either a working HTTP server or a gdbm_file")
    
    def read(self, key=""):
        """Read a document by key. Empty string returns root."""
        try:
            if self.use_http:
                import requests as _req
                url = f"{self.url}/{key}" if key else f"{self.url}/"
                r = _req.get(url, timeout=5)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
            elif self.db:
                if key not in self.db:
                    return None
                result = self.db[key]
                # gdata_local.__getitem__ already returns a parsed dict;
                # gdata_local_simple returns a string — handle both.
                if isinstance(result, (str, bytes, bytearray)):
                    return json.loads(result)
                return result
            else:
                return None
        except Exception as e:
            print(f"Error reading {key}: {e}")
            return None
    
    def keys(self):
        """Get list of all keys."""
        try:
            if self.use_http:
                import requests as _req
                r = _req.post(f"{self.url}/", json={"op": "keys"}, timeout=5)
                r.raise_for_status()
                return r.json()['keys']
            elif self.db:
                return list(self.db.keys())
            else:
                return []
        except Exception as e:
            print(f"Error listing keys: {e}")
            return []

    def write(self, key, doc):
        """Write a document by key. Supported when using HTTP backend."""
        if not self.use_http:
            raise RuntimeError("Write is only supported with HTTP data source")
        try:
            import requests as _req
            url = f"{self.url}/{key}" if key else f"{self.url}/"
            r = _req.put(url, data=json.dumps(doc), timeout=8)
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Failed to write {key}: {e}")
    
    def close(self):
        """Close the database connection."""
        if self.db:
            self.db.close()


# JSONHTL inline span types that wrap plain text, mapped to their HTML tag.
# Both the HTML renderer and the plain-text extractor dispatch through this,
# so a new inline type is added in one place.
INLINE_SPAN_TAGS = {
    'code':   'code',
    'bold':   'b',
    'strong': 'b',
    'italic': 'i',
    'em':     'i',
}


def format_meta_value(value):
    """Format a metadata value (e.g. tags) for display. List/tuple values are
    joined as comma-separated text rather than shown as a raw Python repr like
    ['chess', 'board']. Matches the web renderer's metadata formatting."""
    if isinstance(value, (list, tuple)):
        return ', '.join(str(v) for v in value)
    return str(value)


class NotesHTMLRenderer:
    """Convert JSONHTL note data to HTML for display."""

    def __init__(self, font_family="'Courier New', Courier, monospace", font_size_pt=10):
        self.zoom_percent = 100
        self.selection_range = None
        self.use_selection_markup = False
        self.font_family = str(font_family)
        self.font_size_pt = int(font_size_pt) if int(font_size_pt) > 0 else 10

    def _base_text_style(self):
        # Match terminal screenshot typography for OCR parity.
        return f"font-family: {self.font_family}; font-size: {self.font_size_pt}pt; margin: 20px;"
    
    def render(self, key, data):
        """Render note data as HTML."""
        if data is None:
            return self._error_html(f"Document '{key}' not found")
        
        html = [f'<html><body style="{self._base_text_style()}">']

        if isinstance(data, dict):
            has_title = 'title' in data
            if has_title:
                html.append(f'<h1>{self._escape(str(data["title"]))}</h1>')

            if 'content' in data and isinstance(data['content'], list):
                html.append(self._render_jsonhtl_blocks(data['content'], skip_h1=has_title))
            elif 'content' in data and isinstance(data['content'], str):
                html.append(f'<p>{self._escape(data["content"])}</p>')

            meta_pairs = [(k, v) for k, v in data.items() if k not in ('title', 'content', 'runnable')]
            if meta_pairs:
                parts = ' &nbsp;·&nbsp; '.join(
                    f'<b>{self._escape(str(k))}</b> {self._escape(format_meta_value(v))}'
                    for k, v in meta_pairs
                )
                html.append(
                    f'<p style="font-size: 0.8em; color: #888; margin-top: 2em; '
                    f'border-top: 1px solid #ddd; padding-top: 0.5em;">{parts}</p>'
                )
        elif isinstance(data, list):
            html.append(self._render_jsonhtl_blocks(data))
        else:
            html.append(f'<p>{self._escape(str(data))}</p>')
        
        html.append('</body></html>')
        return '\n'.join(html)
    
    def _render_jsonhtl_blocks(self, blocks, skip_h1=False):
        """Render a list of JSONHTL block elements."""
        html = []
        for idx, block in enumerate(blocks):
            if isinstance(block, dict):
                if 'heading' in block:
                    if skip_h1 and isinstance(block['heading'], dict) and block['heading'].get('level') == 1:
                        continue
                    html.append(self._render_heading(block['heading'], idx))
                elif 'para' in block:
                    html.append(self._render_para(block['para'], idx))
                elif 'codeblock' in block:
                    html.append(self._render_codeblock(block['codeblock'], idx))
                elif 'list' in block:
                    html.append(self._render_list(block['list']))
                elif 'table' in block:
                    html.append(self._render_table(block['table']))
                elif 'svg' in block:
                    html.append(svg_render.svg_block_to_html(block['svg'], self._escape))
                elif 'image' in block:
                    html.append(svg_render.image_block_to_html(block['image'], self._escape))
                elif 'details' in block:
                    html.append(self._render_details(block['details']))
                else:
                    # Unknown block types are ignored to match notes.wsgi behavior.
                    continue
            elif isinstance(block, str):
                html.append(f'<p>{self._escape(block)}</p>')
        return '\n'.join(html)

    def _render_heading(self, heading, block_index=None):
        """Render a JSONHTL heading block."""
        if isinstance(heading, dict):
            level = heading.get('level', 2)
            text = heading.get('text', '')
            tag = f'h{min(max(level, 1), 6)}'
            return f'<{tag}>{self._render_text_with_selection(text, block_index)}</{tag}>'
        return f'<h2>{self._render_text_with_selection(str(heading), block_index)}</h2>'
    
    def _render_para(self, para, block_index=None):
        """Render a JSONHTL para block (can contain inline elements)."""
        if isinstance(para, list):
            # Para is a list of inline elements
            content = self._render_inline_list(para)
            return f'<p>{content}</p>'
        elif isinstance(para, str):
            return f'<p>{self._render_text_with_selection(para, block_index, allow_markup=True)}</p>'
        return f'<p>{self._escape(str(para))}</p>'
    
    def _render_inline_list(self, items):
        """Render a list of inline JSONHTL elements."""
        result = []
        for item in items:
            if isinstance(item, str):
                result.append(self._render_markup_text(item))
            elif isinstance(item, dict):
                if 'link' in item:
                    result.append(self._render_link(item['link']))
                elif 'href' in item:
                    # Direct link object
                    result.append(self._render_link(item))
                else:
                    key = next(iter(item), None)
                    tag = INLINE_SPAN_TAGS.get(key)
                    if tag:
                        result.append(f'<{tag}>{self._escape(item[key])}</{tag}>')
                    else:
                        result.append(self._escape(str(item)))
            else:
                result.append(self._escape(str(item)))
        return ''.join(result)
    
    def _render_link(self, link):
        """Render a JSONHTL link inline element."""
        if isinstance(link, dict):
            href = link.get('href', '')
            text = link.get('text', href)
            # External links are blue; internal note links are green.
            if href.startswith('http://') or href.startswith('https://'):
                return (
                    f'<a href="{self._escape(href)}" '
                    f'style="color: #005fcc;" '
                    f'title="External web link">{self._escape(text)}</a>'
                )
            else:
                return (
                    f'<a href="navigate://{quote(href)}" '
                    f'style="color: #1f7a1f;" '
                    f'title="Internal note link">{self._escape(text)}</a>'
                )
        return f'<a href="navigate://{quote(str(link))}">{self._escape(str(link))}</a>'
    
    def _render_codeblock(self, codeblock, block_index=None):
        """Render a JSONHTL codeblock."""
        if isinstance(codeblock, dict):
            # JSONHTL spec uses 'body' and 'lang'
            body = codeblock.get('body', codeblock.get('text', ''))
            lang = codeblock.get('lang', '')
            cls = f' class="language-{self._escape(lang)}"' if lang else ''
            body_html = self._render_text_with_selection(body, block_index)
            return (
                f'<pre style="background: #f4f4f4; padding: 10px; overflow-x: auto;">'
                f'<code{cls}>{body_html}</code></pre>'
            )
        return (
            f'<pre style="background: #f4f4f4; padding: 10px;"><code>'
            f'{self._render_text_with_selection(str(codeblock), block_index)}</code></pre>'
        )

    def _render_text_with_selection(self, text, block_index, allow_markup=False):
        """Render text, optionally with highlighted selection for the given block."""
        if text is None:
            text = ''
        text = str(text)
        sel = self.selection_range
        if not sel or block_index is None:
            return self._render_markup_text(text) if allow_markup else self._escape(text)

        bs = int(sel.get('block_start', -1))
        be = int(sel.get('block_end', -1))
        if block_index < bs or block_index > be:
            return self._render_markup_text(text) if allow_markup else self._escape(text)

        if not self.use_selection_markup:
            return self._render_markup_text(text) if allow_markup else self._escape(text)

        start = 0
        end = len(text)
        if block_index == bs:
            start = int(sel.get('offset_start', 0))
        if block_index == be:
            end = int(sel.get('offset_end', len(text)))

        if start < 0 or end < 0 or start > end or start > len(text) or end > len(text):
            return self._render_markup_text(text) if allow_markup else self._escape(text)

        before = self._escape(text[:start])
        selected = self._escape(text[start:end])
        after = self._escape(text[end:])
        return (
            before
            + '<font color="#ffffff" bgcolor="#2f3f56"><b>'
            + selected
            + '</b></font>'
            + after
        )

    def _render_list(self, lst):
        """Render a JSONHTL list block."""
        if not isinstance(lst, dict):
            return f'<ul><li>{self._escape(str(lst))}</li></ul>'
        label = lst.get('label', '')
        tag = 'ol' if lst.get('ordered') else 'ul'
        rows = []
        if label:
            rows.append(f'<p><b>{self._escape(str(label))}</b></p>')
        rows.append(f'<{tag}>')
        for item in lst.get('items', []):
            if isinstance(item, str):
                rows.append(f'<li>{self._render_markup_text(item)}</li>')
            elif isinstance(item, list):
                rows.append(f'<li>{self._render_inline_list(item)}</li>')
            elif isinstance(item, dict):
                if 'para' in item:
                    para = item['para']
                    if isinstance(para, list):
                        rows.append(f'<li>{self._render_inline_list(para)}</li>')
                    else:
                        rows.append(f'<li>{self._render_markup_text(str(para))}</li>')
                elif any(k in item for k in ('link', 'code', 'bold', 'href')):
                    rows.append(f'<li>{self._render_inline_list([item])}</li>')
                else:
                    pairs = ', '.join(
                        f'<b>{self._escape(str(k))}:</b> {self._escape(str(v))}'
                        for k, v in item.items()
                    )
                    rows.append(f'<li>{pairs}</li>')
            else:
                rows.append(f'<li>{self._escape(str(item))}</li>')
        rows.append(f'</{tag}>')
        return '\n'.join(rows)

    def _render_details(self, details_dict):
        """Render a details block.

        Known gap: wx.html.HtmlWindow has no <details>/<summary> support
        at all (confirmed empirically — it just dumps the "hidden" content
        as permanently visible text with no collapse behaviour). A real
        fix needs a native wx.CollapsiblePane, which needs this renderer's
        one-HTML-blob-per-page architecture to become a sizer of mixed
        widgets — out of scope here, logged in notes-browser/todo. Degrades
        to always-expanded, in a bordered box so it's still visually
        distinguished from surrounding content.
        """
        if not isinstance(details_dict, dict):
            return ''
        summary = self._escape(str(details_dict.get('summary', '') or ''))
        nested = self._render_jsonhtl_blocks(details_dict.get('content', []) or [])
        return (
            '<table width="100%" style="border: 1px solid #ccc; margin: 8px 0;" cellpadding="0" cellspacing="0">'
            f'<tr><td bgcolor="#eeeeee" style="padding: 6px 10px;"><b>{summary}</b></td></tr>'
            f'<tr><td style="padding: 6px 10px;">{nested}</td></tr>'
            '</table>'
        )

    def _render_table(self, table):
        """Render a JSONHTL table block as an HTML table."""
        if not isinstance(table, dict):
            return f'<p>{self._escape(str(table))}</p>'
        columns = table.get('columns', [])
        rows = table.get('rows', [])
        html = [
            f'<table style="border-collapse: collapse; width: 100%; margin: 8px 0; '
            f'font-family: {self.font_family}; font-size: {self.font_size_pt}pt;">'
        ]
        # Header row — dark background, white text (matches .bks th style)
        if columns:
            html.append('<tr>')
            for col in columns:
                html.append(
                    f'<th bgcolor="#222222" style="border: 1px solid #ddd; padding: 8px; text-align: left;">'
                    f'<font color="#ffffff"><b>{self._escape(str(col))}</b></font></th>'
                )
            html.append('</tr>')
        # Data rows — highlight sprint rows consistently without requiring
        # presentation metadata in JSONHTL.  Fall back to alternating stripes.
        sprint_col = None
        for col_index, col in enumerate(columns):
            if str(col).strip().lower() in ('sprint', 'sprint / queue'):
                sprint_col = col_index
                break
        sprint_colours = (
            '#cfe2f3', '#eadcf8', '#fce5cd', '#d0e0e3',
            '#f4cccc', '#ffe08a', '#b7d7ff', '#d9d2e9',
        )
        for i, row in enumerate(rows):
            row_colour = None
            cells = row if isinstance(row, list) else [row]
            if sprint_col is not None and sprint_col < len(cells):
                sprint_match = re.search(
                    r'\bSprint\s+(\d+)\b', str(cells[sprint_col]), re.IGNORECASE
                )
                if sprint_match:
                    sprint_number = int(sprint_match.group(1))
                    row_colour = sprint_colours[sprint_number % len(sprint_colours)]
            if row_colour is None and i % 2 == 1:
                row_colour = '#f9f9f9'
            bg_attr = f' bgcolor="{row_colour}"' if row_colour else ''
            html.append('<tr>')
            for cell in cells:
                html.append(
                    f'<td{bg_attr} style="border: 1px solid #ddd; padding: 8px;">'
                    f'{self._render_markup_text(str(cell))}</td>'
                )
            html.append('</tr>')
        html.append('</table>')
        return '\n'.join(html)

    def _escape(self, text):
        """Escape HTML special characters."""
        return (text
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;'))
    
    def _render_markup_text(self, text):
        """Render a plain text string, interpreting markdown-style inline markup.
        
        Supports:
          **bold**  -> <b>bold</b>
          `code`    -> <code>code</code>
          *italic*  -> <i>italic</i>
        
        Markup can be nested within a string alongside JSONHTL structured
        elements.  The text is HTML-escaped first, then markup patterns are
        applied, so markup inside code spans is safe.
        """
        # First escape the whole string
        escaped = self._escape(text)
        # Apply markup patterns (order matters: ** before *)
        # `code` — backtick code spans
        escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped)
        # **bold**
        escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped)
        # *italic* (but not inside an already-matched **)
        escaped = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', escaped)
        escaped = escaped.replace('\n', '<br>')
        return escaped

    def _error_html(self, message):
        """Render an error message."""
        return f'''<html><body style="{self._base_text_style()}">
<h2 style="color: red;">Error</h2>
<p>{self._escape(message)}</p>
</body></html>'''


class NotesHtmlWindow(wx.html.HtmlWindow):
    """Custom HtmlWindow that handles link clicks for navigation."""
    
    def __init__(self, parent, browser):
        wx.html.HtmlWindow.__init__(self, parent)
        self.browser = browser
    
    def OnLinkClicked(self, link):
        """Handle link clicks - navigate:// links go to pages, others open externally."""
        url = link.GetHref()
        
        if url.startswith("navigate://"):
            # Internal navigation
            page_key = unquote(url[len("navigate://"):])
            self.browser._navigate_to(page_key)
        elif url.startswith("http://") or url.startswith("https://"):
            # External link - open in system browser
            import webbrowser
            webbrowser.open(url)
        else:
            # Treat as internal page key
            self.browser._navigate_to(url)


class ControlSettingsDialog(wx.Dialog):
    """Dialog for configuring the control listening interface."""

    def __init__(self, parent, config):
        super().__init__(parent, title="Control Interface Settings",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._build(config)
        self.Fit()
        self.CenterOnParent()

    def _section_box(self, outer, label):
        box = wx.StaticBox(outer, label=label)
        sizer = wx.StaticBoxSizer(box, wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        grid.AddGrowableCol(1)
        return sizer, grid

    def _build(self, cfg):
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- TCP section ---
        tcp_sizer, tcp_grid = self._section_box(self, "TCP")
        self._tcp_enabled = wx.CheckBox(self, label="Enabled")
        self._tcp_enabled.SetValue(bool(cfg.get('control_tcp_enabled', False)))
        tcp_sizer.Add(self._tcp_enabled, 0, wx.ALL, 4)

        tcp_grid.Add(wx.StaticText(self, label="Host:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._tcp_host = wx.TextCtrl(self, value=str(cfg.get('control_host', '127.0.0.1')))
        tcp_grid.Add(self._tcp_host, 1, wx.EXPAND)

        tcp_grid.Add(wx.StaticText(self, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._tcp_port = wx.SpinCtrl(self, min=1, max=65535,
                                     value=str(cfg.get('control_tcp_port', 8711)))
        tcp_grid.Add(self._tcp_port, 1, wx.EXPAND)
        tcp_sizer.Add(tcp_grid, 0, wx.EXPAND | wx.ALL, 4)
        outer.Add(tcp_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # --- UDP section ---
        udp_sizer, udp_grid = self._section_box(self, "UDP")
        self._udp_enabled = wx.CheckBox(self, label="Enabled")
        self._udp_enabled.SetValue(bool(cfg.get('control_udp_enabled', False)))
        udp_sizer.Add(self._udp_enabled, 0, wx.ALL, 4)

        udp_grid.Add(wx.StaticText(self, label="Host:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._udp_host = wx.TextCtrl(self, value=str(cfg.get('control_host', '127.0.0.1')))
        udp_grid.Add(self._udp_host, 1, wx.EXPAND)

        udp_grid.Add(wx.StaticText(self, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._udp_port = wx.SpinCtrl(self, min=1, max=65535,
                                     value=str(cfg.get('control_udp_port', 8711)))
        udp_grid.Add(self._udp_port, 1, wx.EXPAND)
        udp_sizer.Add(udp_grid, 0, wx.EXPAND | wx.ALL, 4)
        outer.Add(udp_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # --- Unix socket section ---
        unix_sizer, unix_grid = self._section_box(self, "Unix Socket")
        self._unix_enabled = wx.CheckBox(self, label="Enabled")
        self._unix_enabled.SetValue(bool(cfg.get('control_unix_enabled', False)))
        unix_sizer.Add(self._unix_enabled, 0, wx.ALL, 4)

        unix_grid.Add(wx.StaticText(self, label="Socket path:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._unix_socket = wx.TextCtrl(self, value=str(cfg.get('control_unix_socket', '')))
        unix_grid.Add(self._unix_socket, 1, wx.EXPAND)
        unix_sizer.Add(unix_grid, 0, wx.EXPAND | wx.ALL, 4)
        outer.Add(unix_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # --- Shared token ---
        token_sizer, token_grid = self._section_box(self, "Authentication")
        token_grid.Add(wx.StaticText(self, label="Token:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._token = wx.TextCtrl(self, value=str(cfg.get('control_token', '')))
        token_grid.Add(self._token, 1, wx.EXPAND)
        token_sizer.Add(token_grid, 0, wx.EXPAND | wx.ALL, 4)
        outer.Add(token_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # --- Buttons ---
        btn_sizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        outer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(outer)

    def get_values(self):
        return {
            'control_tcp_enabled': self._tcp_enabled.GetValue(),
            'control_udp_enabled': self._udp_enabled.GetValue(),
            'control_unix_enabled': self._unix_enabled.GetValue(),
            'control_host': self._tcp_host.GetValue().strip() or '127.0.0.1',
            'control_tcp_port': self._tcp_port.GetValue(),
            'control_udp_port': self._udp_port.GetValue(),
            'control_unix_socket': self._unix_socket.GetValue().strip(),
            'control_token': self._token.GetValue(),
        }


class NotesBrowser(wx.Frame):
    """Main browser window."""

    ID_COPY_SELECTED = wx.NewIdRef()
    ID_COPY_JSON = wx.NewIdRef()
    ID_COPY_LINK = wx.NewIdRef()
    ID_COPY_LINK_URL = wx.NewIdRef()
    ID_ZOOM_IN = wx.NewIdRef()
    ID_ZOOM_OUT = wx.NewIdRef()
    ID_ZOOM_RESET = wx.NewIdRef()
    ID_PREFERENCES = wx.NewIdRef()

    BASE_HTML_FONT = 10

    def __init__(self, data_source, config):
        """Initialize the browser."""
        wx.Frame.__init__(self, None, title="Notes Browser", size=(900, 600))
        
        # Set application icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notes_browser.png')
        if os.path.exists(icon_path):
            icon = wx.Icon(icon_path, wx.BITMAP_TYPE_PNG)
            self.SetIcon(icon)
        
        self.data_source = data_source
        self.config = config
        self.renderer = NotesHTMLRenderer(
            font_family=self.config.get('render_font_family', "'Courier New', Courier, monospace"),
            font_size_pt=self.config.get('render_font_size_pt', 10),
        )
        self.history = []
        self.history_position = -1  # Start before any history
        self.current_page = None  # None so first navigation adds to history
        self.selection_range = None
        self.last_selection_mode = 'none'
        self._page_meta_text = ''
        self.control_server = None
        
        # Detect background brightness for text colour choices
        self._bg_is_dark = None  # resolved lazily after UI is created
        
        self._create_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._navigate_to("")  # Start at root
        self._update_base_url_status()
        self._start_control_server()
    
    def _create_ui(self):
        """Create the user interface."""
        # Menu bar
        menubar = wx.MenuBar()
        
        # File menu
        file_menu = wx.Menu()
        open_item = file_menu.Append(wx.ID_OPEN, "&Open Page\tCtrl+O", "Open any page")
        file_menu.AppendSeparator()
        exit_item = file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl+Q", "Exit application")
        menubar.Append(file_menu, "&File")

        # Edit menu
        edit_menu = wx.Menu()
        copy_item = edit_menu.Append(self.ID_COPY_SELECTED, "&Copy Selected\tCtrl+C", "Copy selected text")
        copy_json_item = edit_menu.Append(self.ID_COPY_JSON, "Copy Page &JSON\tCtrl+Shift+C", "Copy current page JSON")
        copy_link_item = edit_menu.Append(self.ID_COPY_LINK, "Copy Lin&k\tCtrl+L", "Copy current page key")
        copy_link_url_item = edit_menu.Append(self.ID_COPY_LINK_URL, "Copy Link As &URL\tCtrl+Shift+L", "Copy HTML renderer URL")
        menubar.Append(edit_menu, "&Edit")
        
        # Navigate menu (renamed from Edit - more appropriate)
        nav_menu = wx.Menu()
        back_item = nav_menu.Append(wx.ID_BACKWARD, "&Back\tAlt+Left", "Go back")
        forward_item = nav_menu.Append(wx.ID_FORWARD, "&Forward\tAlt+Right", "Go forward")
        nav_menu.AppendSeparator()
        home_item = nav_menu.Append(wx.ID_HOME, "&Home\tCtrl+H", "Go to root page")
        self.refresh_menu_item = nav_menu.Append(wx.ID_REFRESH, "&Refresh\tF5", "Refresh page")
        menubar.Append(nav_menu, "&Navigate")

        # View menu
        view_menu = wx.Menu()
        zoom_in_item = view_menu.Append(self.ID_ZOOM_IN, "Zoom &In\tCtrl+=", "Increase content size")
        zoom_out_item = view_menu.Append(self.ID_ZOOM_OUT, "Zoom &Out\tCtrl+-", "Decrease content size")
        zoom_reset_item = view_menu.Append(self.ID_ZOOM_RESET, "Zoom &Reset\tCtrl+0", "Reset content size")
        menubar.Append(view_menu, "&View")

        # Settings menu
        settings_menu = wx.Menu()
        prefs_item = settings_menu.Append(self.ID_PREFERENCES, "&Control Interface…", "Configure control listening interface")
        menubar.Append(settings_menu, "&Settings")

        self.SetMenuBar(menubar)

        # Bind menu events
        self.Bind(wx.EVT_MENU, self._on_open, open_item)
        self.Bind(wx.EVT_MENU, self._on_exit, exit_item)
        self.Bind(wx.EVT_MENU, self._on_back, back_item)
        self.Bind(wx.EVT_MENU, self._on_forward, forward_item)
        self.Bind(wx.EVT_MENU, self._on_home, home_item)
        self.Bind(wx.EVT_MENU, self._on_refresh, self.refresh_menu_item)
        self.Bind(wx.EVT_MENU, self._on_copy_selected, copy_item)
        self.Bind(wx.EVT_MENU, self._on_copy_page_json, copy_json_item)
        self.Bind(wx.EVT_MENU, self._on_copy_link, copy_link_item)
        self.Bind(wx.EVT_MENU, self._on_copy_link_as_url, copy_link_url_item)
        self.Bind(wx.EVT_MENU, self._on_zoom_in, zoom_in_item)
        self.Bind(wx.EVT_MENU, self._on_zoom_out, zoom_out_item)
        self.Bind(wx.EVT_MENU, self._on_zoom_reset, zoom_reset_item)
        self.Bind(wx.EVT_MENU, self._on_control_settings, prefs_item)
        
        # Set up accelerator table for keys that don't work well as menu shortcuts
        accel_entries = [
            (wx.ACCEL_ALT, wx.WXK_LEFT, wx.ID_BACKWARD),
            (wx.ACCEL_ALT, wx.WXK_RIGHT, wx.ID_FORWARD),
            (wx.ACCEL_NORMAL, wx.WXK_F5, wx.ID_REFRESH),
            (wx.ACCEL_CTRL, ord('O'), wx.ID_OPEN),
            (wx.ACCEL_CTRL, ord('Q'), wx.ID_EXIT),
            (wx.ACCEL_CTRL, ord('H'), wx.ID_HOME),
            (wx.ACCEL_CTRL, ord('C'), int(self.ID_COPY_SELECTED)),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('C'), int(self.ID_COPY_JSON)),
            (wx.ACCEL_CTRL, ord('L'), int(self.ID_COPY_LINK)),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('L'), int(self.ID_COPY_LINK_URL)),
            (wx.ACCEL_CTRL, ord('='), int(self.ID_ZOOM_IN)),
            (wx.ACCEL_CTRL, ord('-'), int(self.ID_ZOOM_OUT)),
            (wx.ACCEL_CTRL, ord('0'), int(self.ID_ZOOM_RESET)),
        ]
        accel_table = wx.AcceleratorTable(accel_entries)
        self.SetAcceleratorTable(accel_table)
        
        # Main panel
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Navigation bar
        nav_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.back_btn = wx.Button(panel, label="< Back")
        self.forward_btn = wx.Button(panel, label="Forward >")
        self.home_btn = wx.Button(panel, label="Home")
        self.refresh_btn = wx.Button(panel, label="Refresh")
        self.page_text = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        
        nav_sizer.Add(self.back_btn, 0, wx.ALL, 5)
        nav_sizer.Add(self.forward_btn, 0, wx.ALL, 5)
        nav_sizer.Add(self.home_btn, 0, wx.ALL, 5)
        nav_sizer.Add(self.refresh_btn, 0, wx.ALL, 5)
        nav_sizer.Add(wx.StaticText(panel, label="Page:"), 0, wx.ALL, 8)
        nav_sizer.Add(self.page_text, 1, wx.ALL | wx.EXPAND, 5)
        
        self.back_btn.Bind(wx.EVT_BUTTON, self._on_back)
        self.forward_btn.Bind(wx.EVT_BUTTON, self._on_forward)
        self.home_btn.Bind(wx.EVT_BUTTON, self._on_home)
        self.refresh_btn.Bind(wx.EVT_BUTTON, self._on_refresh)
        self.page_text.Bind(wx.EVT_TEXT_ENTER, self._on_page_text_enter)
        self.page_text.Bind(wx.EVT_SET_FOCUS, self._on_page_text_focus)
        self.page_text.Bind(wx.EVT_KILL_FOCUS, self._on_page_text_blur)
        
        sizer.Add(nav_sizer, 0, wx.EXPAND)

        # Selection indicator (for RPC-driven selection visibility)
        self.selection_info = wx.StaticText(panel, label="Selection: (none)")
        sizer.Add(self.selection_info, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
        
        # HTML viewer - use HtmlWindow (always available, unlike WebView)
        self.html = NotesHtmlWindow(panel, self)
        self.html.Bind(wx.EVT_MOUSEWHEEL, self._on_html_mousewheel)
        
        sizer.Add(self.html, 1, wx.EXPAND)
        
        panel.SetSizer(sizer)

        self.CreateStatusBar(2)
        self.SetStatusWidths([-2, -3])
        self.SetStatusText("Ready", 0)

    def _update_selection_indicator(self):
        """Show current selection range and text in the UI."""
        if not self.selection_range:
            native_text = ''
            if hasattr(self.html, 'SelectionToText'):
                try:
                    native_text = self.html.SelectionToText().strip('\n')
                except Exception:
                    native_text = ''
            if native_text and str(self.last_selection_mode).startswith('native'):
                self.selection_info.SetLabel(
                    f'Selection {self.last_selection_mode} | "{native_text}"'
                )
                self._flush_ui_updates()
                return
            label = self._page_meta_text if self._page_meta_text else 'Selection: (none)'
            self.selection_info.SetLabel(label)
            self._flush_ui_updates()
            return

        sel = self.selection_range
        text = self._read_selection_text().replace('\n', ' ')
        if len(text) > 100:
            text = text[:97] + '...'
        self.selection_info.SetLabel(
            f"Selection b{sel['block_start']}:{sel['offset_start']} - "
            f"b{sel['block_end']}:{sel['offset_end']} | \"{text}\""
        )
        self._flush_ui_updates()

    def _flush_ui_updates(self):
        """Apply layout + paint updates so screenshots reflect latest state."""
        try:
            self.Layout()
            self.Refresh()
            self.Update()
            wx.YieldIfNeeded()
        except Exception:
            pass

    def _capture_window_bitmap(self):
        """Capture current frame client area into a wx.Bitmap."""
        try:
            if self.IsIconized():
                self.Iconize(False)
            self.Show(True)
            self.Raise()
        except Exception:
            pass

        self._flush_ui_updates()

        w, h = self.GetClientSize()
        dc_factory = wx.ClientDC
        if w <= 0 or h <= 0:
            # Some X11/window-manager states report a zero client area even
            # when the frame has a real size. Fall back to the full window DC
            # so control-socket screenshots still work for automation.
            w, h = self.GetSize()
            dc_factory = wx.WindowDC
        if w <= 0 or h <= 0:
            raise RuntimeError('Window has invalid size for screenshot')

        bitmap = wx.Bitmap(w, h)
        mem = wx.MemoryDC(bitmap)
        try:
            mem.Blit(0, 0, w, h, dc_factory(self), 0, 0)
        finally:
            mem.SelectObject(wx.NullBitmap)
        return bitmap

    def _sixel_rle_encode(self, chars):
        """Run-length encode SIXEL data chars."""
        if not chars:
            return ''
        out = []
        run_char = chars[0]
        run_count = 1
        for ch in chars[1:]:
            if ch == run_char:
                run_count += 1
                continue

            if run_count >= 4:
                out.append(f'!{run_count}{run_char}')
            else:
                out.append(run_char * run_count)
            run_char = ch
            run_count = 1

        if run_count >= 4:
            out.append(f'!{run_count}{run_char}')
        else:
            out.append(run_char * run_count)
        return ''.join(out)

    def _bitmap_to_sixel_body(self, bitmap):
        """Convert bitmap to SIXEL body text without DCS/ST control wrappers."""
        image = bitmap.ConvertToImage()
        w = int(image.GetWidth())
        h = int(image.GetHeight())
        raw = image.GetData()

        # Quantize to 6x6x6 cube (216 colors) for predictable SIXEL palette use.
        qpix = bytearray(w * h)
        used = [False] * 216
        i = 0
        px = 0
        raw_len = len(raw)
        while i + 2 < raw_len:
            r = raw[i]
            g = raw[i + 1]
            b = raw[i + 2]
            qr = (r * 5 + 127) // 255
            qg = (g * 5 + 127) // 255
            qb = (b * 5 + 127) // 255
            idx = qr * 36 + qg * 6 + qb
            qpix[px] = idx
            used[idx] = True
            i += 3
            px += 1

        used_indices = [idx for idx, flag in enumerate(used) if flag]
        parts = [f'"1;1;{w};{h}']

        # Define only actually-used palette entries.
        for idx in used_indices:
            qr = idx // 36
            qg = (idx % 36) // 6
            qb = idx % 6
            pr = int(round(qr * 100.0 / 5.0))
            pg = int(round(qg * 100.0 / 5.0))
            pb = int(round(qb * 100.0 / 5.0))
            parts.append(f'#{idx};2;{pr};{pg};{pb}')

        for y0 in range(0, h, 6):
            band_indices = []
            seen = set()
            y_max = min(h, y0 + 6)
            for yy in range(y0, y_max):
                row_base = yy * w
                for x in range(w):
                    idx = qpix[row_base + x]
                    if idx not in seen:
                        seen.add(idx)
                        band_indices.append(idx)

            for idx in band_indices:
                chars = []
                for x in range(w):
                    bits = 0
                    for bit in range(6):
                        yy = y0 + bit
                        if yy >= h:
                            break
                        if qpix[yy * w + x] == idx:
                            bits |= (1 << bit)
                    chars.append(chr(63 + bits))
                parts.append(f'#{idx}{self._sixel_rle_encode(chars)}$')

            parts.append('-')

        if parts and parts[-1] == '-':
            parts.pop()
        return ''.join(parts), w, h, len(used_indices)

    def _capture_window_screenshot(self, out_path=None):
        """Capture the current frame as PNG and return path."""
        assert out_path is None or isinstance(out_path, str)
        root_str = self.config.get('project_root') or os.environ.get('PWD') or str(Path.cwd())
        project_root = Path(root_str)
        default_shots_dir = project_root / 'notes-browser' / 'screenshots'

        if out_path:
            target = Path(out_path)
            if not target.is_absolute():
                target = project_root / target
        else:
            shots_dir = default_shots_dir
            shots_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            target = shots_dir / f'notes-browser-{stamp}.png'

        target.parent.mkdir(parents=True, exist_ok=True)
        bitmap = self._capture_window_bitmap()

        if not bitmap.SaveFile(str(target), wx.BITMAP_TYPE_PNG):
            raise RuntimeError(f'Failed to write screenshot: {target}')
        self._set_status(f'Screenshot saved: {target}')
        return str(target)

    def _resolve_output_path(self, out_path, default_name):
        """Resolve optional output path against project root."""
        assert out_path is None or isinstance(out_path, str)
        root_str = self.config.get('project_root') or os.environ.get('PWD') or str(Path.cwd())
        project_root = Path(root_str)
        default_shots_dir = project_root / 'notes-browser' / 'screenshots'

        if out_path:
            target = Path(out_path)
            if not target.is_absolute():
                target = project_root / target
        else:
            default_shots_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            target = default_shots_dir / f'{default_name}-{stamp}.txt'

        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _capture_window_sixel(self, out_path=None, include_data=True):
        """Capture current frame to SIXEL body text (no control wrappers)."""
        bitmap = self._capture_window_bitmap()
        sixel, w, h, colors = self._bitmap_to_sixel_body(bitmap)

        saved_path = None
        if out_path is not None:
            target = self._resolve_output_path(out_path, 'notes-browser-sixel')
            target.write_text(sixel, encoding='utf-8')
            saved_path = str(target)

        result = {
            'format': 'sixel-body',
            'width': w,
            'height': h,
            'colors': colors,
        }
        if saved_path:
            result['path'] = saved_path
            self._set_status(f'SIXEL saved: {saved_path}')
        if include_data:
            result['sixel'] = sixel
        return result

    def _iter_word_cells(self):
        """Yield (text, x, y, w, h) for rendered HtmlWordCell entries."""
        root = self.html.GetInternalRepresentation()
        if root is None:
            return

        def walk(cell, ax=0, ay=0):
            if not cell:
                return
            x = ax + cell.GetPosX()
            y = ay + cell.GetPosY()
            if isinstance(cell, wx.html.HtmlWordCell):
                yield (cell.ConvertToText(None), x, y, cell.GetWidth(), cell.GetHeight())
            child = cell.GetFirstChild() if hasattr(cell, 'GetFirstChild') else None
            while child:
                for item in walk(child, x, y):
                    yield item
                child = child.GetNext() if hasattr(child, 'GetNext') else None

        for item in walk(root):
            yield item

    def _find_word_selection_range(self, target, occurrence=1):
        """Map a rendered word back to block offsets in current JSONHTL content."""
        token = str(target or '').strip()
        if not token:
            return None

        occ = max(1, int(occurrence))
        seen = 0
        blocks = self._get_content_blocks()
        for block_index, block in enumerate(blocks):
            text = self._block_text(block)
            if not text:
                continue
            pos = 0
            while True:
                idx = text.find(token, pos)
                if idx < 0:
                    break

                before = text[idx - 1] if idx > 0 else ''
                after_idx = idx + len(token)
                after = text[after_idx] if after_idx < len(text) else ''

                before_is_word = before.isalnum() or before == '_'
                after_is_word = after.isalnum() or after == '_'
                if not before_is_word and not after_is_word:
                    seen += 1
                    if seen == occ:
                        return {
                            'block_start': block_index,
                            'offset_start': idx,
                            'block_end': block_index,
                            'offset_end': idx + len(token),
                        }

                pos = idx + 1

        return None

    def _apply_native_word_selection(self):
        """Apply native HtmlWindow word selection for current range when possible."""
        self.last_selection_mode = 'logical-only'
        sel = self.selection_range
        if not sel:
            return False
        if sel['block_start'] != sel['block_end']:
            return False

        blocks = self._get_content_blocks()
        idx = sel['block_start']
        if idx < 0 or idx >= len(blocks):
            return False

        block_text = self._block_text(blocks[idx])
        start = sel['offset_start']
        end = sel['offset_end']
        if start < 0 or end < 0 or start >= end or end > len(block_text):
            return False

        selected = block_text[start:end]
        target = selected.strip()
        if not target or any(ch.isspace() for ch in target):
            return False

        for word_text, x, y, w, h in self._iter_word_cells():
            if word_text.strip() == target:
                # Native selection path used by manual interaction.
                self.html.SelectWord(wx.Point(x + max(1, min(3, w - 1)), y + max(1, min(3, h - 1))))
                self._flush_ui_updates()
                self.last_selection_mode = 'native-word'
                return True

        return False

    def _apply_native_word_selection_by_text(self, text, occurrence=1):
        """Select a visible word using HtmlWindow native selection mechanism."""
        self.selection_range = None
        self.last_selection_mode = 'none'
        self._navigate_to(self._current_page_key(), add_to_history=False)

        target = str(text or '').strip()
        if not target:
            return False
        occ = max(1, int(occurrence))
        seen = 0
        for word_text, x, y, w, h in self._iter_word_cells():
            if word_text.strip() == target:
                seen += 1
                if seen == occ:
                    self.html.SelectWord(wx.Point(x + max(1, min(3, w - 1)), y + max(1, min(3, h - 1))))
                    self._flush_ui_updates()
                    self.selection_range = self._find_word_selection_range(target, occurrence=occ)
                    self.last_selection_mode = 'native-word'
                    return True
        return False

    def _apply_native_line_selection_by_text(self, text, occurrence=1):
        """Select a visible line using HtmlWindow native line-selection mechanism."""
        self.selection_range = None
        self.last_selection_mode = 'none'
        self._navigate_to(self._current_page_key(), add_to_history=False)

        target = str(text or '').strip()
        if not target:
            return False
        occ = max(1, int(occurrence))
        seen = 0
        word_seen = {}
        for word_text, x, y, w, h in self._iter_word_cells():
            token = word_text.strip()
            if token:
                word_seen[token] = word_seen.get(token, 0) + 1
            if token and (target in token or token in target):
                seen += 1
                if seen == occ:
                    self.html.SelectLine(wx.Point(x + max(1, min(3, w - 1)), y + max(1, min(3, h - 1))))
                    self._flush_ui_updates()
                    self.selection_range = self._find_word_selection_range(token, occurrence=word_seen.get(token, 1))
                    self.last_selection_mode = 'native-line'
                    return True
        return False
    
    def _is_dark_bg(self):
        """Detect whether the text control has a dark background."""
        if self._bg_is_dark is None:
            bg = self.page_text.GetBackgroundColour()
            brightness = math.sqrt(bg.Red()**2 + bg.Green()**2 + bg.Blue()**2)
            self._bg_is_dark = brightness < 127 * math.sqrt(3)
        return self._bg_is_dark
    
    def _text_colour(self):
        """Return normal text colour appropriate for the background."""
        return wx.Colour(255, 255, 255) if self._is_dark_bg() else wx.Colour(0, 0, 0)
    
    def _placeholder_colour(self):
        """Return muted placeholder colour appropriate for the background."""
        return wx.Colour(120, 120, 120) if self._is_dark_bg() else wx.Colour(160, 160, 160)

    def _set_status(self, message):
        self.SetStatusText(message, 0)

    def _start_control_server(self):
        tcp_enabled = bool(self.config.get('control_tcp_enabled', True))
        udp_enabled = bool(self.config.get('control_udp_enabled', True))
        unix_enabled = bool(self.config.get('control_unix_enabled', False))
        if not (tcp_enabled or udp_enabled or unix_enabled):
            self._set_status('Control interface disabled (all transports off)')
            return
        host = self.config.get('control_host', '127.0.0.1')
        tcp_port = self.config.get('control_tcp_port', 8711)
        udp_port = self.config.get('control_udp_port', 8711)
        unix_socket = self.config.get('control_unix_socket', '')
        token = self.config.get('control_token', '')

        if unix_enabled and not unix_socket:
            self._set_status('Control start failed: unix enabled but control_unix_socket is empty')
            return

        try:
            self.control_server = RemoteControlServer(
                browser=self,
                host=host,
                tcp_enabled=tcp_enabled,
                udp_enabled=udp_enabled,
                unix_enabled=unix_enabled,
                tcp_port=tcp_port,
                udp_port=udp_port,
                unix_socket=unix_socket,
                token=token,
            )
            self.control_server.start()
            listeners = []
            if tcp_enabled:
                listeners.append(f"tcp:{host}:{tcp_port}")
            if udp_enabled:
                listeners.append(f"udp:{host}:{udp_port}")
            if unix_enabled:
                listeners.append(f"unix:{unix_socket}")
            self._set_status(f"Control ready on {', '.join(listeners)}")
        except Exception as e:
            self.control_server = None
            self._set_status(f"Control start failed: {e}")

    def _on_control_settings(self, _event):
        dlg = ControlSettingsDialog(self, self.config)
        if dlg.ShowModal() == wx.ID_OK:
            updates = dlg.get_values()
            self.config.update(updates)
            if self.control_server:
                self.control_server.stop()
                self.control_server = None
            self._start_control_server()
        dlg.Destroy()

    def _update_base_url_status(self):
        base = str(self.config.get('html_base_url', '')).rstrip('/')
        source = self.config.get('html_base_url_source', 'fallback')
        self.SetStatusText(f"HTML URL: {base or '(unset)'} [{source}]", 1)

    def _copy_to_clipboard(self, text):
        if text is None:
            return False
        if not wx.TheClipboard.Open():
            return False
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(str(text)))
            wx.TheClipboard.Flush()
            return True
        finally:
            wx.TheClipboard.Close()

    def _current_page_key(self):
        return self.current_page or ''

    def _page_key_to_html_url(self, key):
        base = str(self.config.get('html_base_url', 'http://127.0.0.1/notes')).rstrip('/')
        if key == '':
            return base + '/'
        return base + '/' + quote(key, safe='/')

    def _block_text(self, block):
        if isinstance(block, str):
            return block
        if not isinstance(block, dict):
            return ''

        if 'heading' in block:
            heading = block['heading']
            if isinstance(heading, dict):
                return str(heading.get('text', ''))
            return str(heading)

        if 'para' in block:
            para = block['para']
            if isinstance(para, str):
                return para
            if isinstance(para, list):
                return self._inline_text(para)
            return str(para)

        if 'codeblock' in block:
            codeblock = block['codeblock']
            if isinstance(codeblock, dict):
                return str(codeblock.get('body', codeblock.get('text', '')))
            return str(codeblock)

        if 'list' in block:
            lst = block['list']
            if not isinstance(lst, dict):
                return str(lst)
            lines = []
            label = lst.get('label')
            if label:
                lines.append(str(label))
            for item in lst.get('items', []):
                if isinstance(item, str):
                    lines.append(item)
                elif isinstance(item, list):
                    lines.append(self._inline_text(item))
                elif isinstance(item, dict):
                    lines.append(', '.join(f"{k}: {v}" for k, v in item.items()))
                else:
                    lines.append(str(item))
            return '\n'.join(lines)

        if 'table' in block:
            table = block['table']
            if not isinstance(table, dict):
                return str(table)
            rows = []
            cols = table.get('columns', [])
            if cols:
                rows.append(' | '.join(str(c) for c in cols))
            for row in table.get('rows', []):
                cells = row if isinstance(row, list) else [row]
                rows.append(' | '.join(str(c) for c in cells))
            return '\n'.join(rows)

        if 'svg' in block:
            sv = block['svg']
            if not isinstance(sv, dict):
                return ''
            return str(sv.get('alt') or sv.get('caption') or '')

        if 'image' in block:
            im = block['image']
            if not isinstance(im, dict):
                return ''
            return str(im.get('alt') or im.get('caption') or '')

        if 'details' in block:
            d = block['details']
            if not isinstance(d, dict):
                return ''
            nested = '\n'.join(self._block_text(b) for b in d.get('content', []) or [])
            return '\n'.join(filter(None, [str(d.get('summary') or ''), nested]))

        return ''

    def _inline_text(self, items):
        parts = []
        for item in items:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if 'link' in item:
                    link = item['link']
                    if isinstance(link, dict):
                        parts.append(str(link.get('text', link.get('href', ''))))
                    else:
                        parts.append(str(link))
                elif 'href' in item:
                    parts.append(str(item.get('text', item.get('href', ''))))
                else:
                    key = next(iter(item), None)
                    if INLINE_SPAN_TAGS.get(key):
                        parts.append(str(item[key]))
                    else:
                        parts.append(str(item))
            else:
                parts.append(str(item))
        return ''.join(parts)

    def _get_current_doc(self):
        return self.data_source.read(self._current_page_key())

    def _get_content_blocks(self):
        doc = self._get_current_doc()
        if isinstance(doc, dict):
            content = doc.get('content', [])
            return content if isinstance(content, list) else []
        if isinstance(doc, list):
            return doc
        return []

    def _get_rendered_text(self):
        blocks = self._get_content_blocks()
        texts = [self._block_text(block) for block in blocks]
        return '\n\n'.join(t for t in texts if t)

    def _read_selection_text(self):
        if hasattr(self.html, 'SelectionToText'):
            try:
                native_text = self.html.SelectionToText()
                if native_text:
                    return native_text.strip('\n')
            except Exception:
                pass

        sel = self.selection_range
        if not sel:
            return ''
        blocks = self._get_content_blocks()
        if not blocks:
            return ''

        bs = sel['block_start']
        be = sel['block_end']
        os0 = sel['offset_start']
        oe0 = sel['offset_end']

        if bs == be:
            text = self._block_text(blocks[bs])
            return text[os0:oe0]

        out = []
        first = self._block_text(blocks[bs])
        out.append(first[os0:])
        for idx in range(bs + 1, be):
            out.append(self._block_text(blocks[idx]))
        last = self._block_text(blocks[be])
        out.append(last[:oe0])
        return '\n'.join(out)

    def _replace_text_in_block(self, block, start, end, replacement):
        """Replace text in a block for block types with direct text payloads."""
        if isinstance(block, str):
            return block[:start] + replacement + block[end:]
        if not isinstance(block, dict):
            raise ValueError('Unsupported block type for write')

        if 'para' in block and isinstance(block['para'], str):
            para = block['para']
            block['para'] = para[:start] + replacement + para[end:]
            return block

        if 'heading' in block:
            heading = block['heading']
            if isinstance(heading, dict):
                text = str(heading.get('text', ''))
                heading['text'] = text[:start] + replacement + text[end:]
                return block
            if isinstance(heading, str):
                block['heading'] = heading[:start] + replacement + heading[end:]
                return block

        if 'codeblock' in block:
            codeblock = block['codeblock']
            if isinstance(codeblock, dict):
                body_key = 'body' if 'body' in codeblock else 'text'
                body = str(codeblock.get(body_key, ''))
                codeblock[body_key] = body[:start] + replacement + body[end:]
                return block
            if isinstance(codeblock, str):
                block['codeblock'] = codeblock[:start] + replacement + codeblock[end:]
                return block

        raise ValueError('Write only supports string para/heading/codeblock blocks')

    def _write_selection_text(self, replacement):
        """Replace current selection and persist document when backend supports writes."""
        if not self.selection_range:
            raise ValueError('No selection set')
        if not isinstance(replacement, str):
            raise ValueError('Replacement text must be a string')

        sel = self.selection_range
        if sel['block_start'] != sel['block_end']:
            raise ValueError('Cross-block writes are not supported')

        doc = self._get_current_doc()
        if not isinstance(doc, (dict, list)):
            raise ValueError('Current document is not writable JSONHTL')

        idx = sel['block_start']
        start = sel['offset_start']
        end = sel['offset_end']

        if isinstance(doc, dict):
            content = doc.get('content', [])
            if not isinstance(content, list):
                raise ValueError("Current document has no list 'content'")
            if idx < 0 or idx >= len(content):
                raise ValueError('Selection block out of range')
            content[idx] = self._replace_text_in_block(content[idx], start, end, replacement)
        else:
            if idx < 0 or idx >= len(doc):
                raise ValueError('Selection block out of range')
            doc[idx] = self._replace_text_in_block(doc[idx], start, end, replacement)

        self.data_source.write(self._current_page_key(), doc)
        self.selection_range['offset_end'] = self.selection_range['offset_start'] + len(replacement)
        self._navigate_to(self._current_page_key(), add_to_history=False)
    
    def _navigate_to(self, page_key, add_to_history=True):
        """Navigate to a specific page."""
        if page_key != self.current_page:
            self.selection_range = None
            self.last_selection_mode = 'none'

        # Add to history if different from current and not navigating via back/forward
        if add_to_history and page_key != self.current_page:
            # Trim forward history if we're not at the end
            if self.history_position < len(self.history) - 1:
                self.history = self.history[:self.history_position + 1]
            
            self.history.append(page_key)
            self.history_position = len(self.history) - 1
        
        self.current_page = page_key
        if page_key == "":
            self.page_text.SetValue("(root)")
            self.page_text.SetForegroundColour(self._placeholder_colour())
        else:
            self.page_text.SetValue(page_key)
            self.page_text.SetForegroundColour(self._text_colour())
        
        # Load and render the page
        self.renderer.selection_range = self.selection_range
        self.renderer.use_selection_markup = (self.last_selection_mode == 'logical-only')
        data = self.data_source.read(page_key)
        html = self.renderer.render(page_key, data)
        
        # Load HTML into viewer
        self.html.SetPage(html)
        # Store metadata for status line display
        self._page_meta_text = ''
        if isinstance(data, dict):
            pairs = [(k, format_meta_value(v)) for k, v in data.items() if k not in ('title', 'content')]
            if pairs:
                self._page_meta_text = '  ·  '.join(f'{k}: {v}' for k, v in pairs)
        self._update_selection_indicator()
        self._set_status(f"Loaded page: {page_key or '(root)'}")
    
    def _on_open(self, event):
        """Open a specific page."""
        dlg = wx.TextEntryDialog(self, "Enter page name:", "Open Page")
        if dlg.ShowModal() == wx.ID_OK:
            page_key = dlg.GetValue()
            self._navigate_to(page_key)
        dlg.Destroy()
    
    def _on_exit(self, event):
        """Exit the application."""
        self.Close(True)
    
    def _on_back(self, event):
        """Go back in history."""
        if self.history_position > 0:
            self.history_position -= 1
            page = self.history[self.history_position]
            self._navigate_to(page, add_to_history=False)
    
    def _on_forward(self, event):
        """Go forward in history."""
        if self.history_position < len(self.history) - 1:
            self.history_position += 1
            page = self.history[self.history_position]
            self._navigate_to(page, add_to_history=False)
    
    def _on_home(self, event):
        """Go to home page (root)."""
        self._navigate_to("")
    
    def _on_refresh(self, event):
        """Refresh current page."""
        self._navigate_to(self.current_page, add_to_history=False)

    def _on_copy_selected(self, event):
        """Copy selected text from focused control."""
        focused = self.FindFocus()
        text = ''
        if focused and isinstance(focused, wx.TextCtrl):
            start, end = focused.GetSelection()
            if start != end:
                text = focused.GetStringSelection()
        elif hasattr(self.html, 'SelectionToText'):
            try:
                text = self.html.SelectionToText().strip('\n')
            except Exception:
                text = ''

        if not text:
            self._set_status("Nothing selected to copy")
            return

        if self._copy_to_clipboard(text):
            self._set_status(f"Copied selection ({len(text)} chars)")
        else:
            self._set_status("Clipboard unavailable")

    def _on_copy_page_json(self, event):
        """Copy the current page document JSON to clipboard."""
        doc = self._get_current_doc()
        if doc is None:
            self._set_status("No current page document to copy")
            return
        text = json.dumps(doc, indent=2, ensure_ascii=False)
        if self._copy_to_clipboard(text):
            self._set_status("Copied page JSON")
        else:
            self._set_status("Clipboard unavailable")

    def _on_copy_link(self, event):
        """Copy current page key as internal link target."""
        key = self._current_page_key()
        if self._copy_to_clipboard(key):
            self._set_status(f"Copied page key: {key or '(root)'}")
        else:
            self._set_status("Clipboard unavailable")

    def _on_copy_link_as_url(self, event):
        """Copy URL to equivalent page in HTML renderer."""
        key = self._current_page_key()
        url = self._page_key_to_html_url(key)
        if self._copy_to_clipboard(url):
            self._set_status("Copied page URL")
        else:
            self._set_status("Clipboard unavailable")

    def _set_zoom(self, zoom):
        assert isinstance(zoom, int)
        self.renderer.zoom_percent = max(1, int(zoom))
        html_font_size = max(1, round(self.BASE_HTML_FONT * self.renderer.zoom_percent / 100.0))
        # Use HtmlWindow native font scaling (CSS font-size on body is not reliable here).
        # SetFonts(normal_face, fixed_face, sizes) — use Courier for fixed-width (pre/code) blocks.
        self.html.SetFonts("", "Courier", [html_font_size] * 7)
        self._navigate_to(self._current_page_key(), add_to_history=False)
        self._set_status(f"Zoom: {self.renderer.zoom_percent}% (font {html_font_size})")

    def _on_zoom_in(self, event):
        self._set_zoom(self.renderer.zoom_percent + 10)

    def _on_zoom_out(self, event):
        self._set_zoom(self.renderer.zoom_percent - 10)

    def _on_zoom_reset(self, event):
        self._set_zoom(100)

    def _on_html_mousewheel(self, event):
        if event.ControlDown():
            rotation = event.GetWheelRotation()
            if rotation > 0:
                self._on_zoom_in(None)
            elif rotation < 0:
                self._on_zoom_out(None)
            return
        event.Skip()
    
    def _on_page_text_enter(self, event):
        """Navigate when Enter is pressed in the page field."""
        key = self.page_text.GetValue().strip()
        if key == "(root)":
            key = ""
        self._navigate_to(key)
        self.html.SetFocus()
    
    def _on_page_text_focus(self, event):
        """Clear the grey placeholder when the field gains focus."""
        if self.current_page == "" and self.page_text.GetValue() == "(root)":
            self.page_text.SetValue("")
            self.page_text.SetForegroundColour(self._text_colour())
        self.page_text.SelectAll()
        event.Skip()
    
    def _on_page_text_blur(self, event):
        """Restore the grey placeholder if the field is empty on blur."""
        if self.page_text.GetValue().strip() == "":
            self.page_text.SetValue("(root)")
            self.page_text.SetForegroundColour(self._placeholder_colour())
        event.Skip()

    def _on_close(self, event):
        """Shutdown control server and close data source cleanly."""
        if self.control_server:
            self.control_server.stop()
            self.control_server = None
        self.data_source.close()
        event.Skip()

    def _rpc_dispatch(self, method, params):
        """Execute a read-only RPC command on the UI thread."""
        assert isinstance(method, str)
        assert params is None or isinstance(params, dict)
        params = params or {}

        if method == 'status.ping':
            return {'ok': True, 'page': self._current_page_key()}

        if method == 'status.capabilities':
            return {
                'version': 1,
                'read_only': True,
                'methods': [
                    'status.ping',
                    'status.capabilities',
                    'navigate.go_to_page',
                    'navigate.back',
                    'navigate.forward',
                    'navigate.home',
                    'navigate.refresh',
                    'page.get_current',
                    'page.get_document_json',
                    'page.put_document_json',
                    'page.get_rendered_text',
                    'view.get_zoom',
                    'view.zoom_in',
                    'view.zoom_out',
                    'view.zoom_reset',
                    'view.zoom_set',
                    'view.scroll_pages',
                    'selection.get',
                    'selection.set_by_block_offsets',
                    'selection.native_select_word_by_text',
                    'selection.native_select_line_by_text',
                    'selection.read_text',
                    'selection.write_text',
                    'selection.set_and_capture',
                    'links.get_current',
                    'ui.set_window_size',
                    'ui.capture_screenshot',
                    'ui.capture_sixel',
                    'ui.quit',
                ]
            }

        if method == 'navigate.go_to_page':
            key = params.get('key', '')
            if not isinstance(key, str):
                raise ValueError("'key' must be a string")
            self._navigate_to(key)
            return {'page': self._current_page_key()}

        if method == 'navigate.back':
            self._on_back(None)
            return {'page': self._current_page_key()}

        if method == 'navigate.forward':
            self._on_forward(None)
            return {'page': self._current_page_key()}

        if method == 'navigate.home':
            self._on_home(None)
            return {'page': self._current_page_key()}

        if method == 'navigate.refresh':
            self._on_refresh(None)
            return {'page': self._current_page_key()}

        if method == 'page.get_current':
            key = self._current_page_key()
            return {
                'key': key,
                'url': self._page_key_to_html_url(key),
            }

        if method == 'page.get_document_json':
            return {'document': self._get_current_doc()}

        if method == 'page.put_document_json':
            key = params.get('key')
            doc = params.get('document')
            if not isinstance(key, str):
                raise ValueError("'key' must be a string")
            if doc is None:
                raise ValueError("Missing parameter: document")
            self.data_source.write(key, doc)
            if key == self._current_page_key():
                self._navigate_to(key, add_to_history=False)
            return {'status': 'ok', 'key': key}

        if method == 'page.get_rendered_text':
            return {'text': self._get_rendered_text()}

        if method == 'view.get_zoom':
            return {'zoom_percent': self.renderer.zoom_percent}

        if method == 'view.zoom_in':
            self._on_zoom_in(None)
            return {'zoom_percent': self.renderer.zoom_percent}

        if method == 'view.zoom_out':
            self._on_zoom_out(None)
            return {'zoom_percent': self.renderer.zoom_percent}

        if method == 'view.zoom_reset':
            self._on_zoom_reset(None)
            return {'zoom_percent': self.renderer.zoom_percent}

        if method == 'view.zoom_set':
            value = params.get('zoom_percent')
            if value is None:
                raise ValueError("Missing parameter: zoom_percent")
            self._set_zoom(int(value))
            return {'zoom_percent': self.renderer.zoom_percent}

        if method == 'view.scroll_pages':
            pages = int(params.get('pages', 1))
            if pages == 0:
                return {'scrolled': False, 'pages': 0}
            scrolled = bool(self.html.ScrollPages(pages))
            return {'scrolled': scrolled, 'pages': pages}

        if method == 'selection.get':
            self._update_selection_indicator()
            return {'selection': self.selection_range, 'selection_mode': self.last_selection_mode}

        if method == 'selection.set_by_block_offsets':
            required = ['block_index_start', 'offset_start', 'block_index_end', 'offset_end']
            for key in required:
                if key not in params:
                    raise ValueError(f"Missing parameter: {key}")

            bs = int(params['block_index_start'])
            os0 = int(params['offset_start'])
            be = int(params['block_index_end'])
            oe0 = int(params['offset_end'])
            if bs > be or (bs == be and os0 > oe0):
                raise ValueError('Selection start must be <= selection end')

            blocks = self._get_content_blocks()
            if bs < 0 or be < 0 or bs >= len(blocks) or be >= len(blocks):
                raise ValueError('Block index out of range')

            bstart_text = self._block_text(blocks[bs])
            bend_text = self._block_text(blocks[be])
            if os0 < 0 or os0 > len(bstart_text):
                raise ValueError('offset_start out of range')
            if oe0 < 0 or oe0 > len(bend_text):
                raise ValueError('offset_end out of range')

            self.selection_range = {
                'block_start': bs,
                'offset_start': os0,
                'block_end': be,
                'offset_end': oe0,
            }
            self._navigate_to(self._current_page_key(), add_to_history=False)
            native = self._apply_native_word_selection()
            if not native:
                self._navigate_to(self._current_page_key(), add_to_history=False)
            self._update_selection_indicator()
            return {
                'selection': self.selection_range,
                'native_selected': native,
                'selection_mode': self.last_selection_mode,
            }

        if method == 'selection.native_select_word_by_text':
            text = params.get('text')
            if text is None:
                raise ValueError("Missing parameter: text")
            occurrence = int(params.get('occurrence', 1))
            ok = self._apply_native_word_selection_by_text(text, occurrence=occurrence)
            if not ok:
                raise ValueError('Word not found for native selection')
            self._update_selection_indicator()
            return {
                'selected_text': self._read_selection_text(),
                'selection_mode': self.last_selection_mode,
            }

        if method == 'selection.native_select_line_by_text':
            text = params.get('text')
            if text is None:
                raise ValueError("Missing parameter: text")
            occurrence = int(params.get('occurrence', 1))
            ok = self._apply_native_line_selection_by_text(text, occurrence=occurrence)
            if not ok:
                raise ValueError('Line token not found for native selection')
            self._update_selection_indicator()
            return {
                'selected_text': self._read_selection_text(),
                'selection_mode': self.last_selection_mode,
            }

        if method == 'selection.read_text':
            self._update_selection_indicator()
            return {'text': self._read_selection_text(), 'selection_mode': self.last_selection_mode}

        if method == 'selection.write_text':
            text = params.get('text')
            if text is None:
                raise ValueError("Missing parameter: text")
            self._write_selection_text(str(text))
            self._update_selection_indicator()
            return {
                'selection': self.selection_range,
                'text': self._read_selection_text(),
                'selection_mode': self.last_selection_mode,
            }

        if method == 'selection.set_and_capture':
            required = ['block_index_start', 'offset_start', 'block_index_end', 'offset_end']
            for key in required:
                if key not in params:
                    raise ValueError(f"Missing parameter: {key}")

            bs = int(params['block_index_start'])
            os0 = int(params['offset_start'])
            be = int(params['block_index_end'])
            oe0 = int(params['offset_end'])
            if bs > be or (bs == be and os0 > oe0):
                raise ValueError('Selection start must be <= selection end')

            blocks = self._get_content_blocks()
            if bs < 0 or be < 0 or bs >= len(blocks) or be >= len(blocks):
                raise ValueError('Block index out of range')

            bstart_text = self._block_text(blocks[bs])
            bend_text = self._block_text(blocks[be])
            if os0 < 0 or os0 > len(bstart_text):
                raise ValueError('offset_start out of range')
            if oe0 < 0 or oe0 > len(bend_text):
                raise ValueError('offset_end out of range')

            self.selection_range = {
                'block_start': bs,
                'offset_start': os0,
                'block_end': be,
                'offset_end': oe0,
            }
            self._navigate_to(self._current_page_key(), add_to_history=False)
            native = self._apply_native_word_selection()
            if not native:
                self._navigate_to(self._current_page_key(), add_to_history=False)
            self._update_selection_indicator()
            out_path = params.get('path')
            shot_path = self._capture_window_screenshot(out_path=out_path)
            return {
                'selection': self.selection_range,
                'text': self._read_selection_text(),
                'native_selected': native,
                'selection_mode': self.last_selection_mode,
                'path': shot_path,
            }

        if method == 'links.get_current':
            key = self._current_page_key()
            return {
                'key': key,
                'url': self._page_key_to_html_url(key),
            }

        if method == 'ui.capture_screenshot':
            out_path = params.get('path')
            saved = self._capture_window_screenshot(out_path=out_path)
            return {'path': saved}

        if method == 'ui.capture_sixel':
            out_path = params.get('path')
            include_data = bool(params.get('include_data', True))
            return self._capture_window_sixel(out_path=out_path, include_data=include_data)

        if method == 'ui.set_window_size':
            width = params.get('width')
            height = params.get('height')
            if width is None or height is None:
                raise ValueError("Missing parameters: width and height are required")
            width = int(width)
            height = int(height)
            assert isinstance(width, int)
            assert isinstance(height, int)
            if width < 200 or height < 200:
                raise ValueError('width and height must be >= 200')
            self.SetSize(width, height)
            self._flush_ui_updates()
            w, h = self.GetSize()
            return {'width': int(w), 'height': int(h)}

        if method == 'ui.quit':
            # Close asynchronously so the RPC reply can be sent first.
            wx.CallAfter(self.Close)
            return {'status': 'quitting'}

        raise KeyError(f'Method not found: {method}')


class RemoteControlServer:
    """JSON-RPC 2.0 control plane over TCP line-delimited JSON and UDP datagrams."""

    def __init__(self, browser, host, tcp_enabled, udp_enabled, unix_enabled, tcp_port, udp_port, unix_socket='', token=''):
        self.browser = browser
        self.host = host
        self.tcp_enabled = bool(tcp_enabled)
        self.udp_enabled = bool(udp_enabled)
        self.unix_enabled = bool(unix_enabled)
        self.tcp_port = int(tcp_port)
        self.udp_port = int(udp_port)
        self.unix_socket = str(unix_socket or '')
        self.token = token or ''
        self._stop = threading.Event()
        self._threads = []
        self._tcp_sock = None
        self._udp_sock = None
        self._unix_sock = None

    def start(self):
        if not (self.tcp_enabled or self.udp_enabled or self.unix_enabled):
            raise RuntimeError('All control transports are disabled')

        if self.tcp_enabled:
            tcp_thread = threading.Thread(target=self._run_tcp, name='notes-browser-tcp', daemon=True)
            tcp_thread.start()
            self._threads.append(tcp_thread)

        if self.udp_enabled:
            udp_thread = threading.Thread(target=self._run_udp, name='notes-browser-udp', daemon=True)
            udp_thread.start()
            self._threads.append(udp_thread)

        if self.unix_enabled:
            uds_thread = threading.Thread(target=self._run_unix, name='notes-browser-unix', daemon=True)
            uds_thread.start()
            self._threads.append(uds_thread)

    def stop(self):
        self._stop.set()
        for sock in (self._tcp_sock, self._udp_sock, self._unix_sock):
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        if self.unix_socket:
            try:
                os.unlink(self.unix_socket)
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def _make_error(self, req_id, code, message):
        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'error': {
                'code': code,
                'message': message,
            }
        }

    def _make_result(self, req_id, result):
        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'result': result,
        }

    def _validate_token(self, params):
        if not self.token:
            return
        if not isinstance(params, dict) or params.get('token') != self.token:
            raise PermissionError('Invalid control token')

    def _invoke_browser(self, method, params):
        app = wx.App.Get()
        if app is None:
            return self.browser._rpc_dispatch(method, params)

        done = threading.Event()
        holder = {}

        def _run():
            try:
                holder['result'] = self.browser._rpc_dispatch(method, params)
            except Exception as e:
                holder['error'] = e
            finally:
                done.set()

        wx.CallAfter(_run)
        if not done.wait(timeout=10):
            raise TimeoutError('UI thread timeout')
        if 'error' in holder:
            raise holder['error']
        return holder['result']

    def _handle_request(self, raw_message, transport):
        try:
            req = json.loads(raw_message)
        except json.JSONDecodeError:
            return self._make_error(None, -32700, 'Parse error')

        if not isinstance(req, dict):
            return self._make_error(None, -32600, 'Invalid Request')

        req_id = req.get('id')
        method = req.get('method')
        params = req.get('params', {})

        if req.get('jsonrpc') != '2.0' or not isinstance(method, str):
            return self._make_error(req_id, -32600, 'Invalid Request')

        if method in ('ui.capture_screenshot', 'ui.capture_sixel') and transport == 'udp':
            return self._make_error(req_id, -32601, 'Method not available on UDP transport')

        try:
            self._validate_token(params)
            result = self._invoke_browser(method, params)
            if req_id is None:
                return None
            return self._make_result(req_id, result)
        except PermissionError as e:
            return self._make_error(req_id, -32001, str(e))
        except KeyError as e:
            return self._make_error(req_id, -32601, str(e))
        except ValueError as e:
            return self._make_error(req_id, -32602, str(e))
        except Exception as e:
            return self._make_error(req_id, -32050, f'{e}')

    def _run_tcp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.tcp_port))
        sock.listen(5)
        sock.settimeout(0.5)
        self._tcp_sock = sock

        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
                conn.settimeout(1.0)
            except socket.timeout:
                continue
            except OSError:
                break

            self._serve_stream_connection(conn, transport='tcp')

    def _serve_stream_connection(self, conn, transport):
        with conn:
            buf = ''
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf += data.decode('utf-8', errors='replace')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    resp = self._handle_request(line, transport=transport)
                    if resp is None:
                        continue
                    payload = (json.dumps(resp) + '\n').encode('utf-8')
                    try:
                        conn.sendall(payload)
                    except OSError:
                        break

    def _run_unix(self):
        sock_path = self.unix_socket
        parent = os.path.dirname(sock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(sock_path)
        try:
            os.chmod(sock_path, 0o600)
        except Exception:
            pass
        sock.listen(5)
        sock.settimeout(0.5)
        self._unix_sock = sock

        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
                conn.settimeout(1.0)
            except socket.timeout:
                continue
            except OSError:
                break

            self._serve_stream_connection(conn, transport='unix')

        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _run_udp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.host, self.udp_port))
        sock.settimeout(0.5)
        self._udp_sock = sock

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            msg = data.decode('utf-8', errors='replace').strip()
            if not msg:
                continue
            resp = self._handle_request(msg, transport='udp')
            if resp is None:
                continue
            payload = json.dumps(resp).encode('utf-8')
            try:
                sock.sendto(payload, addr)
            except OSError:
                continue


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Notes Browser - Hypertext browser for gdata notes system"
    )
    parser.add_argument(
        "--gdbm-file",
        help="Path to GDBM file (if not using HTTP server)",
        default=None
    )
    parser.add_argument(
        "--url",
        help="URL of gdata HTTP server",
        default=None
    )
    parser.add_argument(
        "--config",
        help="Path to settings YAML file (default: ~/.notes_browser.yaml)",
        default=None
    )
    parser.add_argument(
        "--html-base-url",
        help="Base URL for HTML renderer links (highest precedence)",
        default=None
    )
    parser.add_argument(
        "--html-path",
        help="Reserved: HTML renderer path component",
        default=None
    )
    parser.add_argument(
        "--render-font-family",
        help="HTML render font-family override (e.g. \"'Courier New', Courier, monospace\")",
        default=None
    )
    parser.add_argument(
        "--render-font-size-pt",
        type=int,
        help="HTML render font size in points (default: 10)",
        default=None
    )
    parser.add_argument(
        "--control-host",
        help="Control interface bind host (default from config)",
        default=None
    )
    parser.add_argument(
        "--control-tcp-enabled",
        dest="control_tcp_enabled",
        action="store_true",
        help="Enable TCP control listener"
    )
    parser.add_argument(
        "--no-control-tcp",
        dest="control_tcp_enabled",
        action="store_false",
        help="Disable TCP control listener"
    )
    parser.set_defaults(control_tcp_enabled=None)
    parser.add_argument(
        "--control-tcp-port",
        type=int,
        help="Control interface TCP port",
        default=None
    )
    parser.add_argument(
        "--control-udp-enabled",
        dest="control_udp_enabled",
        action="store_true",
        help="Enable UDP control listener"
    )
    parser.add_argument(
        "--no-control-udp",
        dest="control_udp_enabled",
        action="store_false",
        help="Disable UDP control listener"
    )
    parser.set_defaults(control_udp_enabled=None)
    parser.add_argument(
        "--control-udp-port",
        type=int,
        help="Control interface UDP port",
        default=None
    )
    parser.add_argument(
        "--control-unix-enabled",
        dest="control_unix_enabled",
        action="store_true",
        help="Enable Unix domain stream control listener"
    )
    parser.add_argument(
        "--no-control-unix",
        dest="control_unix_enabled",
        action="store_false",
        help="Disable Unix domain stream control listener"
    )
    parser.set_defaults(control_unix_enabled=None)
    parser.add_argument(
        "--control-unix-socket",
        help="Control interface Unix domain stream socket path",
        default=None
    )
    parser.add_argument(
        "--control-token",
        help="Optional shared token for JSON-RPC control requests",
        default=None
    )
    
    args = parser.parse_args()
    
    cfg = load_browser_config(args)
    cfg['project_root'] = os.environ.get('PWD', os.getcwd())
    url = cfg.get('notes_url')
    gdbm_file = cfg.get('gdbm_file')
    
    # Default to local .agent_notes.gdbm if available
    if not url and not gdbm_file:
        if os.path.exists('.agent_notes.gdbm'):
            gdbm_file = '.agent_notes.gdbm'
        else:
            url = 'http://127.0.0.1:8021'
    
    data_source = None
    try:
        data_source = NotesDataSource(url=url, gdbm_file=gdbm_file)
    except RuntimeError as e:
        print(f"Warning: {e}")
        # If we tried a GDBM file and it failed, fall back to HTTP server
        if gdbm_file:
            print("Falling back to HTTP server at http://127.0.0.1:8021")
            try:
                data_source = NotesDataSource(url='http://127.0.0.1:8021', gdbm_file=None)
            except Exception as e2:
                print(f"Error: {e2}")
                sys.exit(1)
        else:
            sys.exit(1)
    # Create and run app
    app = wx.App()
    frame = NotesBrowser(data_source, cfg)
    frame.Show()
    app.MainLoop()


if __name__ == '__main__':
    main()
