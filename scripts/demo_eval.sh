#!/usr/bin/env bash
# Run the catalog-cleanup eval demo (examples/catalog_cleanup) end to end.
#
#   scripts/demo_eval.sh            print the demo to stdout
#   scripts/demo_eval.sh --check    diff a fresh run against examples/catalog_cleanup/expected_output.txt
#   scripts/demo_eval.sh --update   regenerate that transcript
#
# Offline: the fake provider, no network, no API keys. Output is byte-identical
# across runs. The CI gate FAILS for the `small` candidate on purpose; the demo
# shows that verdict and still exits 0. (`caliper eval --ci` itself exits 1.)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EX="examples/catalog_cleanup"
OUT="build/catalog-eval"
EXPECTED="$ROOT/$EX/expected_output.txt"

export PYTHONHASHSEED=0
export PYTHONIOENCODING=utf-8
export PYTHONDONTWRITEBYTECODE=1
export NO_COLOR=1

banner() {
  printf '\n========================================================================\n'
  printf ' %s\n' "$1"
  printf '========================================================================\n'
}

run() {
  cd "$ROOT"
  rm -rf "$OUT" build/catalog-preflight
  mkdir -p "$OUT"

  echo "Caliper eval demo: is it safe to switch the catalog-cleanup feature to a cheaper model?"
  echo "(synthetic data, fake provider; every number below is from this demo, not a real system)"

  banner "[1/4] Build a golden set from production logs (rare classes kept whole)"
  echo "\$ caliper sample --in $EX/logs.jsonl --stratum-field outcome --keep needs_review,fallback --negatives 40"
  uv run --quiet caliper sample --in "$EX/logs.jsonl" --out "$OUT/sampled.jsonl" \
    --stratum-field outcome --keep needs_review,fallback --negatives 40 \
    --id-field id --reference-field outcome
  echo
  echo "Then a person labels the sample. The labeled result ships as $EX/golden.jsonl:"
  uv run --quiet python - "$EX/golden.jsonl" <<'PY'
import collections, json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
by = collections.Counter(r["stratum"] for r in rows)
print(f"  {len(rows)} rows: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
src = collections.Counter(r.get("label_source") or "unknown" for r in rows)
ok = collections.Counter(r.get("label_source") or "unknown" for r in rows if r["label_approved"])
print("  labelled by: " + ", ".join(f"{k} {v} ({ok[k]} approved)" for k, v in sorted(src.items())))
split = collections.Counter(r["split"] for r in rows)
print("  split: " + ", ".join(f"{k} {v}" for k, v in sorted(split.items())) + "; slices: "
      + ", ".join(f"{k} {v}" for k, v in sorted(collections.Counter(
          s for r in rows for s in r["slices"]).items())))
PY

  banner "[2/4] Preflight: a candidate shape the provider rejects fails fast"
  echo "\$ caliper eval --config $EX/caliper.preflight.toml"
  set +e
  uv run --quiet caliper eval --config "$EX/caliper.preflight.toml" \
    --out build/catalog-preflight --no-resume 2>&1
  status=$?
  set -e
  echo "(exit $status: nothing was spent on the golden set)"

  banner "[3/4] Evaluate: noise floor, slices, matched operating point, calibration, CI gate"
  echo "decide.py replays the feature: 3 replies, majority tax code, auto_publish at score >= 0.75."
  echo "\$ caliper eval --config $EX/caliper.toml --out $OUT --no-resume --ci --summary-md $OUT/summary.md"
  set +e
  uv run --quiet caliper eval --config "$EX/caliper.toml" --out "$OUT" --no-resume \
    --ci --summary-md "$OUT/summary.md" 2>/dev/null
  status=$?
  set -e
  echo
  if [ "$status" -eq 1 ]; then
    echo ">>> caliper eval --ci exited 1: the gate FAILED, so this change would be blocked in CI."
    echo ">>> (Expected here: 'small' regresses on rare product families; 'small-thinking' is"
    echo ">>>  non-inferior overall but loses one must-not-lose rare-code record on test.)"
  else
    echo ">>> caliper eval --ci exited $status"
  fi

  banner "[4/4] The PR comment ($OUT/summary.md)"
  cat "$OUT/summary.md"
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
      echo "eval demo output matches $EX/expected_output.txt" >&2
    else
      echo "eval demo output differs from $EX/expected_output.txt" >&2
      exit 1
    fi
    ;;
  --update)
    run >"$EXPECTED"
    echo "wrote $EX/expected_output.txt" >&2
    ;;
  *)
    echo "usage: $0 [--check|--update]" >&2
    exit 2
    ;;
esac
