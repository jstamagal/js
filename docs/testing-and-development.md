# Testing And Development

The test suite is designed to exercise public harness behavior: CLI dispatch,
runtime tool loops, registry surfaces, subagent isolation, memory, wiki tools, and live proxy paths.

## Install Test Dependencies

```bash
UV_PROJECT_ENVIRONMENT=/tmp/js-private-venv just sync
```

Playwright does not publish musllinux wheels. On Alpine or another musl system,
omit `browser`; the browser-probe integration test skips while the rest of the
suite remains available.

Use a distinct `UV_PROJECT_ENVIRONMENT` for each checkout/audit, including on
subsequent uv/just commands. Sharing an editable environment lets another
checkout redirect imports and console scripts. Do not provision toolkit
executables with pip; those belong to the managed toolkit installer.

Offline tests use an autouse profile fixture: HOME is temporary, inherited XDG
overrides and JS configuration overrides are removed, and the runtime directory
is private. Tests can set explicit temporary overrides after fixture setup.
Subprocesses inherit this isolation. Also isolate HOME/XDG in the invoking shell
to protect import-time code before fixtures run; never use real session paths
to reproduce an inherited-path failure.

Playwright's Python package does not include its browser executable. Provision
the matching browser revision explicitly into a private managed cache (this is
a large download; obtain approval in resource-constrained environments):

```bash
UV_PROJECT_ENVIRONMENT=/tmp/js-private-venv \
PLAYWRIGHT_BROWSERS_PATH=/tmp/js-private-browsers \
uv run --extra browser python -m playwright install chromium --only-shell
```

Use that same `PLAYWRIGHT_BROWSERS_PATH` on browser test commands. An existing
matching cache can instead be copied into the private directory without a
download. Do not use `--with-deps` or install system packages. A missing browser
is a setup failure, not a reason to remove assertions or skip browser coverage.

For example:

```bash
uv run pytest --version
```

## Main Offline Suite

```bash
python -m pytest -m "not ai_provider and not vision and not e2e"
```

Verified in this environment:

```bash
python -m pytest -m "not ai_provider and not vision and not e2e" -p no:cacheprovider
```

## Focused Suites

Tool descriptions and canonical surface:

```bash
pytest -q tests/test_tool_descriptions.py tests/test_agent_tool_surface.py
```

Runtime and tool smoke tests:

```bash
pytest -q tests/test_runtime_offline_integration.py tests/test_tool_runtime_smoke.py
```

Subagents:

```bash
pytest -q tests/test_subagent_isolation.py
```

CLI modes:

```bash
pytest -q tests/test_cli_prompt_mode.py tests/test_repl_harness.py
```

Memory/config:

```bash
pytest -q tests/test_memory_config_harness.py
```

Wiki:

```bash
pytest -q tests/test_wiki_native_tools.py
```

## Live Tests

Tests marked `ai_provider` require configured `ai-python` provider credentials
or a local OpenAI-compatible endpoint:

```bash
AI_GATEWAY_API_KEY=... python -m pytest -m ai_provider tests/test_real_integrations.py
```

Vision tests require a reachable local vision model and image dependencies.

```bash
JS_PROVIDER=openai JS_BASE_URL=http://localhost:11434/v1 JS_API_KEY=ollama JS_VISION_TEST_MODEL=gemma4:e4b python -m pytest -m vision tests/test_real_integrations.py::test_live_vision_model_reads_image_through_real_message_path
```

Declared in `pyproject.toml`:

- `e2e`: complete user-visible runtime path.
- `ai_provider`: requires configured `ai-python` provider credentials or local endpoint.
- `vision`: requires local vision model and Pillow.
## Current Coverage Areas

The offline suite covers:

- CLI prompt mode, pipe mode, debug-file forwarding, reasoning/maxout overrides.
- `js --commit` default cwd, target dir, stdin/operator context, invalid combos.
- REPL reset/wipe/rollback behavior.
- Config env parsing and documented bytes env names.
- Session path confinement.
- Runtime context cap hydration.
- Claude provider-facing names with canonical persisted history.
- Tool retry limit final error behavior.
- Filesystem read/write/patch/remove/undo behavior, including multi-edit atomicity.
- Image result dehydration.
- Registry generation and tool selector behavior.
- Tool description file coverage.
- Subagent context/session/tool-surface isolation.
- Parallel subagent sibling failure preservation.
- Wiki conversion, guarded page writes, and ordered closeout.
- Artifact native tools.

## Development Style

Prefer tests through public behavior:

- CLI `main()` for CLI behavior.
- `runtime.run_turn()` with fake `js.model_client.stream_model` stubs for runtime behavior.
- real tool handlers with `ToolContext(tmp_path)` for tool behavior.
- `tmp_path` for filesystem isolation.

This project intentionally favors granular commits:

- implementation fixes separate from broad test additions
- docs separate from code and tests
- no compatibility churn unless explicitly intended

Do not include generated local state such as sessions, caches, `.venv`, or
unrelated lockfiles unless the repo intentionally starts tracking them.
