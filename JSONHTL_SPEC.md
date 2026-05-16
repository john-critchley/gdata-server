# JSONHTL - JSON Hypertext Language

**Version 0.1 — Draft**

JSONHTL is a minimal document format encoded as JSON. It is designed to be trivially parseable by LLMs and straightforwardly convertible to HTML or other presentation formats by simple renderers.

JSONHTL defines only primitive building blocks. Higher-level conventions (document metadata, node hierarchies, runnable documents, etc.) are defined in separate convention documents linked from the root node of a given system.


## Document Structure

A JSONHTL document is a JSON object. The only required key is `content`.

```json
{
  "title": "Example Document",
  "content": [...]
}
```

`title` is optional but conventional. Any other top-level keys are permitted; their meaning is defined by convention documents, not this spec. Renderers and consumers should ignore keys they do not recognise.


## Content

`content` is either a **string** (shorthand for a single paragraph of plain text) or a **list of block elements**. When it is a list, each block is a JSON object with a single identifying key.

A string value:

```json
{"title": "Quick note", "content": "Remember to update the config."}
```

is equivalent to:

```json
{"title": "Quick note", "content": [{"para": ["Remember to update the config."]}]}
```


### Block Elements

#### para

A paragraph. Contains a list of **inline elements**.

```json
{"para": ["This is plain text with a ", {"link": {"href": "other-node", "text": "link"}}, " in it."]}
```

#### heading

A section heading. Contains `level` (integer, 1–6) and `text` (string).

```json
{"heading": {"level": 1, "text": "Introduction"}}
```

#### codeblock

A block of code or preformatted text. Contains `lang` (string, may be empty) and `body` (string).

```json
{"codeblock": {"lang": "python", "body": "print('hello')"}}
```

Additional keys on a codeblock (e.g. `name`, `exec`) are not defined by this spec but may be defined by convention documents.

#### list

A labelled or unlabelled list of items. Contains `label` (string, optional), `ordered` (boolean, optional, default false), and `items` (list).

```json
{"list": {"label": "issues", "ordered": false, "items": [
  "First item",
  ["inline ", {"code": "elements"}, " are also valid"],
  {"id": "foo", "title": "An item as a key-value object"}
]}}
```

Each item may be:
- A **string** — plain text (markdown inline formatting permitted).
- A **list of inline elements** — same content model as `para`.
- An **object** — arbitrary key-value pairs; renderers display these as `key: value` entries within the list item.

`label`, if present, is rendered as a visible heading or prefix immediately before the list. `ordered` selects `<ol>` (true) or `<ul>` (false).

#### writable_note

A writable text input area for loading and persisting external data. Begins blank and accepts text input via GUI, API, or programmatic writes. Contains `notename` (string) and optionally `placeholder` (string).

```json
{"writable_note": {"notename": "user_input", "placeholder": "Enter your notes here..."}}
```

**Behaviour:**

- **Display:** Text area with a "Save" button below it.
- **Edit:** User/API/MCP can write text into the area.
- **Save:** On button click or API invocation, text is POSTed to `/writeback/<safe_notename>`.
- **Post-save:** Text area is cleared to placeholder state. User sees success/error response.
- **Timestamp:** Server appends ISO 8601 timestamp to saved file. If two saves occur within the same second, the second overwrites the first (humans cannot paste multiple documents and POST that rapidly).

**Notename validation:**

A valid `notename` matches the regex: `^[a-zA-Z0-9_-]+$` (alphanumeric, underscore, hyphen). Slashes in document keys (e.g., `serscr/roundtrip-test`) are replaced with `__` in filenames by the renderer (`serscr__roundtrip-test`).

**Keys:**

| Key | Type | Description |
|---|---|---|
| `notename` | string | Identifier for this writable area. Must match `[a-zA-Z0-9_-]+`. Used to construct POST endpoint and filename. |
| `placeholder` | string | Optional. Text displayed in textarea when empty. |

**POST request format:**

Renderer POSTs to `/writeback/<safe_notename>` with text content:

```
POST /writeback/user_input

Content-Type: application/json
{"text": "content here"}
```

**File storage (server-side):**

- **Browser context:** Saves to `writeback/<safe_notename>_<timestamp>.txt` relative to the browser location.
- **Web server context:** Saves to `<WEBDAV_ROOT>/writeback/<safe_notename>_<timestamp>.txt`.

Where `<timestamp>` is ISO 8601 format (e.g., `2026-05-05T15:30:42Z`). Same-second overwrites are intentional. WebDAV integration allows external tools to access saved files.

**Server response (success):**

```json
{
  "status": "ok",
  "saved": true,
  "notename": "user_input",
  "timestamp": "2026-05-05T15:30:42Z",
  "path": "writeback/user_input_2026-05-05T15:30:42Z.txt"
}
```

**Server response (error):**

```json
{
  "status": "error",
  "message": "Invalid notename: contains forbidden characters"
}
```

**Security:**

- Notename must match regex; no path traversal possible.
- `/` replaced with `__`; directory traversal prevented.
- Timestamp prevents rapid re-posts from creating multiple files.
- Server validates notename before write.

## Inline Elements

Inline elements appear inside `para` lists. An inline element is either a **string** (plain text) or an **object** with a single identifying key.

#### Plain text

A bare JSON string.

```json
"This is just text."
```

#### link

A hypertext reference. Contains `href` (string) and `text` (string).

```json
{"link": {"href": "getting-started", "text": "Getting Started"}}
```

**href resolution:**

- Starts with `http://` or `https://` — external web link.
- Anything else — a key in the local data store. The storage layer defines how keys map to documents; JSONHTL does not impose hierarchy or path semantics on keys.

