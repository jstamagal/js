# js — task runner over uv.
#
# `uv run` auto-syncs the project env from uv.lock on every invocation, so the
# venv, the `js` console script, and all deps are always present and correct.
# You never activate a venv, never `pip install`, never call `.venv/bin/js`
# (that path breaks the moment the package isn't installed into the venv — the
# whole `.venv/bin/js` dance is what this file replaces). This justfile is the
# single workflow entry point for the repo.
#
# `just` with no arg lists recipes. Pass-through recipes (run/commit)
# forward everything after the recipe name, so `just run -p "summarize this"`
# reaches js unchanged.

set dotenv-load

# Playwright publishes glibc Linux wheels but no musllinux wheels. Keep its
# browser backend automatic everywhere it is installable without breaking the
# rest of js on Alpine and other musl systems.
browser-extra := `if ldd --version 2>&1 | grep -qi musl; then true; else printf '%s' '--extra browser'; fi`

# show all recipes (default when `just` is called with no argument)
default:
    @just --list

# ── run the harness ─────────────────────────────────────────────────────────

#   just run -p "summarize this repo"
#   just run --commit
# run js — no args opens the REPL; any js flags/args pass through.
[positional-arguments]
run *args:
    uv run {{ browser-extra }} js "$@"

# Commit workflow is deliberately plain: run `js --commit` from repo root.
# Do not pass -p, a target path, or a message; the commit agent inspects/stages/messages.
# No-arg convenience only. (Extra words after `just commit` are not forwarded —
# just parses them as more recipes to run.)
# run the commit agent (`js --commit`) — takes no arguments.
commit:
    uv run {{ browser-extra }} js --commit

# ── env / deps ───────────────────────────────────────────────────────────────

# sync the project env from uv.lock, including the test extra. idempotent —
# run after a fresh clone, after pulling changed deps, or any time the env
# feels off. Also fetches the Chromium build, since a synced env whose
# browser_probe cannot launch is not actually synced.
# rebuild the env from uv.lock — the real fix for a broken venv.
sync:
    uv sync --extra test {{ browser-extra }}
    just install-browser
    just install-tool-binaries

# drop into a shell with the project env active (uv owns the venv).
shell:
    uv run {{ browser-extra }} bash

