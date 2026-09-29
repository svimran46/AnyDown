#!/bin/sh
set -eu

# The provider is local to this container, so the app can safely use
# http://127.0.0.1:4416 without exposing the unauthenticated provider publicly.
node /opt/bgutil/server/build/main.js --host 127.0.0.1 --port 4416 &
POT_PID=$!

UVICORN_PID=""

cleanup() {
  # Forward the signal to the app so it can drain in-flight requests.
  [ -n "$UVICORN_PID" ] && kill "$UVICORN_PID" 2>/dev/null || true
  kill "$POT_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Do not `exec` uvicorn below: exec replaces the shell, so this EXIT trap would
# never run and the provider process would be orphaned on shutdown.

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

# If YOUTUBE_COOKIES_FILE points to a read-only secret (like Render's /etc/secrets),
# copy it to a writable location so yt-dlp can update session cookies without crashing.
if [ -n "${YOUTUBE_COOKIES_FILE:-}" ] && [ -f "$YOUTUBE_COOKIES_FILE" ]; then
  cp "$YOUTUBE_COOKIES_FILE" /tmp/youtube_cookies.txt
  chmod 600 /tmp/youtube_cookies.txt || true
  export YOUTUBE_COOKIES_FILE=/tmp/youtube_cookies.txt
fi

# --proxy-headers makes uvicorn set request.client.host from X-Forwarded-For.
# The app deliberately does NOT read that header itself (see TRUSTED_PROXY_HOPS
# in .env.example): it is client-controlled and would defeat rate limiting.
uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" &
UVICORN_PID=$!
wait "$UVICORN_PID"
