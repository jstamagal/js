#!/usr/bin/env bash
# Run defaultagent's 16 benchmark turns against a real provider, on a real
# project, with every tool available and every tool deferred.
#
#   ./sweep.sh q27          qwen3.8-27b       @ 216.158.72.164:8000
#   ./sweep.sh qfn          qwen3.8-flash-next @ 133.125.98.111:8080
#   ./sweep.sh deepseek     deepseek-flash     @ api.deepseek.com
#
# Everything lands in /tmp/toolsweep-<arm>/ : the project it worked on, the
# full stdout log with every ▸ tool call, and the timing stats.
set -euo pipefail

ARM="${1:?usage: sweep.sh <q27|qfn|deepseek> [reasoning]}"
EFFORT="${2:-xhigh}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="/tmp/toolsweep-$ARM"
WORK="$OUT/project"

case "$ARM" in
  q27)      BASE="http://216.158.72.164:8000/v1"  ; MODEL="qwen3.8-27b" ; KEY=foo ;;
  qfn)      BASE="http://133.125.98.111:8080/v1"  ; MODEL="qwen3.8-flash-next" ; KEY=foo ;;
  deepseek) BASE="https://api.deepseek.com"       ; MODEL="deepseek-flash"
            KEY="${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY not set}" ; EFFORT="${2:-max}" ;;
  *) echo "unknown arm: $ARM" >&2; exit 2 ;;
esac

rm -rf "$OUT" 2>/dev/null || true
mkdir -p "$OUT"
bash "$HERE/fixture.sh" "$WORK" >/dev/null
echo "project : $WORK"
echo "arm     : $ARM  $MODEL  $BASE  effort=$EFFORT"

# what the harness is actually booting with, recorded before the run
( cd ~/js && git rev-parse --abbrev-ref HEAD && .venv/bin/python -c "
from js.toolkit.registry import _LAZY_SUITES
print(f'deferred: {len(_LAZY_SUITES)} tools')
print(sorted(_LAZY_SUITES))
" ) > "$OUT/harness-state.txt"
cat "$OUT/harness-state.txt"
echo

export JS_PROVIDER=openai
export JS_SERVICE=openai
export JS_BASE_URL="$BASE"
export JS_MODEL="$MODEL"
export JS_REASONING="$EFFORT"
export JS_API_KEY="$KEY"

# -C runs it as if launched from the project. -d turns on the per-turn trace
# so stdout carries every "▸ toolname {args}" line — the whole point of the
# capture. Without it a bench run logs only the model's prose.
# Throwaway HOME with only the agent profile in it. The jail blocks the real
# one outright; this gives js somewhere legitimate to look.
SBHOME="$OUT/home"
mkdir -p "$SBHOME/.config/js/agents" "$WORK/.tmp"
cp -r ~/.config/js/agents/toolaudit "$SBHOME/.config/js/agents/"

# Absolute path: ~/.local/bin is not inside the jail, only the uv tool dir is.
"$HERE/sandbox.sh" "$WORK" "$SBHOME" \
  "$HOME/.local/share/uv/tools/js/bin/js" --bench toolaudit \
     -C "$WORK" \
     -r "$EFFORT" \
     -d \
     --stats-json "$OUT/stats.json" \
     --stats-csv  "$OUT/stats.csv" \
  2>&1 | tee "$OUT/run.log"

echo
echo "=== findings the agent filed ==="
if [ -d "$WORK/.scratch/toolaudit" ]; then
  cp -r "$WORK/.scratch/toolaudit" "$OUT/findings"
  ls -la "$OUT/findings"
else
  echo "(none filed)"
fi

echo
echo "=== files the agent left behind ==="
( cd "$WORK" && git status --short && echo "--- diff ---" && git diff --stat )

echo
echo "log    : $OUT/run.log"
echo "stats  : $OUT/stats.json"
echo "project: $WORK"
