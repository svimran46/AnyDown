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

# Poll for provider readiness instead of a simple sleep.
PROVIDER_READY=0
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:4416/ping >/dev/null 2>&1; then
    PROVIDER_READY=1
    break
  fi
  sleep 0.5
done
if [ "$PROVIDER_READY" -ne 1 ]; then
  echo "bgutil POT provider failed to start within 30s; aborting." >&2
  exit 1
fi

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}"
