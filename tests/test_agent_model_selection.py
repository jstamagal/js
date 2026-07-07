from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import ai
import pytest
import ai.types.usage

from js import cli, logins, persona, runtime
from js.config import Config
from js.model_client import ModelStreamResult
from js.toolkit import ToolContext
from js.toolkit.meta import task


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OLLAMA_BASE_URL = "http://parent-ollama.test/v1"


@pytest.fixture(autouse=True)
def isolated_provider_state(monkeypatch, tmp_path):
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", tmp_path / "login-store")
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "OLLAMA_API_KEY",
        "OLLAMA_LOCAL_API_KEY",
        "OLLAMA_BASE_URL",
        "OLLAMA_LOCAL_BASE_URL",
        "OLLAMA_MODEL",
        "OLLAMA_LOCAL_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def _save_login(provider_id: str, **kw) -> None:
    """Authorize routing to ``provider_id`` — a `provider/model` prefix routes only
    when the operator has logged in (an env key alone never creates a route)."""
    logins.save_login(logins.Login(provider_id=provider_id, **kw))


def _fake_stream_result(text: str = "ok") -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _write_agent_dir(
    root: Path,
    agent_id: str = "worker",
    manifest: str = "tools: []\n",
    body: str = "WORKER SYSTEM\n",
) -> Path:
    prompts = root / agent_id
    prompts.mkdir(parents=True)
    (prompts / "00-tools.yaml").write_text(manifest, encoding="utf-8")
    (prompts / "01-body.md").write_text(body, encoding="utf-8")
    return prompts


def _make_cfg(
    tmp_path: Path,
    prompts_dir: Path,
    *,
    model: str = "parent-model",
    provider_id: str | None = None,
    provider_base_url: str | None = None,
    provider_api_key: str | None = None,
    provider_headers: dict[str, str] | None = None,
    explicit_model: bool = False,
    prefer_inherit: bool = False,
    lock_subagent_model: bool = False,
    agents_files: tuple[Path, ...] = (),
) -> Config:
    agent_dir = tmp_path / ".js" / "sessions" / "parent"
    return Config(
        agent_id="parent",
        agent_dir=agent_dir,
        model=model,
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
        provider_headers=provider_headers or {},
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=agent_dir / ".history",
        sessions_dir=agent_dir,
        session_file=agent_dir / "parent.jsonl",
        prompts_dir=prompts_dir,
        explicit_model=explicit_model,
        prompt_roots=(prompts_dir.parent,),
        agents_files=agents_files,
        project_dir=tmp_path,
        prefer_inherit=prefer_inherit,
        lock_subagent_model=lock_subagent_model,
    )


