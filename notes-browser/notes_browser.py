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
from pathlib import Path
from urllib.parse import quote

# Add parent directory to path to import gdata
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gdata
import notes_client

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
                json_str = self.db[key]
                return json.loads(json_str)
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
    
    def close(self):
        """Close the database connection."""
        if self.db:
            self.db.close()


class NotesHTMLRenderer:
    """Convert JSONHTL note data to HTML for display."""
    
    def render(self, key, data):
        """Render note data as HTML."""
        if data is None:
            return self._error_html(f"Document '{key}' not found")
        
        html = ['<html><body style="font-family: sans-serif; margin: 20px;">']
        
        # Add page title
        html.append(f'<h1>Page: {key or "ROOT"}</h1>')
        html.append('<hr/>')
        
        if isinstance(data, dict):
            # JSONHTL document - look for title and content
            metadata = {}
            
            # Render title if present
            if 'title' in data:
                html.append(f'<h2>{self._escape(str(data["title"]))}</h2>')
            
            # Render content array (JSONHTL blocks)
            if 'content' in data and isinstance(data['content'], list):
                html.append(self._render_jsonhtl_blocks(data['content']))
            
            # Collect metadata fields
            for field, value in data.items():
                if field in ['title', 'content']:
                    continue  # Already handled
                elif field.startswith('_') or field in ['created', 'modified', 'updated', 'type', 'kind', 'version', 'tags']:
                    metadata[field] = value
                elif isinstance(value, (dict, list)):
                    metadata[field] = value
                else:
                    # Other scalar fields - show inline
                    html.append(f'<p><b>{self._escape(field)}:</b> {self._escape(str(value))}</p>')
            
            # Add metadata section if exists
            if metadata:
                html.append('<hr/>')
                html.append('<h3 style="color: #666;">Metadata</h3>')
                html.append('<table style="border-collapse: collapse; width: 100%; max-width: 600px;">')
                for field, value in metadata.items():
                    html.append('<tr style="border-bottom: 1px solid #ddd;">')
                    html.append(f'<td style="padding: 5px; font-weight: bold; width: 150px;">{self._escape(field)}</td>')
                    val_str = str(value)[:200]
                    html.append(f'<td style="padding: 5px;"><code>{self._escape(val_str)}</code></td>')
                    html.append('</tr>')
                html.append('</table>')
        
        elif isinstance(data, list):
            # Raw list - render as JSONHTL blocks
            html.append(self._render_jsonhtl_blocks(data))
        
        else:
            # Simple value
            html.append(f'<p>{self._escape(str(data))}</p>')
        
        html.append('</body></html>')
        return '\n'.join(html)
    
    def _render_jsonhtl_blocks(self, blocks):
        """Render a list of JSONHTL block elements."""
        html = []
        for block in blocks:
            if isinstance(block, dict):
                if 'heading' in block:
                    html.append(self._render_heading(block['heading']))
                elif 'para' in block:
                    html.append(self._render_para(block['para']))
                elif 'codeblock' in block:
                    html.append(self._render_codeblock(block['codeblock']))
                elif 'table' in block:
                    html.append(self._render_table(block['table']))
                else:
                    # Unknown block type - show as text
                    html.append(f'<p>{self._escape(str(block))}</p>')
            elif isinstance(block, str):
                html.append(f'<p>{self._escape(block)}</p>')
        return '\n'.join(html)
    
    def _render_heading(self, heading):
        """Render a JSONHTL heading block."""
        if isinstance(heading, dict):
            level = heading.get('level', 2)
            text = heading.get('text', '')
            tag = f'h{min(max(level, 1), 6)}'
            return f'<{tag}>{self._escape(text)}</{tag}>'
        return f'<h2>{self._escape(str(heading))}</h2>'
    
    def _render_para(self, para):
        """Render a JSONHTL para block (can contain inline elements)."""
        if isinstance(para, list):
            # Para is a list of inline elements
            content = self._render_inline_list(para)
            return f'<p>{content}</p>'
        elif isinstance(para, str):
            return f'<p>{self._escape(para)}</p>'
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
                elif 'code' in item:
                    result.append(f'<code>{self._escape(item["code"])}</code>')
                elif 'href' in item:
                    # Direct link object
                    result.append(self._render_link(item))
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
            # Use navigate:// protocol for internal links
            if href.startswith('http://') or href.startswith('https://'):
                return f'<a href="{self._escape(href)}">{self._escape(text)}</a>'
            else:
                return f'<a href="navigate://{quote(href)}" style="color: #0066cc;">{self._escape(text)}</a>'
        return f'<a href="navigate://{quote(str(link))}">{self._escape(str(link))}</a>'
    
    def _render_codeblock(self, codeblock):
        """Render a JSONHTL codeblock."""
        if isinstance(codeblock, dict):
            # JSONHTL spec uses 'body' and 'lang'
            body = codeblock.get('body', codeblock.get('text', ''))
            lang = codeblock.get('lang', codeblock.get('language', ''))
            return f'<pre style="background: #f4f4f4; padding: 10px; overflow-x: auto;"><code>{self._escape(body)}</code></pre>'
        return f'<pre style="background: #f4f4f4; padding: 10px;"><code>{self._escape(str(codeblock))}</code></pre>'
    
    def _render_table(self, table):
        """Render a JSONHTL table block as an HTML table."""
        if not isinstance(table, dict):
            return f'<p>{self._escape(str(table))}</p>'
        columns = table.get('columns', [])
        rows = table.get('rows', [])
        html = ['<table style="border-collapse: collapse; width: auto; margin: 8px 0;">']
        # Header row
        if columns:
            html.append('<tr>')
            for col in columns:
                html.append(
                    f'<th style="border: 1px solid #999; padding: 6px 12px; '
                    f'background: #e8e8e8; text-align: left;">'
                    f'{self._render_markup_text(str(col))}</th>'
                )
            html.append('</tr>')
        # Data rows
        for row in rows:
            html.append('<tr>')
            cells = row if isinstance(row, list) else [row]
            for cell in cells:
                html.append(
                    f'<td style="border: 1px solid #ccc; padding: 6px 12px;">'
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
        return escaped
    
    def _error_html(self, message):
        """Render an error message."""
        return f'''<html><body style="font-family: sans-serif; margin: 20px;">
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
            from urllib.parse import unquote
            page_key = unquote(url[len("navigate://"):])
            self.browser._navigate_to(page_key)
        elif url.startswith("http://") or url.startswith("https://"):
            # External link - open in system browser
            import webbrowser
            webbrowser.open(url)
        else:
            # Treat as internal page key
            self.browser._navigate_to(url)


class NotesBrowser(wx.Frame):
    """Main browser window."""
    
    def __init__(self, data_source):
        """Initialize the browser."""
        wx.Frame.__init__(self, None, title="Notes Browser", size=(900, 600))
        
        # Set application icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notes_browser.png')
        if os.path.exists(icon_path):
            icon = wx.Icon(icon_path, wx.BITMAP_TYPE_PNG)
            self.SetIcon(icon)
        
        self.data_source = data_source
        self.renderer = NotesHTMLRenderer()
        self.history = []
        self.history_position = -1  # Start before any history
        self.current_page = None  # None so first navigation adds to history
        
        # Detect background brightness for text colour choices
        self._bg_is_dark = None  # resolved lazily after UI is created
        
        self._create_ui()
        self._navigate_to("")  # Start at root
    
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
        
        # Navigate menu (renamed from Edit - more appropriate)
        nav_menu = wx.Menu()
        back_item = nav_menu.Append(wx.ID_BACKWARD, "&Back\tAlt+Left", "Go back")
        forward_item = nav_menu.Append(wx.ID_FORWARD, "&Forward\tAlt+Right", "Go forward")
        nav_menu.AppendSeparator()
        home_item = nav_menu.Append(wx.ID_HOME, "&Home\tCtrl+H", "Go to root page")
        self.refresh_menu_item = nav_menu.Append(wx.ID_REFRESH, "&Refresh\tF5", "Refresh page")
        menubar.Append(nav_menu, "&Navigate")
        
        self.SetMenuBar(menubar)
        
        # Bind menu events
        self.Bind(wx.EVT_MENU, self._on_open, open_item)
        self.Bind(wx.EVT_MENU, self._on_exit, exit_item)
        self.Bind(wx.EVT_MENU, self._on_back, back_item)
        self.Bind(wx.EVT_MENU, self._on_forward, forward_item)
        self.Bind(wx.EVT_MENU, self._on_home, home_item)
        self.Bind(wx.EVT_MENU, self._on_refresh, self.refresh_menu_item)
        
        # Set up accelerator table for keys that don't work well as menu shortcuts
        accel_entries = [
            (wx.ACCEL_ALT, wx.WXK_LEFT, wx.ID_BACKWARD),
            (wx.ACCEL_ALT, wx.WXK_RIGHT, wx.ID_FORWARD),
            (wx.ACCEL_NORMAL, wx.WXK_F5, wx.ID_REFRESH),
            (wx.ACCEL_CTRL, ord('O'), wx.ID_OPEN),
            (wx.ACCEL_CTRL, ord('Q'), wx.ID_EXIT),
            (wx.ACCEL_CTRL, ord('H'), wx.ID_HOME),
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
        
        # HTML viewer - use HtmlWindow (always available, unlike WebView)
        self.html = NotesHtmlWindow(panel, self)
        
        sizer.Add(self.html, 1, wx.EXPAND)
        
        panel.SetSizer(sizer)
        
        # Status bar removed
    
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
    
    def _navigate_to(self, page_key, add_to_history=True):
        """Navigate to a specific page."""
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
        data = self.data_source.read(page_key)
        html = self.renderer.render(page_key, data)
        
        # Load HTML into viewer
        self.html.SetPage(html)
        
        # Status bar update removed
    
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
        self._navigate_to(self.current_page)
    
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
    
    args = parser.parse_args()
    
    # Determine data source
    url = args.url or os.getenv('NOTES_URL')
    gdbm_file = args.gdbm_file or os.getenv('GDBM_FILE')
    
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
    frame = NotesBrowser(data_source)
    frame.Show()
    app.MainLoop()
    data_source.close()


if __name__ == '__main__':
    main()