Note: the behaviour when following an external link (e.g. whether the target is another JSONHTL store, a web page, or something else) is not defined by this version of the spec and is reserved for future work.

#### code

Inline code. Contains a string.

```json
{"code": "gdata_server.py"}
```


## General Rules

1. **Lists or scalars.** Where this spec defines a value as a list, a bare scalar (string, integer) is also acceptable as shorthand for a single-element list. Parsers should normalise to list form internally. For example, `{"para": "just text"}` is equivalent to `{"para": ["just text"]}`.

2. **Unknown keys are ignored.** Blocks, inline elements, and top-level keys that a consumer does not recognise should be silently skipped. This allows convention documents to extend the format without breaking basic renderers.

3. **No presentation markup.** JSONHTL does not define emphasis, bold, underline, font size, or colour. Content is semantic. Presentation is the renderer's concern.

4. **Convention documents over spec changes.** New element types, metadata schemas, and structural conventions should be defined in convention documents stored within the system and linked from the root node — not by extending this spec.


## Root Node Convention

When JSONHTL documents are stored in a key-value system, the root node (typically the empty-string key `""`) serves as a bootstrap. It should:

- Use only base JSONHTL elements (as defined in this spec) so any consumer can read it without prior knowledge.
- Briefly describe the link/navigation model.
- Link to convention documents that define any extended keys, metadata schemas, or organisational structures used in this particular system.

An LLM or tool encountering the system for the first time reads the root node, follows only the links it needs, and stops. This keeps context window usage minimal.


## Runnable Documents

A JSONHTL document can be marked as **runnable** by setting `"runnable": true` at the top level. A runnable document contains one or more executable code cells alongside ordinary prose content.

```json
{
  "title": "My Sheet",
  "runnable": true,
  "content": [...]
}
```

Renderers that support runnable documents (such as the notes-browser) display these in a notebook-style view with per-cell Run buttons, shared execution state, and captured output areas. Renderers that do not support runnable documents may ignore the `runnable` flag and display the document as plain prose.

### Executable codeblocks

Within a runnable document, a `codeblock` is made executable by adding `"exec": true`. Only executable codeblocks are run; non-executable codeblocks are displayed as read-only reference code.

```json
{"codeblock": {"lang": "python", "exec": true, "name": "setup", "body": "value = 42"}}
```

Additional keys used in executable codeblocks:

| Key | Type | Description |
|---|---|---|
| `exec` | boolean | `true` marks this codeblock as executable |
| `name` | string | Stable identifier for the cell. Used as the cell key in RPC calls and state tracking. Must be unique within the document. If absent or duplicated, the cell's position index is used instead. |
| `lang` | string | Language tag. Currently only `python`, `py`, or empty string are executed. |
| `body` | string | Source code to run. |

### Cell identity and input detection

When the browser loads a runnable document, it scans each executable cell for `input()` calls using AST analysis. Any detected calls generate corresponding input fields in the UI and in the control socket API. Input values are ordered by occurrence within the cell; cell N's first `input()` is field `name/0`, the second is `name/1`, and so on.

### Execution model

All cells share a single Python namespace for the lifetime of the sheet session. Variables defined in one cell are visible to later cells. Execution order within a run-all sequence follows document order. Restarting the kernel clears the shared namespace; clearing outputs does not.

### Example runnable document

```json
{
  "title": "Quick Calculation",
  "runnable": true,
  "content": [
    {"heading": {"level": 1, "text": "Quick Calculation"}},
    {"para": ["Define a base value, then compute results."]},
    {"codeblock": {"lang": "python", "exec": true, "name": "setup",
      "body": "value = 42\nprint('value set to', value)"}},
    {"para": ["Compute a result using the base value."]},
    {"codeblock": {"lang": "python", "exec": true, "name": "compute",
      "body": "print('double:', value * 2)"}}
  ]
}
```


## Example Document

```json
{
  "title": "Project Overview",
  "content": [
    {"heading": {"level": 1, "text": "gdata-server"}},
    {"para": [
      "A FastAPI-based HTTP API for GDBM databases. See ",
      {"link": {"href": "gdata-server/api", "text": "API documentation"}},
      " for endpoint details."
    ]},
    {"para": [
      "Configuration is handled via ",
      {"code": ".gdata_server.yaml"},
      " or environment variables."
    ]},
    {"codeblock": {"lang": "bash", "body": "uvicorn gdata_server:app --host 127.0.0.1 --port 8020"}}
  ]
}
```


## Example Root Node

```json
{
  "title": "Notes System",
  "content": [
    {"heading": {"level": 1, "text": "Welcome"}},
    {"para": [
      "This is a JSONHTL document store. Each node is a JSON document keyed by a plain string. Links between nodes use the ",
      {"code": "link"},
      " element with the key as the ",
      {"code": "href"},
      " value."
    ]},
    {"heading": {"level": 2, "text": "Conventions"}},
    {"para": [
      "This system uses extended conventions documented here:"
    ]},
    {"para": [
      {"link": {"href": "conventions/metadata", "text": "Metadata keys"}},
      " — describes ",
      {"code": "created"},
      ", ",
      {"code": "type"},
      ", ",
      {"code": "tags"},
      " and other node-level keys."
    ]},
    {"para": [
      {"link": {"href": "conventions/hierarchy", "text": "Hierarchy conventions"}},
      " — how nodes are organised and navigated."
    ]},
    {"para": [
      {"link": {"href": "conventions/runnable", "text": "Runnable documents"}},
      " — extended codeblock keys for executable content."
    ]}
  ]
}
```