def _capture_single_task_route(
    monkeypatch,
    tmp_path: Path,
    cfg: Config,
    *,
    model_arg: str = "",
) -> dict[str, object]:
    seen: dict[str, object] = {}

    def stream_stub(**kwargs):
        seen.update(kwargs)
        return _fake_stream_result("CHILD_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    monkeypatch.setattr(runtime, "_resolve_max_output", lambda _model, _provider_id: None)

    context = ToolContext(cwd=tmp_path)
    context.config = cfg
    actual = task(["do the child work"], agent_id="worker", model=model_arg, context=context)

    assert "CHILD_OK" in actual
    return seen


def test_apply_agent_model_uses_frontmatter_model_and_prefixed_provider_route(tmp_path):
    prompts = _write_agent_dir(
        tmp_path / "prompts",
        "agent-model",
        "model: deepseek/agent-frontmatter-model\ntools: []\n",
    )
    cfg = _make_cfg(tmp_path, prompts)
    prompt_spec = persona.load_prompt_spec(prompts)
    _save_login("deepseek")

    actual = cli._apply_agent_model(cfg, prompt_spec, None)

    assert actual.model == "agent-frontmatter-model"
    assert actual.provider_id == "deepseek"
    assert actual.provider_base_url == DEEPSEEK_BASE_URL
    assert cfg.model == "parent-model"
    assert cfg.provider_id is None


@pytest.mark.parametrize(
    ("model_arg", "explicit_model"),
    [("operator-model", False), (None, True)],
)
def test_operator_pinned_model_overrides_agent_frontmatter_model(tmp_path, model_arg, explicit_model):
    prompts = _write_agent_dir(
        tmp_path / "prompts",
        "agent-model",
        "model: deepseek/agent-frontmatter-model\ntools: []\n",
    )
    cfg = _make_cfg(tmp_path, prompts, explicit_model=explicit_model)
    prompt_spec = persona.load_prompt_spec(prompts)

    actual = cli._apply_agent_model(cfg, prompt_spec, model_arg)

    assert actual is cfg


def test_apply_agent_model_noops_without_frontmatter_model(tmp_path):
    prompts = _write_agent_dir(tmp_path / "prompts", "agent-model", "tools: []\n")
    cfg = _make_cfg(tmp_path, prompts)
    prompt_spec = persona.load_prompt_spec(prompts)

    actual = cli._apply_agent_model(cfg, prompt_spec, None)

    assert actual is cfg


def test_subagent_prefixed_model_reroutes_and_bare_model_keeps_parent_provider(monkeypatch, tmp_path):
    prefixed_prompts = _write_agent_dir(
        tmp_path / "prefixed" / "prompts",
        manifest="model: deepseek/subagent-routed-model\ntools: []\n",
    )
    prefixed_cfg = _make_cfg(tmp_path, prefixed_prompts, provider_id=None)
    _save_login("deepseek")

    prefixed = _capture_single_task_route(monkeypatch, tmp_path, prefixed_cfg)

    assert prefixed["model_id"] == "subagent-routed-model"
    assert prefixed["provider_id"] == "deepseek"
    assert prefixed["provider_base_url"] == DEEPSEEK_BASE_URL

    bare_prompts = _write_agent_dir(
        tmp_path / "bare" / "prompts",
        manifest="model: subagent-bare-model\ntools: []\n",
    )
    bare_cfg = _make_cfg(
        tmp_path,
        bare_prompts,
        provider_id="ollama",
        provider_base_url=OLLAMA_BASE_URL,
        provider_api_key="parent-key",
        provider_headers={"x-parent": "1"},
    )

    bare = _capture_single_task_route(monkeypatch, tmp_path, bare_cfg)

    assert bare["model_id"] == "subagent-bare-model"
    assert bare["provider_id"] == "ollama"
    assert bare["provider_base_url"] == OLLAMA_BASE_URL
    assert bare["provider_api_key"] == "parent-key"
    assert bare["provider_headers"] == {"x-parent": "1"}


def test_subagent_model_precedence_tool_arg_then_prefer_inherit(monkeypatch, tmp_path):
    prompts = _write_agent_dir(
        tmp_path / "precedence" / "prompts",
        manifest="model: deepseek/frontmatter-model\ntools: []\n",
    )
    cfg = _make_cfg(tmp_path, prompts, provider_id=None)
    _save_login("deepseek")

    tool_arg = _capture_single_task_route(
        monkeypatch,
        tmp_path,
        cfg,
        model_arg="deepseek/tool-call-model",
    )

    assert tool_arg["model_id"] == "tool-call-model"
    assert tool_arg["provider_id"] == "deepseek"

    inherit_cfg = replace(
        cfg,
        model="parent-inherited-model",
        provider_id="ollama",
        provider_base_url=OLLAMA_BASE_URL,
        provider_api_key="parent-key",
        prefer_inherit=True,
    )

    inherited = _capture_single_task_route(monkeypatch, tmp_path, inherit_cfg)

    assert inherited["model_id"] == "parent-inherited-model"
    assert inherited["provider_id"] == "ollama"
    assert inherited["provider_base_url"] == OLLAMA_BASE_URL


def test_lock_subagent_model_removes_task_model_schema_property(tmp_path):
    prompts = _write_agent_dir(tmp_path / "prompts", "parent", "tools: []\n")
    cfg = _make_cfg(tmp_path, prompts)

    unlocked_tool = cli._registry_for(cfg).resolve("task")
    locked_tool = cli._registry_for(replace(cfg, lock_subagent_model=True)).resolve("task")

    assert unlocked_tool is not None
    assert locked_tool is not None
    assert "model" in unlocked_tool.openai_spec()["function"]["parameters"]["properties"]
    assert "model" not in locked_tool.openai_spec()["function"]["parameters"]["properties"]


def test_subagent_frontmatter_model_survives_agents_file_prepend(monkeypatch, tmp_path):
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("PARENT AGENTS INSTRUCTIONS\n", encoding="utf-8")
    prompts = _write_agent_dir(
        tmp_path / "prompts",
        manifest="model: deepseek/agents-file-model\ntools: []\n",
        body="WORKER BODY\n",
    )
    cfg = _make_cfg(tmp_path, prompts, agents_files=(agents_file,))
    _save_login("deepseek")

    seen = _capture_single_task_route(monkeypatch, tmp_path, cfg)
    system = seen["messages"][0].parts[0].text

    assert seen["model_id"] == "agents-file-model"
    assert seen["provider_id"] == "deepseek"
    assert "PARENT AGENTS INSTRUCTIONS" in system
    assert "WORKER BODY" in system

def test_prefixed_model_overrides_pinned_parent_provider():
    from js.routing import ProviderNotLoggedInError, resolve_model_route

    # An explicit agent/subagent prefixed model the operator LOGGED INTO overrides a
    # differently-pinned parent provider, and does NOT inherit the parent's base/key.
    _save_login("deepseek")
    route = resolve_model_route(
        "deepseek/deepseek-v4-flash",
        configured_provider_id="ollama",
        configured_base_url=OLLAMA_BASE_URL,
        configured_api_key="ollama-parent-key",
        explicit_model=True,
        use_saved_login=False,
        prefix_overrides_provider=True,
    )
    assert route.provider_id == "deepseek"
    assert route.model == "deepseek-v4-flash"
    assert route.base_url != OLLAMA_BASE_URL
    assert route.api_key != "ollama-parent-key"

    # An invocation-explicit provider prefix is authoritative. If it names a
    # different known provider without a login, fail instead of riding the stale
    # pinned provider.
    with pytest.raises(ProviderNotLoggedInError):
        resolve_model_route(
            "anthropic/claude-sonnet-4",
            configured_provider_id="ollama",
            configured_base_url=OLLAMA_BASE_URL,
            configured_api_key="ollama-parent-key",
            explicit_model=True,
            use_saved_login=False,
            prefix_overrides_provider=True,
        )


def test_saved_login_prefix_overrides_pinned_provider_without_flag():
    from js.routing import resolve_model_route

    # The operator logged into opencode-go and asks for opencode-go/glm-5.1 while
    # a stale `provider.id=deepseek` sits pinned in jsrc. The saved login makes
    # the prefix authoritative even without prefix_overrides_provider and even
    # with use_saved_login=False (the live REPL state path) — and it carries the
    # login's base/key, not deepseek's.
    logins.save_login(logins.Login(
        provider_id="opencode-go",
        provider_api_key="sk-oc",
        provider_base_url="https://opencode.ai/zen/go/v1",
    ))
    route = resolve_model_route(
        "opencode-go/glm-5.1",
        configured_provider_id="deepseek",
        configured_base_url=DEEPSEEK_BASE_URL,
        configured_api_key="sk-deepseek",
        explicit_model=True,
        use_saved_login=False,
    )
    assert route.provider_id == "opencode-go"
    assert route.model == "glm-5.1"
    assert route.base_url == "https://opencode.ai/zen/go/v1"
    assert route.api_key == "sk-oc"


def test_unlogged_vendor_prefix_does_not_hijack_pinned_gateway():
    from js.routing import resolve_model_route

    # Gateway protection: a vendor prefix the operator never logged into yields
    # to the pinned gateway (no saved `anthropic` login -> no override).
    route = resolve_model_route(
        "anthropic/claude-sonnet-4",
        configured_provider_id="omp",
        configured_base_url="https://gateway.test/v1",
        configured_api_key="sk-omp",
        explicit_model=True,
        use_saved_login=False,
    )
    assert route.provider_id == "omp"
    assert route.model == "anthropic/claude-sonnet-4"
    assert route.base_url == "https://gateway.test/v1"


def test_env_key_alone_creates_no_route_and_errors():
    from js.routing import ProviderNotLoggedInError, resolve_model_route

    # RULING #1: a provider-native env key never creates a route. The default
    # model prefixes deepseek; with DEEPSEEK_API_KEY set but no login and nothing
    # pinned, routing must fail loud rather than farm the key.
    with pytest.raises(ProviderNotLoggedInError) as excinfo:
        resolve_model_route(
            "deepseek/deepseek-v4-flash",
            env={"DEEPSEEK_API_KEY": "sk-decoy"},
        )
    assert "deepseek" in str(excinfo.value)
    assert "not logged in" in str(excinfo.value)


def test_catalog_prefix_with_env_token_but_no_login_errors():
    from js.routing import ProviderNotLoggedInError, resolve_model_route

    # A catalog provider (models.dev) is KNOWN, so its prefix splits — but an env
    # token (HF_TOKEN) does not authorize it. No login, nothing pinned -> error.
    with pytest.raises(ProviderNotLoggedInError) as excinfo:
        resolve_model_route(
            "huggingface/some-org/some-model",
            env={"HF_TOKEN": "hf-decoy"},
        )
    assert "huggingface" in str(excinfo.value)
    assert "js --login huggingface" in str(excinfo.value)


def test_js_namespace_env_routes_without_any_login():
    from js.routing import resolve_model_route

    # RULING carve-out: JS_* vars are explicit js-directed instructions, not
    # provider-native env farming. They arrive as configured provider.id/base/key
    # and must route with no login at all.
    route = resolve_model_route(
        "custom-model",
        configured_provider_id="openai",
        configured_base_url="http://js-directed.test/v1",
        configured_api_key="sk-js",
        env={},
        explicit_model=True,
    )
    assert route.provider_id == "openai"
    assert route.model == "custom-model"
    assert route.base_url == "http://js-directed.test/v1"
    assert route.api_key == "sk-js"
