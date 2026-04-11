#!/bin/bash
# Start test server for running tests
cd /home/john/py/gdata-server

# Create clean test database
rm -f .test.gdbm
python3 -c "
import dbm.gnu as gdbm
import json
# Create fresh test database
db = gdbm.open('.test.gdbm', 'c')
# Add a test root document
db[b''] = json.dumps({'value': 'Test database - ready for testing'}).encode()
db.close()
print('Created clean test database')
"

# Start test server
echo "Starting test server on port 8020 with test database..."
GDATA_SERVER_CONFIG=.gdata_server_test.yaml GDATA_SERVER_HTTP=1 python3 gdata_server.py > test_server.log 2>&1 &
TEST_SERVER_PID=$!
echo "Test server started with PID: $TEST_SERVER_PID"
