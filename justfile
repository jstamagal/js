# js — task runner over uv.
#
# `uv run` auto-syncs the project env from uv.lock on every invocation, so the
# venv, the `js` console script, and all deps are always present and correct.
# You never activate a venv, never `pip install`, never call `.venv/bin/js`
# (that path breaks the moment the package isn't installed into the venv — the
# whole `.venv/bin/js` dance is what this file replaces). This justfile is the
# single workflow entry point for the repo.
#
# `just` with no arg lists recipes. Pass-through recipes (run/drain/commit)
# forward everything after the recipe name, so `just run -p "summarize this"`
# reaches js unchanged.

set dotenv-load

# show all recipes (default when `just` is called with no argument)
default:
    @just --list

# ── run the harness ─────────────────────────────────────────────────────────

# run js. no args -> interactive REPL. pass any js flags/args through.
#   just run -p "summarize this repo"
#   just run --commit
# just run --wiki=ingest --vault=creative ~/notes/source.md
run *args:
    uv run js {{ args }}

# run js-drain (drain jobs). e.g. just drain creative -a
drain *args:
    uv run js-drain {{ args }}

# Commit workflow is deliberately plain: run `js --commit` from repo root.
# Do not pass -p, a target path, or a message; the commit agent inspects/stages/messages.
# This recipe exists only as a no-arg convenience and rejects arguments.
commit:
    uv run js --commit

# ── env / deps ───────────────────────────────────────────────────────────────

# sync the project env from uv.lock, including the test extra. idempotent —
# run after a fresh clone, after pulling changed deps, or any time the env
# feels off. this is the real fix for "the venv is broken": it rebuilds it from
# the lockfile.
sync:
    uv sync --extra test

# drop into a shell with the project env active (uv owns the venv).
shell:
    uv run bash

# install `js` + `js-drain` onto PATH as launchers shebanged to a managed venv,
# editable so they track the working tree (no reinstall after a code edit). uv
# puts the launchers in its tool bin dir — usually ~/.local/bin.
#   just install   then   js -p "hi"   from anywhere
install:
    uv tool install --force --editable .
    @echo "installed js + js-drain. if they aren't on PATH yet: uv tool update-shell"

# remove the installed js launchers.
uninstall:
    uv tool uninstall js

# ── testing ─────────────────────────────────────────────────────────────────

# offline suite — the verified command from docs/testing-and-development.md.
# skips ai_provider (needs live creds) and vision (needs a local vision model).
test:
    uv run --extra test pytest -m "not ai_provider and not vision" -p no:cacheprovider

# run one test file or node. e.g. just test-file tests/test_picker.py
test-file file:
    uv run --extra test pytest -q {{ file }}

# run tests by pytest marker. e.g. just test-mark "not ai_provider"
test-mark marker:
    uv run --extra test pytest -q -m "{{ marker }}"

# live ai_provider suite — needs configured provider creds or a local
# OpenAI-compatible endpoint. e.g. AI_GATEWAY_API_KEY=... just test-live
test-live:
    uv run --extra test pytest -q -m ai_provider tests/test_real_integrations.py

# live vision suite — needs ollama + a pulled vision model. default gemma4:e4b,
# override with JS_VISION_TEST_MODEL=<tag>. e.g. just test-vision
test-vision:
    uv run --extra test pytest -q -m vision tests/test_real_integrations.py

# focused suites — mirror the groups in docs/testing-and-development.md
test-tools:
    uv run --extra test pytest -q tests/test_tool_descriptions.py tests/test_agent_tool_surface.py
test-runtime:
    uv run --extra test pytest -q tests/test_runtime_offline_integration.py tests/test_tool_runtime_smoke.py
test-subagents:
    uv run --extra test pytest -q tests/test_subagent_isolation.py
test-cli:
    uv run --extra test pytest -q tests/test_cli_prompt_mode.py tests/test_repl_harness.py
test-memory:
    uv run --extra test pytest -q tests/test_memory_config_harness.py
test-wiki:
    uv run --extra test pytest -q tests/test_wiki_native_tools.py tests/test_artifact_native_tools.py tests/test_drain_harness.py

# ── quality ─────────────────────────────────────────────────────────────────
# ruff lives in the dev dependency-group, so `uv sync` installs it and it's on
# PATH inside the project env — js agents calling the shell tool can run
# `ruff check` / `ruff format` directly. config lives in pyproject ([tool.ruff]);
# the justfile only says what to run. mypy was tried and dropped: it flooded the
# dynamic codebase (ToolContext dynamic attrs, **kwargs splats, implicit
# optionals) with ~115 unactionable errors — not a useful gate here.

# ruff check: errors + pyflakes (defaults) + pyupgrade.
lint:
    uv run ruff check .

# apply ruff's safe auto-fixes (dequote annotations, deprecated-import updates,
# lru_cache->cache, etc.). does NOT remove unused imports (those may be
# re-exports — needs --unsafe-fixes + your judgment) and does NOT reformat.
fix:
    uv run ruff check --fix .

# ruff format in place. one-time full-repo adoption: rewrites ~110 files and
# collapses intentional comment alignment — run deliberately, review the diff,
# only if you want ruff's formatting.
format:
    uv run ruff format .

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
    -rm -rf build dist .coverage coverage.xml htmlcov .pytest_cache .ruff_cache .mypy_cache
    -find . -type d -name __pycache__ -prune -exec rm -rf {} +
    -find . -type d -name '*.egg-info' -exec rm -rf {} +
    @echo "cleaned."
