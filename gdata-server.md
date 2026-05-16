# gdata-server

gdata-server is a simple HTTP API for storing and retrieving JSONHTL documents in a GDBM database. It is the backend for the notes browser.

## Starting the server

```bash
python gdata_server.py
```

By default this starts an HTTP server on `127.0.0.1:8020` using a GDBM file named `.gdbm` in the current directory.

```bash
./start_notes_server.sh
```

Uses the production database and port 8021 (defined by `.gdata_server.yaml`).

## Configuration

Configuration is loaded in this order of precedence:

1. YAML config file specified by `GDATA_SERVER_CONFIG` environment variable
2. `.gdata_server.yaml` in the current directory (if present)
3. Individual environment variables (`GDBM_FILE`, `GDATA_SERVER_PORT`, `GDATA_SERVER_HOST`)
4. Built-in defaults

Example `.gdata_server.yaml`:

```yaml
gdbm_file: /home/user/notes.gdbm
gdata_server_port: 8021
gdata_server_host: 127.0.0.1
```

| Setting | Default | Description |
|---|---|---|
| `gdbm_file` | `.gdbm` | Path to the GDBM database file |
| `gdata_server_port` | `8020` | TCP port to listen on |
| `gdata_server_host` | `127.0.0.1` | Interface to bind (use `0.0.0.0` for all interfaces) |


## HTTP API

All documents are stored as JSONHTL — JSON objects accessible by a plain string key. The URL path encodes the key: `/my/doc/key` addresses the key `my/doc/key`. Keys are URL-decoded, so spaces and special characters are supported.

### GET /{key}

Retrieve a document.

```
GET /my/note HTTP/1.1
```

Returns the raw JSON body with `Content-Type: application/json`. Returns `404` if the key does not exist.

If the document has revision tracking, the response includes an `ETag` header with the current revision string (e.g. `"r3"`). This can be used with `If-Match` on subsequent PUT or PATCH requests.

### PUT /{key}

Store a document. The request body must be a valid JSON object.

```
PUT /my/note HTTP/1.1
Content-Type: application/json

{"title": "My Note", "content": "Hello."}
```

Returns `{"status": "ok"}` on success.

To use optimistic concurrency, include an `If-Match` header with the current revision. The server will reject the write with `412 Precondition Failed` if the revision has changed.

### DELETE /{key}

Delete a document and its associated metadata.

Returns `{"status": "deleted"}` on success, `404` if the key does not exist.

### HEAD /{key}

Check whether a key exists. Returns `200` if it does, `404` if it does not. No body.

### POST / (bulk operations)

```
POST / HTTP/1.1
Content-Type: application/json

{"op": "keys"}
```

| `op` | Returns | Description |
|---|---|---|
| `keys` | `{"keys": [...]}` | Sorted list of all keys (excluding internal metadata) |
| `dump` | `{"items": {...}}` | All key/value pairs as a JSON object |
| `flush` | `{"status": "flushed"}` | Sync the GDBM file to disk |
| `stop` | `{"status": "stopping"}` | Gracefully shut down the server |

### POST /{key} (patch operations)

Apply a structural patch to an existing JSONHTL document. The body is a JSON object with an `op` field.

```
POST /my/note HTTP/1.1
Content-Type: application/json

{"op": "append_block", "block": {"para": ["New paragraph."]}}
```

An optional `If-Match` header enables optimistic concurrency (same as PUT).

#### Block operations

Blocks are the top-level items in a document's `content` list. Each block has a stable `block_id` assigned by the server. Use `GET` then read the `ETag` and call the patch endpoint with the IDs returned from the response body — the block IDs are stored in a sidecar alongside the document.

To get block IDs along with the document body, POST with `op: get_with_block_ids`:

```json
{"op": "get_with_block_ids"}
```

Response:

```json
{"document": {...}, "block_ids": ["b-abc123", "b-def456"]}
```

| `op` | Required fields | Description |
|---|---|---|
| `get_with_block_ids` | — | Return document with its block ID list |
| `patch_meta` | `fields` (object) | Update top-level document keys (not `content`) |
| `append_block` | `block` | Add a new block at the end of `content` |
| `insert_block` | `index`, `block` | Insert before the block at position `index` |
| `replace_block` | `index` or `block_id`, `block` | Replace a block by position or ID |
| `delete_block` | `index` or `block_id` | Delete a block by position or ID |
| `delete_blocks` | `indices` (list) | Delete multiple blocks by position |
| `insert_before` | `block_id`, `block` | Insert a new block before the given block ID |
| `insert_after` | `block_id`, `block` | Insert a new block after the given block ID |

#### Table operations

Table blocks support structured edits via `op: table.*`. All table operations require a `block_id` identifying which table block to operate on.

| `op` | Required fields | Description |
|---|---|---|
| `table.insert_column` | `block_id`, `name`, `position` | Insert a new column |
| `table.delete_column` | `block_id`, `column` | Delete a column by name or index |
| `table.rename_column` | `block_id`, `column`, `name` | Rename a column |
| `table.move_column` | `block_id`, `column`, `to` or `after` | Move a column to a new position |
| `table.reorder_columns` | `block_id`, `order` | Reorder all columns by providing the new order list |
| `table.set_columns` | `block_id`, `columns` | Rename all columns at once |
| `table.fill_column` | `block_id`, `column`, `value` | Fill every cell in a column with a value |
| `table.insert_row` | `block_id`, `position`, `values?` | Insert a new row |
| `table.append_row` | `block_id`, `values?` | Append a new row at the end |
| `table.delete_row` | `block_id`, `row` | Delete a row by index |
| `table.move_row` | `block_id`, `row`, `to` | Move a row to a new position |
| `table.fill_row` | `block_id`, `row`, `value` | Fill every cell in a row with a value |
| `table.set_cell` | `block_id`, `row`, `column`, `value` | Set one cell value |
| `table.sort` | `block_id`, `by`, `ascending?` | Sort rows by one or more columns |
| `table.set_caption` | `block_id`, `caption` | Set or clear the table caption |
| `table.set_index` | `block_id`, `column` | Mark a column as the index column for row lookup |
| `table.clear_index` | `block_id` | Remove the index column designation |
| `table.transpose` | `block_id` | Transpose the table (rows become columns) |
| `table.replace` | `block_id`, `old_value`, `new_value`, `column?` | Replace a cell value throughout the table or in one column |


## Error responses

All errors return an appropriate HTTP status code and a JSON body with a `detail` field:

```json
{"detail": "key not found: my/note"}
```

Common status codes:

| Code | Meaning |
|---|---|
| `404` | Key not found |
| `409` | Conflict (e.g. patching a document without a `content` list) |
| `412` | Precondition failed (revision mismatch) |
| `400` | Bad request (invalid op, missing fields, etc.) |
