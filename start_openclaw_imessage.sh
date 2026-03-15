#!/usr/bin/env bash
# Launches the OpenClaw iMessage bridge in the background.
# Output is appended to ~/openclaw_imessage.log.
# Usage:  ./start_openclaw_imessage.sh [stop|status]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="$SCRIPT_DIR/imessage_bridge.py"
LOG_FILE="$HOME/openclaw_imessage.log"
PID_FILE="$SCRIPT_DIR/.imessage_bridge.pid"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

cmd_start() {
    if is_running; then
        echo "Bridge already running (PID $(cat "$PID_FILE"))."
        exit 0
    fi

    echo "Starting OpenClaw iMessage bridge..."
    echo "Log file: $LOG_FILE"

    nohup python3 "$BRIDGE" >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo "Started with PID $pid."
}

cmd_stop() {
    if ! is_running; then
        echo "Bridge is not running."
        [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
        exit 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "Stopping bridge (PID $pid)..."
    kill "$pid"
    rm -f "$PID_FILE"
    echo "Stopped."
}

cmd_status() {
    if is_running; then
        echo "Bridge is running (PID $(cat "$PID_FILE"))."
        echo "Log file: $LOG_FILE"
    else
        echo "Bridge is not running."
    fi
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "${1:-start}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    *)
        echo "Usage: $0 [start|stop|status]"
        exit 1
        ;;
esac
