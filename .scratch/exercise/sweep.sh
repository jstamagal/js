#!/usr/bin/env bash
# Run the toolaudit agent's benchmark turns against a real provider, on a real
# repo clone, under a bubblewrap jail, with every tool deferred.
#
#   ./sweep.sh <arm> [effort]     ./sweep.sh            # lists the arms
#
# Everything lands in /tmp/toolsweep-<arm>/ : the clone it worked on, the full
# log with every tool call and result, the timing stats, and any findings filed.
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# ARMS.  One line each.  No punctuation, no quotes, no trailing anything.
#
#   arm  <name>  <base-url>  <model>  <key>  <effort>
#
# <base-url>  a real url, OR the word  login  to let js route it itself from
#             ~/.config/js/logins.toml and provider env keys. With  login  the
#             model is written the way js wants it, provider/model, and the key
#             column is ignored -- write  none  there.
#
# <key>    none            for an endpoint that wants no real key
#          env:SOME_VAR    to take it from the environment by that name
# Proxy arms use TOOLSWEEP_PROXY_API_KEY from the environment or the ignored
# .env beside this script. Keep credential values out of this file.
# Never write a $ here. An unset $VAR would vanish and silently shift every
# column after it one place to the left.
# <effort> off minimal low medium high xhigh max
#
# To add an arm, copy a line and change the words.  Nothing else in this file
# needs to change.
# ─────────────────────────────────────────────────────────────────────────────
arms() {
  arm q27 http://216.158.72.164:8000/v1 qwen3.8-27b none xhigh
  arm qfn http://133.125.98.111:8080/v1 qwen3.8-flash-next none xhigh
  arm gpt http://vader:8317/v1 gpt-6-astra env:TOOLSWEEP_PROXY_API_KEY xhigh
  arm deepseek https://api.deepseek.com deepseek-flash env:DEEPSEEK_API_KEY max
  arm qwenflash http://vader:8317/v1 hyper/qwen3.8-flash env:TOOLSWEEP_PROXY_API_KEY max
  arm qwen27b http://vader:8317/v1 hyper/qwen3.8-27b env:TOOLSWEEP_PROXY_API_KEY xhigh
  arm muse login openrouter/meta/muse-spark-1.3-contributor env:OPENROUTER_API_KEY xhigh
  arm glm login openrouter/z-ai/glm-5.3-flash env:OPENROUTER_API_KEY xhigh
  arm nex login openrouter/nex-agi/nex-n2.5-pro:free env:OPENROUTER_API_KEY xhigh
  arm laguna login openrouter/poolside/laguna-s-2.1:free env:OPENROUTER_API_KEY xhigh
  arm nemolight login openrouter/nvidia/nemotron-3.5-lightning:free env:OPENROUTER_API_KEY xhigh
}

# ─────────────────────────────────────────────────────────────────────────────

ARM="${1:-}"
EFFORT_CLI="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/.env" ]; then
  source "$HERE/.env"
fi

ARM_NAMES=""
arm() {
  ARM_NAMES="$ARM_NAMES $1"
  [ "$1" = "$ARM" ] || return 0
  if [ "$#" -ne 5 ]; then
    echo "arm $1: expected 5 words (name url model key effort), got $#" >&2
    exit 2
  fi
  BASE="$2"
  MODEL="$3"
  KEY="$4"
  EFFORT="${EFFORT_CLI:-$5}"
}
arms

if [ -z "$ARM" ]; then
  echo "usage: sweep.sh <arm> [effort]"
  echo "arms:$ARM_NAMES"
  exit 2
fi
if [ -z "${MODEL:-}" ]; then
  echo "unknown arm: $ARM" >&2
  echo "arms:$ARM_NAMES" >&2
  exit 2
fi
case "$KEY" in
none) KEY=placeholder ;;
env:*)
  VAR="${KEY#env:}"
  KEYVAR="$VAR"
  KEY="$(printenv "$VAR" || true)"
  if [ -z "$KEY" ]; then
    echo "arm $ARM: \$$VAR is not set in this shell" >&2
    exit 2
  fi
  ;;
esac

OUT="/tmp/toolsweep-$ARM"
WORK="$OUT/project"

rm -rf "$OUT" 2>/dev/null || true
mkdir -p "$OUT"
bash "$HERE/fixture.sh" "$WORK" >/dev/null
echo "project : $WORK"
echo "arm     : $ARM  $MODEL  $BASE  effort=$EFFORT"

# what the harness is actually booting with, recorded before the run
(cd ~/js && git rev-parse --abbrev-ref HEAD && .venv/bin/python -c "
from js.toolkit.registry import _LAZY_SUITES
print(f'deferred: {len(_LAZY_SUITES)} tools')
print(sorted(_LAZY_SUITES))
") >"$OUT/harness-state.txt"
cat "$OUT/harness-state.txt"
echo

export JS_MODEL="$MODEL"
export JS_REASONING="$EFFORT"
if [ "$BASE" = login ]; then
  # The jail strips credentials by default; a login-routed arm needs exactly one
  # of them to reach its provider. KEYVAR is the name from the arm's key column.
  export SANDBOX_KEEP="${KEYVAR:-}"
  # Deliberately unset. An explicit JS_PROVIDER switches OFF the provider/model
  # prefix routing in config.py, so `openrouter/foo` would be shipped whole to
  # whatever base url is pinned instead of going to openrouter.
  unset JS_PROVIDER JS_SERVICE JS_BASE_URL JS_API_KEY 2>/dev/null || true
else
  export JS_PROVIDER=openai
  export JS_SERVICE=openai
  export JS_BASE_URL="$BASE"
  export JS_API_KEY="$KEY"
fi

# Preflight. A wrong model id or a dead endpoint otherwise burns every turn in
# the file before anyone notices, one error line at a time.
echo -n "preflight: "
if PRE="$("$HOME/.local/share/uv/tools/js/bin/js" -p "say ok" -n -q 2>&1 | tail -2)"; then
  echo "$PRE" | tail -1
else
  echo "FAILED"
  echo "$PRE" >&2
  echo "-> arm $ARM cannot reach its model; not starting the sweep" >&2
  exit 1
fi

# -C runs it as if launched from the project. -d turns on the per-turn trace
# so stdout carries every "▸ toolname {args}" line — the whole point of the
# capture. Without it a bench run logs only the model's prose.
# Throwaway HOME with only the agent profile in it. The jail blocks the real
# one outright; this gives js somewhere legitimate to look.
SBHOME="$OUT/home"
mkdir -p "$SBHOME/.config/js" "$WORK/.tmp"
# The whole js config, not just the agent. jsrc and models-cache.json are what
# make `openrouter/foo` resolve to a provider at all; without them routing falls
# back to the bare ai-sdk gateway and every turn dies not-logged-in.
cp -r ~/.config/js/. "$SBHOME/.config/js/"

# Absolute path: ~/.local/bin is not inside the jail, only the uv tool dir is.
"$HERE/sandbox.sh" "$WORK" "$SBHOME" \
  "$HOME/.local/share/uv/tools/js/bin/js" --bench toolaudit \
  -C "$WORK" \
  -r "$EFFORT" \
  -d \
  --stats-json "$OUT/stats.json" \
  --stats-csv "$OUT/stats.csv" \
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
(cd "$WORK" && git status --short && echo "--- diff ---" && git diff --stat)

echo
echo "log    : $OUT/run.log"
echo "stats  : $OUT/stats.json"
echo "project: $WORK"
