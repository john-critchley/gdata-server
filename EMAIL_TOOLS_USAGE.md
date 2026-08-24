# Email Tools Usage Guide

The misc MCP connector now includes email tools for testing and integration with the envoy orchestrator. These tools provide read/write access to the local mail server via IMAP and SMTP.

## Overview

- **email_send**: Compose and send an email
- **email_list**: List emails in a folder with headers
- **email_read**: Read a complete email including body and all headers
- **email_list_folders**: List all mailbox folders

## Tools Reference

### email_send

Send an email to the local mail server.

**Parameters:**
- `to` (string, required): Email recipient address
- `subject` (string, required): Email subject line
- `body` (string, required): Email body text
- `headers` (object, optional): Additional email headers as key-value pairs
  - Do not include: From, To, Subject, Date, Message-ID (these are auto-generated)

**Response:** JSON object with status and message-id
```json
{
  "status": "sent",
  "to": "user@example.com",
  "subject": "Test Email",
  "message_id": "<178759908176.759915...@critchley.biz>"
}
```

**Example:**
```
Compose a test email to john@example.com with subject "Testing email tools" 
and body "This is a test email from the MCP connector"
```

### email_list

List emails in a mailbox folder with header information.

**Parameters:**
- `folder` (string, optional): Mailbox folder name. Default: "INBOX"
- `search_criteria` (string, optional): IMAP search criteria. Default: "ALL"
  - Examples: "UNSEEN", "SEEN", "FROM user@example.com", "SUBJECT test"
- `max_results` (integer, optional): Maximum results to return. Default: 10

**Response:** JSON object with folder info and email list
```json
{
  "folder": "INBOX",
  "count": 3,
  "emails": [
    {
      "seq": "23",
      "from": "sender@example.com",
      "to": "recipient@example.com",
      "subject": "Test Email",
      "date": "Mon, 24 Aug 2026 20:01:57 +0100",
      "message_id": "<178759811732.755049...@critchley.biz>",
      "flags": "23 (FLAGS (\\Seen)) ..."
    }
  ]
}
```

**Example:**
```
List the 5 most recent unseen emails in INBOX
```

### email_read

Read a complete email including body text and all headers.

**Parameters:**
- `seq_id` (string, required): Email sequence ID (from email_list)
- `folder` (string, optional): Mailbox folder name. Default: "INBOX"

**Response:** JSON object with full email details
```json
{
  "seq": "23",
  "folder": "INBOX",
  "headers": {
    "From": "sender@example.com",
    "To": "recipient@example.com",
    "Subject": "Test Email",
    "Date": "Mon, 24 Aug 2026 20:01:57 +0100",
    "Message-ID": "<178759811732.755049...@critchley.biz>",
    "Content-Type": "text/plain; charset=utf-8"
  },
  "body": "This is the email body text",
  "flags": "23 (FLAGS (\\Seen)) ..."
}
```

**Example:**
```
Read the full email with seq_id "23" from INBOX
```

### email_list_folders

List all available mailbox folders on the server.

**Parameters:** None

**Response:** JSON object with folder list
```json
{
  "status": "success",
  "folders": ["INBOX", "Sent", "Done", "Drafts"],
  "count": 4
}
```

**Example:**
```
What mailbox folders are available?
```

## Testing Workflow

1. **List folders** to see available mailboxes
   ```
   Use email_list_folders to see what folders exist
   ```

2. **Send a test email**
   ```
   Use email_send to inject a test email into the system
   ```

3. **List emails**
   ```
   Use email_list to find the email you just sent
   ```

4. **Read full email**
   ```
   Use email_read with the seq_id from the list to read the complete email
   ```

## Integration with Envoy

To test envoy's orchestrator with these tools:

1. **Inject a request email** using `email_send`
   - Send to: `envoy@critchley.biz`
   - Subject: Your request (e.g., "Write a poem")
   - Body: Detailed instructions

2. **List emails** to verify injection
   ```
   Use email_list with search_criteria "FROM envoy_test@critchley.biz"
   ```

3. **Monitor for responses** after running orchestrator
   ```
   List emails in "Sent" folder to see envoy's responses
   ```

4. **Read responses** to verify orchestrator processed your request
   ```
   Use email_read to get full response details
   ```

## Notes

- All responses are returned as JSON for easy parsing and integration
- Headers are returned as a dictionary for structured access
- The `seq_id` from `email_list` is used as a reference in `email_read`
- Folder names are case-sensitive and depend on your mail server setup
- The From address is automatically set to `envoy@critchley.biz`
