#!/bin/bash
# Start the gdata server with notes database
cd /home/john/py/gdata-server

# Update config to use the original notes database
cat > .gdata_server.yaml << EOF
gdbm_file: /home/john/py/gdata-server/.agent_notes.gdbm
gdata_server_port: 8021
gdata_server_host: 127.0.0.1
EOF

# Check if server is already running
if pgrep -f "python3 gdata_server.py" > /dev/null; then
    echo "Server already running. Stopping it first..."
    pkill -f "python3 gdata_server.py"
    sleep 1
fi

# Start server in background
echo "Starting gdata server on port 8021 with notes database..."
nohup python3 gdata_server.py > server.log 2>&1 &
echo "Server started with PID: $!"
echo "Server log: server.log"
echo "Test with: curl http://127.0.0.1:8021/"