# set js up on this box with one command; a rerun asks only about what is
# still missing. `js` on PATH is a launcher in ~/.local/bin that brings this
# checkout's venv in line with uv.lock on every start (`uv sync --inexact`, a
# few ms when nothing changed) and runs the venv's js: one venv, the project's,
# so a dependency added to the project is there the next time js starts. The
# launcher execs .venv/bin/js rather than `uv run js` because uv run puts
# .venv/bin first on PATH and sets VIRTUAL_ENV, and every shell command the
# model runs would inherit them. Also: the wiki symlink, js's pinned CLI
# binaries in tools/bin and the Chromium build, the PATH block in ~/.zshrc and
# ~/.bashrc, then the questions (`python -m js.install`): each key js uses that
# is set neither in the environment nor in ~/.js/.env, and a default model when
# the configured one has no provider to run on.
#   just install   then   js -p "hi"   from anywhere
# set js up on this box: launcher, binaries, missing keys and default model.
install:
    #!/usr/bin/env bash
    set -euo pipefail
    # refuse to install from a linked worktree: the launcher and the wiki
    # symlink would point at a tree that vanishes when the worktree is cleaned
    # up, leaving `js` and `wiki` broken everywhere.
    if [ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]; then
        echo "!! this is a linked git worktree — run 'just install' from the main checkout:" >&2
        echo "!!   $(dirname "$(git rev-parse --git-common-dir)")" >&2
        exit 1
    fi
    repo="$(pwd -P)"
    uv_bin="$(command -v uv)"
    bin="$HOME/.local/bin"
    launcher="$bin/js"
    marker="# js launcher written by just install"
    # large wheels over a flaky link: retry the transfer before giving up.
    UV_HTTP_RETRIES="${UV_HTTP_RETRIES:-5}" uv sync --inexact {{ browser-extra }}
    # the uv tool install this recipe used to make; its shim sits where the
    # launcher goes.
    if uv tool list 2>/dev/null | grep -q '^js '; then
        uv tool uninstall js
    fi
    mkdir -p "$bin"
    if [ -e "$launcher" ] && ! grep -qF "$marker" "$launcher" 2>/dev/null; then
        echo "!! $launcher is not a js launcher — remove it and rerun" >&2
        exit 1
    fi
    tmp="$(mktemp "$bin/.js.XXXXXX")"
    cat > "$tmp" <<EOF
    #!/bin/sh
    $marker in $repo.
    # Brings the checkout's venv in line with uv.lock, then runs its js.
    "$uv_bin" sync --quiet --inexact --project "$repo" {{ browser-extra }} ||
        echo "js launcher: uv sync failed; starting the venv as it stands" >&2
    exec "$repo/.venv/bin/js" "\$@"
    EOF
    chmod 755 "$tmp"
    mv -f "$tmp" "$launcher"
    ln -sf "$repo/tools/wiki" "$bin/wiki"
    just install-tool-binaries
    just install-browser
    # put the managed binaries on the operator's PATH too. js itself resolves
    # them by absolute path, but fd/bat/fzf are downloaded for a human and for
    # other agents to call by name, and hunting for them is the whole problem.
    # One marked block per rc file, appended once; ~/.local/bin joins it when
    # it is not on PATH already.
    case ":$PATH:" in
        *":$bin:"*) bin_line="" ;;
        *) bin_line="export PATH=\"$bin:\$PATH\"" ;;
    esac
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
        [ -f "$rc" ] || continue
        if grep -q '# js tools PATH block begin' "$rc"; then
            if ! grep -q 'COLORTERM=truecolor' "$rc"; then
                sed -i '/# js tools PATH block end/i export COLORTERM=truecolor' "$rc"
                echo "added COLORTERM=truecolor to the js block in $rc"
            fi
            if [ -n "$bin_line" ] && ! grep -qF "$bin_line" "$rc"; then
                sed -i "/# js tools PATH block end/i $bin_line" "$rc"
                echo "added $bin to the js block in $rc"
            fi
            echo "ok: $rc has the js tools PATH block"
            continue
        fi
        {
            echo ''
            echo '# js tools PATH block begin'
            echo "export PATH=\"$repo/tools/bin:\$PATH\""
            [ -z "$bin_line" ] || echo "$bin_line"
            echo 'export COLORTERM=truecolor'
            echo '# js tools PATH block end'
        } >> "$rc"
        echo "added the js tools PATH block to $rc"
    done
    # verify the install took: the launcher starts this tree's js, and it is
    # the js PATH resolves.
    "$launcher" --help > /dev/null
    if [ -n "$bin_line" ]; then
        echo "ok: $launcher runs $repo/.venv/bin/js; open a new shell to put $bin on PATH"
    else
        shim="$(command -v js || true)"
        if [ "$shim" != "$launcher" ]; then
            echo "!! js on PATH is $shim, not $launcher — remove it or put $bin first on PATH" >&2
            exit 1
        fi
        echo "ok: $launcher runs $repo/.venv/bin/js"
    fi
    "$repo/.venv/bin/python" -m js.install

# Download js's pinned, checksummed subprocess binaries into tools/bin. The
# managed aria2c performs transfers after urllib bootstraps it.
# download js's pinned, checksummed CLI binaries into tools/bin.
install-tool-binaries:
    uv run {{ browser-extra }} python -m js.tool_binaries

# Provision managed binaries even when system copies exist; never use a package manager.
ensure-tools: install-tool-binaries

# The `browser` extra installs the playwright PYTHON package; the browser
# itself is a separate ~114MB download into ~/.cache/ms-playwright. Without it
# browser_probe fails at runtime with "Executable doesn't exist" even though
# the import succeeds. Idempotent: re-running with the browser present exits
# immediately. Skipped on musl, where the extra is not installed at all.
# download the Chromium build browser_probe drives.
install-browser:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{ browser-extra }}" ]; then
        echo "musl system: browser extra not installed, skipping chromium download"
        exit 0
    fi
    uv run {{ browser-extra }} python -m playwright install chromium

# remove the js launcher and the wiki symlink `just install` made, and a uv
# tool install of js from before the launcher.
uninstall:
    #!/usr/bin/env bash
    set -euo pipefail
    if grep -qF "# js launcher written by just install" "$HOME/.local/bin/js" 2>/dev/null; then
        rm -f "$HOME/.local/bin/js"
    fi
    if uv tool list 2>/dev/null | grep -q '^js '; then
        uv tool uninstall js
    fi
    rm -f "$HOME/.local/bin/wiki"

# ── testing ─────────────────────────────────────────────────────────────────

# the verified offline command from docs/testing-and-development.md.
# skips ai_provider (needs live creds), vision (needs a local vision model),
# and e2e (live end-to-end paths).
# offline suite — skips the live markers. Cached per tree state: an unchanged
# tree replays the last run. `just test --force` reruns.
test *args:
    scripts/cached-test.sh {{ args }} uv run {{ browser-extra }} --extra test pytest -q -m "not ai_provider and not vision and not e2e and not live" -p no:cacheprovider -n logical --dist worksteal

