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

# Stop any existing test server
pkill -f "GDATA_SERVER_CONFIG=.gdata_server_test.yaml.*python3 gdata_server.py" 2>/dev/null || true
sleep 1

# Start test server
echo "Starting test server on port 8020 with test database..."
GDATA_SERVER_CONFIG=.gdata_server_test.yaml GDATA_SERVER_HTTP=1 python3 gdata_server.py > test_server.log 2>&1 &
TEST_SERVER_PID=$!
echo "Test server started with PID: $TEST_SERVER_PID"

# Wait for server to be ready
sleep 2
if curl -s http://127.0.0.1:8020/ > /dev/null; then
    echo "Test server is ready!"
    echo "Server log: test_server.log"
    echo "To run tests: python3 test_gdata.py"
    echo "To stop test server: pkill -f 'GDATA_SERVER_CONFIG=.gdata_server_test.yaml'"
else
    echo "Test server failed to start - check test_server.log"
    exit 1
fi