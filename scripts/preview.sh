#!/bin/sh
# Local/preview launcher for AnyDown (non-Docker environments).
#
# Production uses the Dockerfile + start.sh, which always have the bgutil
# provider at /opt/bgutil. This script is for Freebuff/local previews: it
# starts the provider when a build is found (BGUTIL_SERVER_DIR, /tmp/bgutil,
# or a checkout next to the repo), and starts the API regardless — the
# extractor falls back to PO-token-free clients when the provider is absent.
set -eu

cd "$(dirname "$0")/.."

PROVIDER_CANDIDATES="${BGUTIL_SERVER_DIR:-} /tmp/bgutil/server ./bgutil/server"
PROVIDER_JS=""
for dir in $PROVIDER_CANDIDATES; do
  if [ -f "$dir/build/main.js" ]; then
    PROVIDER_JS="$dir/build/main.js"
    break
  fi
done

POT_PID=""
if [ -n "$PROVIDER_JS" ] && command -v node >/dev/null 2>&1; then
  echo "[preview] starting bgutil provider: $PROVIDER_JS"
  node "$PROVIDER_JS" --host 127.0.0.1 --port 4416 &
  POT_PID=$!
  export YTDLP_POT_PROVIDER_URL="http://127.0.0.1:4416"
  i=0
  while [ "$i" -lt 40 ]; do
    if curl -sf http://127.0.0.1:4416/ping >/dev/null 2>&1; then
      echo "[preview] bgutil provider ready"
      break
    fi
    i=$((i + 1))
    sleep 0.5
  done
else
  echo "[preview] no bgutil provider build found; continuing without PO tokens"
fi

cleanup() {
  [ -n "$POT_PID" ] && kill "$POT_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

PORT="${PORT:-10000}"
echo "[preview] starting AnyDown API on 0.0.0.0:$PORT"
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