# run one test file or node. e.g. just test-file tests/test_picker.py
test-file file:
    uv run {{ browser-extra }} --extra test pytest -q {{ file }}

# run tests by pytest marker. e.g. just test-mark "not ai_provider"
test-mark marker:
    uv run {{ browser-extra }} --extra test pytest -q -m "{{ marker }}"

# live ai_provider suite — needs configured provider creds or a local
# OpenAI-compatible endpoint. e.g. AI_GATEWAY_API_KEY=... just test-live
# live ai_provider suite — needs provider creds or a local endpoint.
test-live:
    uv run {{ browser-extra }} --extra test pytest -q -m "ai_provider or live" tests/test_real_integrations.py tests/test_browse_obscura.py tests/test_browse_http_status.py tests/test_browse_delayed.py

# live vision suite — needs ollama + a pulled vision model. default gemma4:e4b,
# override with JS_VISION_TEST_MODEL=<tag>. e.g. just test-vision
# live vision suite — needs ollama with a vision model pulled.
test-vision:
    uv run {{ browser-extra }} --extra test pytest -q -m vision tests/test_real_integrations.py

# focused suites — mirror the groups in docs/testing-and-development.md
# tool descriptions + per-agent tool surface
test-tools:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_tool_descriptions.py tests/test_agent_tool_surface.py
# runtime loop: offline integration, tool runtime smoke, and the turn_* modules
test-runtime:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_runtime_offline_integration.py tests/test_tool_runtime_smoke.py tests/test_turn_stream.py tests/test_turn_surface.py tests/test_turn_budget.py tests/test_turn_call.py
# subagent isolation
test-subagents:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_subagent_isolation.py
# -p prompt mode + REPL harness
test-cli:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_cli_prompt_mode.py tests/test_repl_harness.py
# memory + config harness
test-memory:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_memory_config_harness.py
# wiki agents' deterministic native tools
test-wiki:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_wiki_native_tools.py

# ── quality ─────────────────────────────────────────────────────────────────
# ruff lives in the dev dependency-group, so `uv sync` installs it and it's on
# PATH inside the project env — js agents calling the shell tool can run
# `ruff check` / `ruff format` directly. config lives in pyproject ([tool.ruff]);
# the justfile only says what to run. mypy was tried and dropped: it flooded the
# dynamic codebase (ToolContext dynamic attrs, **kwargs splats, implicit
# optionals) with ~115 unactionable errors — not a useful gate here.

# days since uv.lock last changed; at this many the freshness gate fails.
# Days, not commits: a busy afternoon puts 20 commits on top of a lock that
# was relocked that morning, and a commit count calls that stale.
deps-stale-days := "14"

# ruff check: errors + pyflakes (defaults) + pyupgrade.
lint:
    uv run {{ browser-extra }} ruff check .

# apply ruff's safe auto-fixes (dequote annotations, deprecated-import updates,
# lru_cache->cache, etc.). does NOT remove unused imports (those may be
# re-exports — needs --unsafe-fixes + your judgment) and does NOT reformat.
# apply ruff's safe auto-fixes only — no import removal, no reformat.
fix:
    uv run {{ browser-extra }} ruff check --fix .

# ruff format in place. one-time full-repo adoption: rewrites ~110 files and
# collapses intentional comment alignment — run deliberately, review the diff,
# only if you want ruff's formatting.
# ruff format the whole repo in place — deliberate; review the diff.
format:
    uv run {{ browser-extra }} ruff format .

# fail once uv.lock has gone deps-stale-days without changing.
deps-fresh:
    #!/usr/bin/env bash
    set -euo pipefail
    limit="{{ deps-stale-days }}"
    # an empty hash means uv.lock has never been committed: stale by definition.
    changed="$(git log -1 --format=%ct -- uv.lock 2>/dev/null || true)"
    if [ -z "$changed" ]; then
        echo "uv.lock is not committed: run \`just upgrade\`, then run the suites and bump the version in pyproject.toml." >&2
        exit 1
    fi
    days=$(( ( $(date +%s) - changed ) / 86400 ))
    if [ "$days" -ge "$limit" ]; then
        echo "uv.lock has not changed in $days days (limit $limit): run \`just upgrade\` and \`just tools-upgrade\`, then run the suites and bump the version in pyproject.toml." >&2
        exit 1
    fi
    echo "deps fresh (uv.lock changed $days days ago)."

# quality gate = lint + dependency freshness. stops at the first failure.
check: lint deps-fresh
    @echo "quality ok."

# ── diagnostics ──────────────────────────────────────────────────────────────

