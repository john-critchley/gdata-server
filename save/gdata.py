#!/usr/bin/python3
import json
import dbm.gnu as gdbm
import urllib.parse
import requests

class gdata_local_raw:
    """
    A base class for working with gdbm (GNU Database Manager) databases. 
    Provides basic dictionary-like access to a gdbm file.

    Args:
        gdbm_file (str): The filename of the gdbm database. Defaults to '.gdbm'.
        mode (str): The file mode used for opening the gdbm file. Defaults to 'c' 
                    (create if not existing).
        mask (int): File permissions mask for the gdbm file. Defaults to 0o600.
    
    Attributes:
        db: The gdbm database object.
        open (bool): A flag indicating whether the database is open.
    """
    
    def __init__(self, gdbm_file='.gdbm', mode='c', mask=0o600):
        self.db = gdbm.open(gdbm_file, mode, mask)
        self.open = True

    def __enter__(self):
        """Supports using the class as a context manager (with statement)."""
        return self

    def __exit__(self, *blah):
        """Closes the gdbm file when exiting a context manager block."""
        self.close()
        return False

    def close(self):
        """Closes the gdbm file."""
        self.db.close()
        self.open = False

    def __del__(self):
        """Ensures that the gdbm file is closed when the object is destroyed."""
        if 'open' in dir(self) and self.open:
            self.db.close()

    def __getitem__(self, key):
        """Retrieve an item by key, raises KeyError if not found."""
        return self.db[key]

    def __contains__(self, key):
        """Check if a key exists in the database."""
        return key in self.db

    def __setitem__(self, key, item):
        """Set an item in the database."""
        self.db[key] = item

    def __delitem__(self, key):
        """Delete an item from the database."""
        del self.db[key]

    def __iter__(self):
        """Prepare the database for iteration over keys."""
        self.current_item = None
        return self

    def __next__(self):
        """
        Iterate over the keys in the database.
        Raises StopIteration when all keys are iterated over.
        """
        self.current_item = self.db.firstkey() if self.current_item is None else self.db.nextkey(self.current_item)
        if self.current_item is None:
            raise StopIteration
        return self.current_item

    def get(self, key, default=None):
        """
        Get the value for a key. If the key doesn't exist, return the default value.
        """
        return self.__getitem__(key) if self.__contains__(key) else default

    def keys(self):
        """
        Return a set of all keys in the database.
        """
        return set(self)

    def items(self):
        """
        Return a generator of key-value pairs in the database.
        """
        return ((p, self[p]) for p in self)

    def __len__(self):
        """
        Return the number of items in the database.
        """
        return len(self.keys())

class gdata_local_simple(gdata_local_raw):
    """
    A subclass of gdata_local_raw for handling string data (UTF-8 encoded). 
    Provides automatic encoding/decoding of strings.
    """
    
    def __setitem__(self, key, item):
        """Store a string item with UTF-8 encoding."""
        super().__setitem__(key.encode('utf-8'), item.encode('utf-8'))

    def __getitem__(self, key):
        """Retrieve a string item and decode it from UTF-8."""
        return super().__getitem__(key.encode('utf-8')).decode('utf-8')

    def __contains__(self, key):
        """Check if a UTF-8 encoded key exists."""
        return super().__contains__(key.encode('utf-8'))

    def __delitem__(self, key):
        """Delete a UTF-8 encoded key."""
        super().__delitem__(key.encode('utf-8'))

    def __next__(self):
        """Iterate over keys and decode them from UTF-8."""
        return super().__next__().decode('utf-8')

    def __str__(self):
        """Return a string representation of all keys in the database."""
        return ','.join(list(self.keys()))

class gdata_local(gdata_local_simple):
    """
    A subclass of gdata_local_simple that stores and retrieves data as JSON.
    This allows for storing structured data.
    """
    
    def __setitem__(self, key, item):
        """Store the item as a JSON-encoded string."""
        super().__setitem__(key, json.dumps(item))

    def __getitem__(self, key):
        """Retrieve the item as a decoded JSON object."""
        return json.loads(super().__getitem__(key))


class gdata_http_simple:
    """
    HTTP client for gdata-server - similar interface to gdata_local_simple.
    Works with JSON strings over HTTP.
    
    Args:
        url (str): Base URL of the gdata-server (e.g., 'http://localhost:8020')
    """
    
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.open = True
    
    def __enter__(self):
        """Supports using the class as a context manager (with statement)."""
        return self
    
    def __exit__(self, *blah):
        """Closes the connection when exiting a context manager block."""
        self.close()
        return False
    
    def close(self):
        """Closes the connection."""
        self.open = False
    
    def __getitem__(self, key):
        """Retrieve a JSON string via HTTP GET, raises KeyError if not found."""
        quoted_key = urllib.parse.quote(key, safe='')
        response = requests.get(f"{self.url}/{quoted_key}")
        if response.status_code == 404:
            raise KeyError(key)
        response.raise_for_status()
        # Server returns raw JSON string as response body
        return response.text
    
    def __setitem__(self, key, json_string):
        """Store a JSON string via HTTP PUT."""
        quoted_key = urllib.parse.quote(key, safe='')
        response = requests.put(
            f"{self.url}/{quoted_key}",
            data=json_string,
            headers={'Content-Type': 'application/json'}
        )
        response.raise_for_status()
    
    def __delitem__(self, key):
        """Delete a key via HTTP DELETE."""
        quoted_key = urllib.parse.quote(key, safe='')
        response = requests.delete(f"{self.url}/{quoted_key}")
        if response.status_code == 404:
            raise KeyError(key)
        response.raise_for_status()
    
    def __contains__(self, key):
        """Check if a key exists via HTTP HEAD."""
        quoted_key = urllib.parse.quote(key, safe='')
        response = requests.head(f"{self.url}/{quoted_key}")
        return response.status_code == 200
    
    def get(self, key, default=None):
        """
        Get the value for a key. If the key doesn't exist, return the default value.
        """
        return self[key] if key in self else default
    
    def keys(self):
        """
        Return a sorted list of all keys via POST with {"op": "keys"}.
        """
        response = requests.post(
            f"{self.url}/",
            json={"op": "keys"}
        )
        response.raise_for_status()
        return response.json()['keys']
    
    def items(self):
        """
        Return a list of (key, value) tuples via POST with {"op": "dump"}.
        """
        response = requests.post(
            f"{self.url}/",
            json={"op": "dump"}
        )
        response.raise_for_status()
        items_dict = response.json()['items']
        return [(k, v) for k, v in items_dict.items()]
    
    def __iter__(self):
        """Iterate over keys."""
        return iter(self.keys())
    
    def __len__(self):
        """Return the number of items."""
        return len(self.keys())


class gdata_http(gdata_http_simple):
    """
    Rich HTTP client - same interface as gdata_local.
    Handles Python objects via JSON serialization.
    """
    
    def __setitem__(self, key, item):
        """Serialize Python object to JSON string, then store via HTTP."""
        json_string = json.dumps(item)
        super().__setitem__(key, json_string)
    
    def __getitem__(self, key):
        """Get JSON string via HTTP, deserialize to Python object."""
        json_string = super().__getitem__(key)
        return json.loads(json_string)


def gdata(gdbm_file=None, url=None, mode='c', mask=0o600):
    """
    Factory function to create appropriate gdata instance.
    
    Args:
        gdbm_file (str): Path to local GDBM file (for local access)
        url (str): URL of gdata-server (for remote access)
        mode (str): File mode for local GDBM (default 'c')
        mask (int): File permissions mask for local GDBM (default 0o600)
    
    Returns:
        gdata_local instance if gdbm_file specified, gdata_http instance if url specified
    
    Examples:
        # Local access
        with gdata(gdbm_file='/path/to/file.gdbm') as db:
            db['key'] = {'data': 'value'}
        
        # Remote access
        with gdata(url='http://localhost:8020') as db:
            db['key'] = {'data': 'value'}
    """
    if gdbm_file is not None and url is not None:
        raise ValueError("Specify either gdbm_file or url, not both")
    
    if gdbm_file is not None:
        return gdata_local(gdbm_file=gdbm_file, mode=mode, mask=mask)
    elif url is not None:
        return gdata_http(url=url)
    else:
        # Default to local with default filename
        return gdata_local(gdbm_file='.gdbm', mode=mode, mask=mask)


# Backward compatibility aliases
gdata_raw = gdata_local_raw
gdata_simple = gdata_local_simple
