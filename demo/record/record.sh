#!/usr/bin/env bash
# Re-record docs/demo.gif.
#
#   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core demo/record/record.sh
#
# Needs: uv, pnpm (editor deps installed), node, ffmpeg, a Playwright Chromium
# in ~/Library/Caches/ms-playwright (or CHROMIUM=/path/to/chrome). Nothing
# here is a repo dependency. Uses ports 8000 and 3456.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/build/record"
FFMPEG="${FFMPEG:-ffmpeg}"
mkdir -p "$OUT" "$ROOT/docs"
cd "$ROOT"

# 1. The demo transcript must be current: the GIF replays expected_output.txt.
scripts/demo.sh --check

# 2. Store with the invoice_totals eval set for the editor's dry run.
rm -f "$OUT/store.db"
uv run --quiet python demo/record/seed_store.py "sqlite:///$OUT/store.db"

# 3. Servers.
CALIPER_STORE_URL="sqlite:///$OUT/store.db" scripts/dev.sh >"$OUT/dev.log" 2>&1 &
DEV_PID=$!
trap 'pkill -P $DEV_PID 2>/dev/null || true; kill $DEV_PID 2>/dev/null || true' EXIT
for url in http://127.0.0.1:8000/health http://127.0.0.1:3456/; do
  for _ in $(seq 1 120); do
    curl -fsS -o /dev/null "$url" && break
    sleep 1
  done
done

# 4. Frames.
FRAMES_DIR="$OUT/frames" node demo/record/record.mjs

# 5. GIF: low fps (frames hold for their listed duration), one shared palette.
"$FFMPEG" -y -loglevel error -f concat -safe 0 -i "$OUT/frames/frames.txt" \
  -vf "fps=8,split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 "$ROOT/docs/demo.gif"
ls -lh "$ROOT/docs/demo.gif"
