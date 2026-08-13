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
browser-target := `if ldd --version 2>&1 | grep -qi musl; then printf '%s' '.'; else printf '%s' '.[browser]'; fi`

# show all recipes (default when `just` is called with no argument)
default:
    @just --list

# ── run the harness ─────────────────────────────────────────────────────────

#   just run -p "summarize this repo"
#   just run --commit
# run js — no args opens the REPL; any js flags/args pass through.
run *args:
    uv run {{ browser-extra }} js {{ args }}

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
# feels off.
# rebuild the env from uv.lock — the real fix for a broken venv.
sync:
    uv sync --extra test {{ browser-extra }}

# drop into a shell with the project env active (uv owns the venv).
shell:
    uv run {{ browser-extra }} bash

# install `js` onto PATH as launchers shebanged to a managed venv,
# editable so they track the working tree (no reinstall after a code edit). uv
# puts the launchers in its tool bin dir — usually ~/.local/bin. Also downloads
# js's pinned CLI binaries into js/tools and provisions optional interactive
# helpers (fd/bat/fzf). NOTE: the tool venv is resolved from pyproject
# constraints, not uv.lock, so its dep versions can drift from `just run`'s
# env until the next `just install`.
#   just install   then   js -p "hi"   from anywhere
# put js + wiki on PATH as editable launchers, with pinned tool binaries.
install:
    #!/usr/bin/env bash
    set -euo pipefail
    # refuse to install from a linked worktree: the editable install and the
    # wiki symlink would point at a tree that vanishes when the worktree is
    # cleaned up, leaving `js` and `wiki` broken everywhere.
    if [ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]; then
        echo "!! this is a linked git worktree — run 'just install' from the main checkout:" >&2
        echo "!!   $(dirname "$(git rev-parse --git-common-dir)")" >&2
        exit 1
    fi
    uv tool install --force --editable "{{ browser-target }}"
    mkdir -p "$HOME/.local/bin"
    ln -sf "$(pwd)/tools/wiki" "$HOME/.local/bin/wiki"
    just install-tool-binaries
    just ensure-tools
    # verify the install took: whatever `js` PATH resolves must load code from
    # THIS working tree, or an old/foreign install is still answering.
    repo="$(pwd -P)"
    shim="$(command -v js || true)"
    if [ -z "$shim" ]; then
        echo "!! js not on PATH after install — run: uv tool update-shell" >&2
        exit 1
    fi
    pybin="$(sed -n '1s/^#!//p' "$shim")"
    if [ ! -x "$pybin" ]; then
        echo "!! $shim is not a uv tool shim (foreign install shadowing PATH?) — remove it and rerun" >&2
        exit 1
    fi
    loaded="$("$pybin" -c 'import js, pathlib; print(pathlib.Path(js.__file__).resolve().parent)')"
    case "$loaded" in
        "$repo"/*) echo "ok: $shim loads $loaded — editable, tracks this tree (deps changes still need a rerun of: just install)" ;;
        *)
            echo "!! STALE INSTALL: $shim loads $loaded, NOT this tree ($repo)." >&2
            echo "!! an old or non-editable install is still active — uv tool uninstall js, then rerun: just install" >&2
            exit 1
            ;;
    esac

# Download js's pinned, checksummed subprocess binaries into js/tools. The
# system aria2c performs the release-asset transfers.
# download js's pinned, checksummed CLI binaries into js/tools.
install-tool-binaries:
    uv run {{ browser-extra }} python -m js.tool_binaries

# ensure optional interactive CLI helpers are present, installing any that are
# missing via the detected package manager. fd, bat, and fzf back file-finding
# and interactive helpers. idempotent and safe to run on its own.
# ensure fd/bat/fzf exist, installing via the system package manager.
ensure-tools:
    #!/usr/bin/env bash
    set -euo pipefail
    have() { command -v "$1" >/dev/null 2>&1; }
    need=()
    have fd  || have fdfind || need+=(fd)
    have bat || have batcat || need+=(bat)
    have fzf || need+=(fzf)
    if [ ${#need[@]} -eq 0 ]; then
      echo "interactive cli tools present: fd bat fzf"
      exit 0
    fi
    echo "provisioning missing cli tools: ${need[*]}"
    if   have pacman;  then MGR=pacman; INSTALL=(sudo pacman -S --needed --noconfirm)
    elif have apt-get; then MGR=apt;    INSTALL=(sudo apt-get install -y)
    elif have dnf;     then MGR=dnf;    INSTALL=(sudo dnf install -y)
    elif have zypper;  then MGR=zypper; INSTALL=(sudo zypper install -y)
    elif have apk;     then MGR=apk;    INSTALL=(sudo apk add)
    elif have brew;    then MGR=brew;   INSTALL=(brew install)
    else
      echo "!! no supported package manager found (pacman/apt/dnf/zypper/apk/brew)."
      echo "!! install these yourself, then re-run 'just install': ${need[*]}"
      exit 0
    fi
    pkgs=()
    for b in "${need[@]}"; do
      case "$b:$MGR" in
        fd:apt|fd:dnf) pkgs+=(fd-find) ;;
        fd:*)        pkgs+=(fd) ;;
        bat:*)       pkgs+=(bat) ;;
        fzf:*)       pkgs+=(fzf) ;;
      esac
    done
    echo "+ ${INSTALL[*]} ${pkgs[*]}"
    "${INSTALL[@]}" "${pkgs[@]}" || {
      echo "!! auto-install failed; run manually: ${INSTALL[*]} ${pkgs[*]}"
      exit 0
    }

# remove the installed js launchers and the wiki symlink `just install` made.
uninstall:
    uv tool uninstall js
    rm -f "$HOME/.local/bin/wiki"

# ── testing ─────────────────────────────────────────────────────────────────

# the verified offline command from docs/testing-and-development.md.
# skips ai_provider (needs live creds), vision (needs a local vision model),
# and e2e (live end-to-end paths).
# offline suite — skips the live markers (ai_provider, vision, e2e).
test:
    uv run {{ browser-extra }} --extra test pytest -q -m "not ai_provider and not vision and not e2e" -p no:cacheprovider

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
    uv run {{ browser-extra }} --extra test pytest -q -m ai_provider tests/test_real_integrations.py

# live vision suite — needs ollama + a pulled vision model. default gemma4:e4b,
# override with JS_VISION_TEST_MODEL=<tag>. e.g. just test-vision
# live vision suite — needs ollama with a vision model pulled.
test-vision:
    uv run {{ browser-extra }} --extra test pytest -q -m vision tests/test_real_integrations.py

# focused suites — mirror the groups in docs/testing-and-development.md
# tool descriptions + per-agent tool surface
test-tools:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_tool_descriptions.py tests/test_agent_tool_surface.py
# runtime loop: offline integration + tool runtime smoke
test-runtime:
    uv run {{ browser-extra }} --extra test pytest -q tests/test_runtime_offline_integration.py tests/test_tool_runtime_smoke.py
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

# quality gate = lint. stops at the first failure.
check: lint
    @echo "quality ok."

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

# remove all generated/local build state (all of it is gitignored).
clean:
    -rm -rf build dist .coverage coverage.xml htmlcov .pytest_cache .ruff_cache
    -find . -type d -name __pycache__ -prune -exec rm -rf {} +
    -find . -type d -name '*.egg-info' -exec rm -rf {} +
    @echo "cleaned."
