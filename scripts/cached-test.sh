#!/usr/bin/env bash
# Run the offline suite once per tree state. The key is HEAD plus a hash of
# the working-tree diff (tracked and untracked), so an unchanged tree replays
# the last run — its output, its exit code, who ran it and when — instead of
# spending 40 seconds re-learning the same answer. `just test --force` reruns.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
cache_dir=.git/js-test-cache
mkdir -p "$cache_dir"

force=0
if [ "${1:-}" = "--force" ]; then force=1; shift; fi

head=$(git rev-parse HEAD)
dirty=$( { git diff HEAD --no-color; git ls-files --others --exclude-standard -z | xargs -0 -r sha256sum; } | sha256sum | cut -c1-16)
key="${head:0:12}-${dirty}"
hit="$cache_dir/$key"

if [ $force -eq 0 ] && [ -f "$hit.out" ] && [ -f "$hit.meta" ]; then
    cat "$hit.out"
    echo
    echo "== cached run, tree unchanged: $(cat "$hit.meta")  (just test --force reruns)"
    code=$(cat "$hit.code")
    if [ "$code" != 0 ]; then
        echo "== RED at $(git log -1 --format='%h (%an, %ar)'): $(grep -c '^FAILED' "$hit.out") failing. Not done until green. Do not delete or edit the test. Cannot pass it? Confess in FAILURES.md: full model name, date, tests, what you tried, 'I could not make this pass.'"
    fi
    exit "$code"
fi

tmp=$(mktemp)
"$@" 2>&1 | tee "$tmp"
code=${PIPESTATUS[0]}
mv "$tmp" "$hit.out"
echo "$code" > "$hit.code"
if [ "$code" != 0 ]; then
    echo "== RED at $(git log -1 --format='%h (%an, %ar)'): $(grep -c '^FAILED' "$hit.out") failing. Not done until green. Do not delete or edit the test. Cannot pass it? Confess in FAILURES.md: full model name, date, tests, what you tried, 'I could not make this pass.'"
fi
echo "$(date '+%Y-%m-%d %H:%M') by ${JS_TEST_ACTOR:-${USER:-?}} on $(hostname) at ${head:0:7}$( [ "$dirty" != "$(printf '' | sha256sum | cut -c1-16)" ] && echo '+dirty')" > "$hit.meta"
# keep the last 20 runs
ls -t "$cache_dir"/*.meta 2>/dev/null | tail -n +21 | sed 's/\.meta$//' | xargs -r -I{} rm -f {}.out {}.meta {}.code
exit "$code"
