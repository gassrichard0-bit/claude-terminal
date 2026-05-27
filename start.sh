#!/bin/bash
# Start Claude Terminal server
set -e

cd /workspaces/claude-terminal

# Kill any existing server on port 8080
kill $(lsof -ti:8080) 2>/dev/null || true
sleep 1

# Start server in background
nohup python -m uvicorn backend.server:app \
  --host 0.0.0.0 \
  --port 8080 \
  --ws-ping-interval 30 \
  --ws-ping-timeout 10 \
  > /tmp/claude-terminal.log 2>&1 &

sleep 2

# Check it's running
if curl -sf http://localhost:8080/api/health > /dev/null 2>&1; then
  echo "✅ Claude Terminal running on http://localhost:8080"
  echo "🔑 Token: claude123"
else
  echo "❌ Server failed to start. Check /tmp/claude-terminal.log"
  cat /tmp/claude-terminal.log
fi