# per-tool byte cost of model-facing descriptions and parameter schemas.
# forwards args: just tool-bytes --surface shell
tool-bytes *args:
    uv run {{ browser-extra }} python -m js.tooldiag {{ args }}

# move agents from 00-tools.yaml / 00*.md frontmatter to agent.yaml and drop
# tools entries that match no tool, also from existing agent.yaml files. dry
# run unless --apply; --show prints each agent.yaml. default root: the global
# agents dir. just migrate-agents --apply ~/.js/agents .js/agents
migrate-agents *args:
    uv run {{ browser-extra }} python scripts/migrate_agents.py {{ args }}

# move the old js config, data and inbox directories into ~/.js, convert the
# moved agents and file the old sessions by start directory. dry run unless
# --apply. js does this once by itself on first start.
migrate-home *args:
    uv run python -m js.home {{ args }}

# ── build / lockfile / housekeeping ─────────────────────────────────────────

# build sdist + wheel into dist/.
build:
    uv build

# relock deps against the current pyproject (no version upgrades).
lock:
    uv lock

# relock and bump every dep to the latest allowed by pyproject constraints.
upgrade:
    uv lock --upgrade

# show what an upgrade would change, without changing anything.
deps-outdated:
    uv lock --upgrade --dry-run

# report pinned managed binaries that trail their project's latest release.
# exits non-zero when any is stale, so it reads like deps-fresh does.
tools-outdated:
    uv run python scripts/refresh_tool_releases.py --check

# re-pin every managed binary to its latest release: downloads each platform's
# asset, hashes it and the executable inside, and rewrites the pins. Needs gh.
tools-upgrade:
    uv run python scripts/refresh_tool_releases.py

# show what moved upstream in each built-in skill (js/skills/*) since the commit
# its SOURCE: line names. upstream is a git checkout of that repo.
#   just skills-diff                 # ~/matt-skills
#   just skills-diff /path/to/skills
skills-diff upstream=(env_var('HOME') / "matt-skills"):
    #!/usr/bin/env bash
    set -euo pipefail
    up="{{ upstream }}"
    if ! git -C "$up" rev-parse --git-dir >/dev/null 2>&1; then
        echo "!! no git checkout at $up" >&2
        exit 1
    fi
    echo "upstream: $up at $(git -C "$up" rev-parse --short HEAD)"
    for skill_md in js/skills/*/SKILL.md; do
        name=$(basename "$(dirname "$skill_md")")
        source=$(sed -n 's/^SOURCE: //p' "$skill_md" | head -n 1)
        if [ -z "$source" ]; then
            echo "!! $name: no SOURCE: line" >&2
            continue
        fi
        rest=${source#*/tree/}
        commit=${rest%%/*}
        path=${rest#*/}
        if ! git -C "$up" cat-file -e "$commit^{commit}" 2>/dev/null; then
            echo "!! $name: commit ${commit:0:7} is not in $up (fetch it)" >&2
            continue
        fi
        if git -C "$up" diff --quiet "$commit" HEAD -- "$path"; then
            echo "$name: unchanged upstream since ${commit:0:7}"
        else
            echo "== $name: $path changed upstream since ${commit:0:7}"
            git -C "$up" --no-pager diff "$commit" HEAD -- "$path"
        fi
    done

# remove all generated/local build state (all of it is gitignored).
clean:
    -rm -rf build dist .coverage coverage.xml htmlcov .pytest_cache .ruff_cache
    -find . -type d -name __pycache__ -prune -exec rm -rf {} +
    -find . -type d -name '*.egg-info' -exec rm -rf {} +
    @echo "cleaned."

# ── tool bench (bench/toolbench) ────────────────────────────────────────────

# build the sandbox image: js wheel + rg/fd + python/node/go toolchains
toolbench-image:
    mkdir -p bench/toolbench/wheel && find bench/toolbench/wheel -name '*.whl' -delete
    uv build --wheel --out-dir bench/toolbench/wheel
    docker build -t js-toolbench:latest -f bench/toolbench/Dockerfile bench/toolbench

# mine and print the tasks each suite repo yields; no model, no sandbox
toolbench-mine *args:
    uv run python bench/toolbench/run.py --mine {{ args }}

# prove the plumbing: RepoRacer's fake agents through the sandbox on one task
toolbench-smoke *args:
    uv run python bench/toolbench/run.py --agents fake-success,fake-noop --tasks 1 --repos click {{ args }}

# the A/B: default agents (slim vs stock js) on every suite repo
toolbench *args:
    uv run python bench/toolbench/run.py {{ args }}

# rebuild the summary of a finished run: just toolbench-report bench/toolbench/results/<stamp>
toolbench-report dir:
    uv run python bench/toolbench/run.py --report {{ dir }}
