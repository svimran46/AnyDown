#!/bin/sh
set -eu

# The provider is local to this container, so the app can safely use
# http://127.0.0.1:4416 without exposing the unauthenticated provider publicly.
node /opt/bgutil/server/build/main.js --host 127.0.0.1 --port 4416 &
POT_PID=$!

cleanup() {
  kill "$POT_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Give the provider a moment to bind before starting the API.
sleep 1

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}"
