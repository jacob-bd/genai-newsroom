#!/usr/bin/env bash
# Stop the newsroom callback handler daemon.

PID_FILE="$HOME/.alef-agent/workspace/newsroom/data/callback_handler.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found. Callback handler may not be running."
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm -f "$PID_FILE"
    echo "Callback handler stopped (PID $PID)"
else
    echo "Process $PID not found. Cleaning up PID file."
    rm -f "$PID_FILE"
fi
