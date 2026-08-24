# Email Tools for Misc MCP Connector - Summary

## What Was Created

An MCP email connector integrated into the existing `misc_mcp_server.py` that provides five email management tools for testing and developing the envoy orchestrator.

## File Changed

**Location:** `/home/john/py/gdata-server/misc_mcp_server.py`

**Changes:**
- Added imports for email functionality (smtplib, EmailMessage, BytesParser, etc.)
- Imported IMAPClient from envoy project
- Added 5 async helper functions for email operations
- Added 5 new tools to the MCP tool list
- Added tool handlers in the call_tool() function

**Lines Added:** 252

## New Tools

### 1. email_send
Send an email to the local mail server via Brevo SMTP.
- **Parameters:** to, subject, body, optional headers dict
- **Returns:** JSON with status and message-id
- **Use Case:** Inject test emails into envoy's inbox for testing

### 2. email_list
List emails in a mailbox folder with basic headers.
- **Parameters:** folder (default INBOX), search_criteria (default ALL), max_results (default 10)
- **Returns:** JSON with email list, From/To/Subject/Date/Message-ID
- **Use Case:** Find and monitor emails in mailbox

### 3. email_read
Read the complete email including full body text and all headers.
- **Parameters:** seq_id (required), folder (default INBOX)
- **Returns:** JSON with full headers dict, body text, flags
- **Use Case:** Retrieve complete email details for verification

### 4. email_list_folders
List all available mailbox folders.
- **Parameters:** None
- **Returns:** JSON with folder list and count
- **Use Case:** Discover available mailboxes

## Code Reuse

The implementation leverages existing code from the envoy project:
- **IMAPClient**: Connection management, folder operations, email fetching
- **Email parsing**: BytesParser and email.policy for standards-compliant parsing
- **SMTP**: Brevo SMTP relay for sending

This avoids code duplication and maintains consistency with envoy's email handling.

## Testing Workflow Enabled

1. **Direct Email Injection** - Send test emails without external mail client
2. **Email Monitoring** - List and read emails programmatically
3. **Complete Testing Loop** - From injection through response verification
4. **Orchestrator Testing** - Test envoy's entire email-driven workflow from Claude Code UI

## Security Considerations

- SMTP credentials come from environment variable: `BREVO_SMTP_PASSWORD`
- IMAP credentials read from `~/.netrc` (standard Unix convention)
- From address fixed to `envoy@critchley.biz` to prevent spoofing
- Search parameters pass through to IMAP untouched (follows IMAP standard)
- Body is decoded with UTF-8 error handling for safe parsing

## JSON Response Format

All tools return JSON for easy parsing:
- Structured data (headers, email lists)
- Error responses with descriptive messages
- Consistent schema across all tools

## Integration with Claude Code UI

The email tools are now accessible in Claude Code when using the misc MCP connector:
- Discoverable via tool listing
- Full parameter documentation in tool descriptions
- JSON responses easy to parse and display
- Can be chained with other tools for workflows

## Example Usage in Claude Code

```
Send a test email to envoy asking it to write a poem about technology
```

This will:
1. Compose email with proper headers
2. Send via Brevo SMTP
3. Return message-id for tracking

Then:
```
List emails from envoy to see responses
```

And:
```
Read the response email to verify orchestrator processing
```

## Files

- **Modified:** `/home/john/py/gdata-server/misc_mcp_server.py` (252 lines added)
- **Documentation:** `/home/john/py/gdata-server/EMAIL_TOOLS_USAGE.md`
- **Commits:**
  - `21b8321` - Add email tools to misc MCP connector
  - `fa6da3b` - Add documentation for email tools MCP connector

## Next Steps

To use the email tools:

1. Ensure the misc_mcp_server is running with email tools
2. Connect Claude Code to the misc MCP connector
3. Follow the testing workflow in EMAIL_TOOLS_USAGE.md

## Notes

- The tools work with the local mail server configured at `mail.critchley.biz`
- Both IMAP (reading) and SMTP (sending) functionality are supported
- Headers can be set on outgoing emails (except auto-generated fields)
- Email body text is preserved for both plain text and multipart messages
