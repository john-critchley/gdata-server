#!/usr/bin/env python3
"""
IMAP client interface for Envoy email agent.

Provides simple, reliable IMAP operations using standard library imaplib.
Credentials are read from ~/.netrc using the netrc module.
"""

import imaplib
import netrc
import socket
from typing import Optional, List, Tuple


class IMAPClient:
    """
    Simple IMAP client interface.
    
    Uses .netrc for credentials. Add an entry like:
        machine imap.example.com
        login user@example.com
        password yourpassword
    """
    
    def __init__(self, host: str, use_ssl: bool = True):
        """
        Initialize IMAP client.
        
        Args:
            host: IMAP server hostname
            use_ssl: Whether to use SSL/TLS (default True)
        """
        self.host = host
        self.use_ssl = use_ssl
        self.connection: Optional[imaplib.IMAP4_SSL] = None
        self.current_folder: Optional[str] = None
        self._login_from_netrc()
    
    def _login_from_netrc(self):
        """
        Read credentials from ~/.netrc and establish connection.
        
        Raises:
            netrc.NetrcParseError: If .netrc is malformed
            FileNotFoundError: If .netrc doesn't exist
            KeyError: If host not found in .netrc
            imaplib.IMAP4.error: If connection or login fails
        """
        nrc = netrc.netrc()
        auth = nrc.authenticators(self.host)
        
        if not auth:
            raise KeyError(f"No credentials found in .netrc for {self.host}")
        
        login, account, password = auth
        
        # Establish connection
        if self.use_ssl:
            self.connection = imaplib.IMAP4_SSL(self.host)
        else:
            self.connection = imaplib.IMAP4(self.host)
        
        # Login
        self.connection.login(login, password)
    
    def list_folders(self) -> List[str]:
        """
        List all folders in the mailbox.
        
        Returns:
            List of folder names
        """
        status, folders = self.connection.list()
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to list folders: {status}")
        
        # Parse folder names from IMAP LIST response
        # Format: (flags) "delimiter" name  — name may or may not be quoted
        folder_names = []
        for folder in folders:
            line = folder.decode('utf-8')
            # Split on ") " to skip flags, then find name after delimiter
            after_flags = line.split(') ', 1)
            if len(after_flags) == 2:
                # Remainder is: "delimiter" name
                remainder = after_flags[1]
                # Skip the quoted delimiter and space
                parts = remainder.split(' ', 1)
                if len(parts) == 2:
                    name = parts[1].strip('"')
                    folder_names.append(name)

        return folder_names
    
    def select_folder(self, folder: str = 'INBOX') -> Tuple[str, int]:
        """
        Select a folder for operations.
        
        Args:
            folder: Folder name (default 'INBOX')
        
        Returns:
            Tuple of (status, message_count)
        """
        status, data = self.connection.select(folder)
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to select folder {folder}: {status}")
        self.current_folder = folder
        message_count = int(data[0])
        return status, message_count
    
    def search(self, criteria: str = 'ALL') -> List[bytes]:
        """
        Search for messages in the selected folder.
        
        Args:
            criteria: IMAP search criteria (default 'ALL')
                     Examples: 'UNSEEN', 'FROM "sender@example.com"', 'SUBJECT "topic"'
        
        Returns:
            List of message IDs (as bytes)
        """
        status, data = self.connection.search(None, criteria)
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Search failed: {status}")
        
        # data[0] is a space-separated list of message IDs
        message_ids = data[0].split()
        return message_ids
    
    def fetch(self, message_id: bytes, parts: str = '(RFC822)') -> bytes:
        """
        Fetch a message by ID.
        
        Args:
            message_id: Message ID from search()
            parts: What to fetch (default '(RFC822)' for full message)
                   Examples: '(BODY[HEADER])', '(FLAGS)', '(BODY[TEXT])'
        
        Returns:
            Message data as bytes
        """
        status, data = self.connection.fetch(message_id, parts)
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Fetch failed: {status}")
        for item in data:
            if isinstance(item, tuple):
                return item[1]
        raise imaplib.IMAP4.error(f"No RFC822 body in fetch response for message {message_id!r}: {repr(data)[:200]}")
    
    def create_folder(self, folder_name: str):
        """
        Create a new folder.

        Args:
            folder_name: Name of the folder to create
        """
        status, _ = self.connection.create(folder_name)
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to create folder {folder_name}")

    def move_message(self, message_id: bytes, destination_folder: str):
        """
        Move a message to another folder.

        Args:
            message_id: Message ID from search()
            destination_folder: Target folder name
        """
        # Copy to destination
        status, _ = self.connection.copy(message_id, destination_folder)
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to copy message to {destination_folder}")

        # Mark as deleted in current folder
        status, _ = self.connection.store(message_id, '+FLAGS', '\\Deleted')
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to mark message as deleted")
    
    def delete_message(self, message_id: bytes):
        """
        Mark a message for deletion.
        
        Note: Message is only marked, not actually deleted until expunge() is called.
        
        Args:
            message_id: Message ID from search()
        """
        status, _ = self.connection.store(message_id, '+FLAGS', '\\Deleted')
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to mark message as deleted")

    def set_flags(self, message_id: bytes, flags: List[str]):
        """
        Add one or more IMAP flags/keywords to a message.

        Args:
            message_id: Message ID from search()
            flags: List of IMAP flags/keywords (e.g. ['\\Seen'], ['$EnvoyAnalysed'])
        """
        if not flags:
            return
        flag_list = ' '.join(flags)
        status, _ = self.connection.store(message_id, '+FLAGS', f'({flag_list})')
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to set flags {flags} on message {message_id!r}")

    def clear_flags(self, message_id: bytes, flags: List[str]):
        """
        Remove one or more IMAP flags/keywords from a message.

        Args:
            message_id: Message ID from search()
            flags: List of IMAP flags/keywords (e.g. ['\\Seen'], ['$EnvoyAnalysed'])
        """
        if not flags:
            return
        flag_list = ' '.join(flags)
        status, _ = self.connection.store(message_id, '-FLAGS', f'({flag_list})')
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to clear flags {flags} on message {message_id!r}")
    
    def append(self, folder: str, message_bytes: bytes):
        """
        Append a message to a folder.

        Args:
            folder: Target folder name
            message_bytes: Raw RFC822 message bytes
        """
        status, _ = self.connection.append(folder, '', None, message_bytes)
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Failed to append to {folder}")

    def expunge(self):
        """
        Permanently delete all messages marked with \\Deleted flag.
        """
        status, _ = self.connection.expunge()
        if status != 'OK':
            raise imaplib.IMAP4.error(f"Expunge failed: {status}")
    
    def idle_start(self) -> None:
        """
        Issue the IMAP IDLE command.  The folder must already be selected.
        Blocks until the server sends its '+ idling' continuation response.
        Call idle_check() next to wait for notifications, then idle_done() to exit.
        """
        tag = self.connection._new_tag()
        if isinstance(tag, bytes):
            self._idle_tag = tag
        else:
            self._idle_tag = tag.encode()
        self.connection.send(self._idle_tag + b' IDLE\r\n')
        # Wait for '+ idling' continuation line
        while True:
            line = self.connection.readline()
            if not line:
                raise ConnectionError("IDLE not accepted: server closed connection")
            if line.startswith(b'+'):
                return
            # Server may send untagged OK lines before the continuation; ignore them.
            if line.startswith(self._idle_tag):
                raise imaplib.IMAP4.error(f"IDLE rejected: {line!r}")

    def idle_check(self, timeout: float = 1680.0) -> bool:
        """
        Block until the server signals new mail or the timeout expires.

        Must be called after idle_start().  Does NOT exit IDLE — call idle_done()
        afterwards regardless of the return value.

        Args:
            timeout: Seconds to wait before giving up (default 28 min, safely
                     under the typical 30-min server IDLE timeout).

        Returns:
            True  — server sent EXISTS or RECENT (new mail arrived)
            False — timeout elapsed with no notification
        """
        sock = self.connection.socket()
        old_timeout = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            while True:
                line = self.connection.readline()
                if not line:
                    raise ConnectionError("Server closed connection during IDLE")
                if b'EXISTS' in line or b'RECENT' in line:
                    return True
                if b'BYE' in line:
                    raise ConnectionError(f"Server sent BYE during IDLE: {line!r}")
                # Other untagged responses (OK, FLAGS, etc.) — ignore and keep waiting
        except (socket.timeout, TimeoutError):
            return False
        finally:
            sock.settimeout(old_timeout)

    def idle_done(self) -> None:
        """
        Exit IDLE by sending DONE and draining the server's tagged OK response.
        Safe to call even if idle_check() timed out.
        """
        try:
            self.connection.send(b'DONE\r\n')
            # Drain lines until we see the tagged OK (or any tagged response)
            while True:
                line = self.connection.readline()
                if not line:
                    break
                if line.startswith(self._idle_tag):
                    break
        except Exception:
            pass  # Best-effort; caller should reconnect on next loop if needed

    def close(self):
        """
        Close the selected folder and logout.
        """
        if self.connection:
            try:
                self.connection.close()
            except:
                pass  # May already be closed
            
            try:
                self.connection.logout()
            except:
                pass  # May already be logged out
            
            self.connection = None
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure cleanup."""
        self.close()
        return False


def main(host: str = 'localhost', folder: str = 'INBOX'):
    """
    Test IMAP connection and list messages.
    
    Args:
        host: IMAP server hostname
        folder: Folder to examine (default 'INBOX')
    """
    with IMAPClient(host) as client:
        print(f"Connected to {host}")
        
        folders = client.list_folders()
        print(f"\nAvailable folders: {folders}")
        
        status, count = client.select_folder(folder)
        print(f"\nSelected {folder}: {count} messages")
        
        if count > 0:
            message_ids = client.search('ALL')
            print(f"Found {len(message_ids)} messages")
            
            # Show first message as example
            if message_ids:
                msg_data = client.fetch(message_ids[0])
                print(f"\nFirst message preview (first 500 bytes):")
                print(msg_data[:500].decode('utf-8', errors='ignore'))


if __name__ == '__main__':
    import sys
    
    try:
        if len(sys.argv) > 1:
            main(host=sys.argv[1], folder=sys.argv[2] if len(sys.argv) > 2 else 'INBOX')
        else:
            print("Usage: imap_client.py <host> [folder]")
            print("\nCredentials must be in ~/.netrc:")
            print("  machine <host>")
            print("  login <username>")
            print("  password <password>")
            sys.exit(1)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Add credentials to ~/.netrc for the IMAP host", file=sys.stderr)
        sys.exit(1)
    except (netrc.NetrcParseError, FileNotFoundError) as e:
        print(f"Error reading .netrc: {e}", file=sys.stderr)
        sys.exit(1)
    except imaplib.IMAP4.error as e:
        print(f"IMAP error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        # Unexpected errors: let Python show full traceback
        raise
