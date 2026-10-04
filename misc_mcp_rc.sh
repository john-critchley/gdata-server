#!/bin/bash
set -eu

NAME="misc-mcp"
SERVER_SCRIPT="/home/john/py/gdata-server/start_misc_server.sh"
PIDFILE="/home/john/py/gdata-server/misc_mcp_server.pid"
LOGFILE="/home/john/py/gdata-server/misc_mcp_server.log"

is_running() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "${1:-}" in
    start)
        if is_running; then
            echo "$NAME already running (PID $(cat "$PIDFILE"))"
            exit 0
        fi
        rm -f "$PIDFILE"
        echo "Starting $NAME..."
        install -d -o john -g john -m 0700 /home/john/py/gdata-server
        runuser -u john -- env HOME=/home/john MISC_MCP_DEV_MODE=0 \
            "$SERVER_SCRIPT" >>"$LOGFILE" 2>&1 &
        echo $! >"$PIDFILE"
        chown john:john "$PIDFILE" "$LOGFILE" 2>/dev/null || true
        ;;
    stop)
        if is_running; then
            echo "Stopping $NAME (PID $(cat "$PIDFILE"))..."
            kill "$(cat "$PIDFILE")"
            rm -f "$PIDFILE"
        else
            echo "$NAME is not running"
            rm -f "$PIDFILE"
        fi
        ;;
    restart)
        "$0" stop
        "$0" start
        ;;
    status)
        if is_running; then
            echo "$NAME is running (PID $(cat "$PIDFILE"))"
        else
            echo "$NAME is not running"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac