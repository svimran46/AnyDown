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

# If YOUTUBE_COOKIES_FILE points to a read-only secret (like Render's /etc/secrets),
# copy it to a writable location so yt-dlp can update session cookies without crashing.
if [ -n "${YOUTUBE_COOKIES_FILE:-}" ] && [ -f "$YOUTUBE_COOKIES_FILE" ]; then
  cp "$YOUTUBE_COOKIES_FILE" /tmp/youtube_cookies.txt
  chmod 600 /tmp/youtube_cookies.txt || true
  export YOUTUBE_COOKIES_FILE=/tmp/youtube_cookies.txt
fi

# Trust the proxy's X-Forwarded-For so request.client.host is the real client
# IP (required for per-IP throttling behind Render's router). Overridable via
# FORWARDED_ALLOW_IPS if you front this with a stricter proxy setup.
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
