#!/bin/bash
# Start Claude Terminal server (background, port 8080).
set -e

# Resolve repo dir from this script's own location
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Load .env (CLAUDE_TERMINAL_TOKEN pinned here so PWA token survives restarts)
if [ -f ".env" ]; then
  set -a; . ./.env; set +a
fi

# Install Python deps if needed (idempotent)
if [ -f "backend/requirements.txt" ]; then
  python3 -m pip install --user --quiet -r backend/requirements.txt 2>/dev/null || true
fi

# Kill any existing server on port 8080
EXISTING_PID=$(lsof -ti:8080 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
  kill $EXISTING_PID 2>/dev/null || true
  sleep 1
fi

# Start server in background
nohup python3 -m uvicorn backend.server:app \
  --host 0.0.0.0 \
  --port 8080 \
  --ws-ping-interval 30 \
  --ws-ping-timeout 10 \
  > /tmp/claude-terminal.log 2>&1 &

sleep 2

if curl -sf http://localhost:8080/api/health > /dev/null 2>&1; then
  echo "✅ Claude Terminal running on http://localhost:8080"
  echo
  echo "Next steps:"
  echo "  1. In another terminal:  ngrok http 8080"
  echo "  2. Open the printed https://*.ngrok-free.dev URL on your phone"
  echo "  3. Share → Add to Home Screen (the PWA auto-connects)"
  echo
  echo "Tail logs:   tail -f /tmp/claude-terminal.log"
  echo "Stop:        kill \$(lsof -ti:8080)"
else
  echo "❌ Server failed to start. Last 30 lines of log:"
  tail -30 /tmp/claude-terminal.log
  exit 1
fi
