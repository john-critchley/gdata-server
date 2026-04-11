#!/bin/bash
# Start the gdata server with notes database
cd /home/john/py/gdata-server

# Start server in background
echo "Starting gdata server on port 8021 with notes database..."
nohup python3 gdata_server.py > server.log 2>&1 &
echo "Server started with PID: $!"
echo "Server log: server.log"
