#!/usr/bin/env bash
# Run the offline suite on another host: copy this checkout's files there and
# run pytest in the copy. Usage: scripts/test-on.sh HOST [pytest args...]
set -euo pipefail
host=$1; shift
copy=.js/work/js-test-$(basename "$PWD")
git ls-files -co --exclude-standard -z |
    rsync -a --delete --from0 --files-from=- --exclude=.venv ./ "$host:$copy/"
args=$(printf ' %q' "$@")
# Most of the host's cores are the suite's: nobody sits at it.
ssh "$host" "cd $copy && PATH=\$HOME/.local/bin:\$PATH JS_TEST_WORKERS=\${JS_TEST_WORKERS:-\$((\$(nproc) - 2))} \
    uv run --extra test --extra browser pytest -q -p no:cacheprovider$args"
