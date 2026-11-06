#!/usr/bin/env bash
# Run the deterministic Caliper demo (see demo/run_demo.py).
#
#   scripts/demo.sh            print the demo to stdout
#   scripts/demo.sh --check    diff a fresh run against demo/expected_output.txt
#   scripts/demo.sh --update   regenerate demo/expected_output.txt
#
# No network, no API keys, no randomness: output is byte-identical across runs.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECTED="$ROOT/demo/expected_output.txt"

export PYTHONHASHSEED=0
export PYTHONIOENCODING=utf-8
export PYTHONDONTWRITEBYTECODE=1
export NO_COLOR=1

run() {
  (cd "$ROOT" && uv run --quiet python demo/run_demo.py)
}

case "${1:-}" in
  "")
    run
    ;;
  --check)
    actual="$(mktemp)"
    trap 'rm -f "$actual"' EXIT
    run >"$actual"
    if diff -u "$EXPECTED" "$actual"; then
      echo "demo output matches demo/expected_output.txt" >&2
    else
      echo "demo output differs from demo/expected_output.txt" >&2
      exit 1
    fi
    ;;
  --update)
    run >"$EXPECTED"
    echo "wrote demo/expected_output.txt" >&2
    ;;
  *)
    echo "usage: $0 [--check|--update]" >&2
    exit 2
    ;;
esac
