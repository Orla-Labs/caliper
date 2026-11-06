#!/usr/bin/env bash
# Playwright end-to-end suite for `caliper label` (packages/label/e2e).
#
#   scripts/e2e_label.sh                      # whole suite
#   scripts/e2e_label.sh tests/05-team.spec.ts  # extra args go to `playwright test`
#
# Installs what is missing (Python env, the e2e package's npm deps, Chromium), starts
# `caliper label` (start screen) on a free port with a temp HOME and project dir, runs the
# suite against it, then tears everything down. Specs that need another mode (team token,
# legacy serve, a ready project, Postgres) start their own servers the same way.
#
# Env:
#   CALIPER_E2E_PORTS=8791-8795   keep every server in this port range (default: any free port)
#   CALIPER_LABEL_E2E_DOCKER=1    also run the Postgres wizard (needs packages/label/docker/compose.yml
#                                 up; CALIPER_LABEL_PG_PORT, default 55432)
#   E2E_SCREENSHOTS=1             also write the founder screenshots to docs/label-product/
#   E2E_SKIP_INSTALL=1            don't touch uv / npm / browsers
#   E2E_KEEP_TMP=1                keep the temp dir (server logs, projects) for debugging
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
E2E="$ROOT/packages/label/e2e"

if [ "${E2E_SKIP_INSTALL:-}" != "1" ]; then
  [ -x "$ROOT/.venv/bin/caliper" ] || (cd "$ROOT" && uv sync)
  if [ ! -d "$E2E/node_modules/@playwright/test" ]; then
    (cd "$E2E" && npm ci --no-audit --no-fund)
  fi
  # a no-op when the matching Chromium is already in the cache
  (cd "$E2E" && npx playwright install chromium >/dev/null)
fi

CALIPER="${CALIPER_BIN:-$ROOT/.venv/bin/caliper}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/caliper-label-e2e.XXXXXX")"
mkdir -p "$TMP/home" "$TMP/work" "$TMP/servers"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [ "${E2E_KEEP_TMP:-}" = "1" ]; then echo "kept $TMP"; else rm -rf "$TMP"; fi
}
trap cleanup EXIT INT TERM

free_port() {
  python3 - "${CALIPER_E2E_PORTS:-}" <<'PY'
import socket, sys
rng = sys.argv[1]
ports = range(int(rng.split("-")[0]), int(rng.split("-")[-1]) + 1) if rng else [0]
for p in ports:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        print(s.getsockname()[1])
        sys.exit(0)
    except OSError:
        pass
    finally:
        s.close()
sys.exit(f"no free port in {rng}")
PY
}

PORT="$(free_port)"
# The server never sees the developer's database or token variables.
(
  cd "$TMP/work"
  for v in $(env | sed -n 's/^\(PG[A-Z_]*\)=.*/\1/p'); do unset "$v"; done
  unset DATABASE_URL CALIPER_LABEL_TOKEN
  HOME="$TMP/home" CALIPER_LABEL_HOME="$TMP/home/caliper-label" CALIPER_LABEL_PROJECTS="$TMP/work" PYTHONUNBUFFERED=1 \
    exec "$CALIPER" label --port "$PORT" --no-browser
) >"$TMP/servers/start-screen.log" 2>&1 &
SERVER_PID=$!

BASE="http://127.0.0.1:$PORT"
for _ in $(seq 1 240); do
  if curl -fsS "$BASE/api/status" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then cat "$TMP/servers/start-screen.log"; exit 1; fi
  sleep 0.25
done
curl -fsS "$BASE/api/status" >/dev/null || { echo "caliper label did not start on $BASE"; cat "$TMP/servers/start-screen.log"; exit 1; }
echo "caliper label (start screen) on $BASE"

export CALIPER_E2E_BASE_URL="$BASE"
export CALIPER_E2E_HOME="$TMP/home/caliper-label"
export CALIPER_E2E_WORK="$TMP/work"
export CALIPER_E2E_TMP="$TMP/runs"
export CALIPER_BIN="$CALIPER"

cd "$E2E"
start=$(date +%s)
status=0
npx playwright test "$@" || status=$?
echo "e2e: $(( $(date +%s) - start ))s, exit $status"
exit $status
