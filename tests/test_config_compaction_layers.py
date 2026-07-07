from __future__ import annotations


from js import memory, persona, runtime, settings
from js.capped_process import CappedProcessResult
from js.config import Config, from_env
from js.toolkit.registry import build_default_registry


def test_collect_settings_layers_global_project_and_local_with_env_cli(monkeypatch, tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    config_home = home / ".config"
    global_js = config_home / "js"
    global_js.mkdir(parents=True)
    (project / ".js").mkdir(parents=True)
    (global_js / "jsrc").write_text("set model.id global\nset limits.fetch_timeout_s 20\n", encoding="utf-8")
    (project / ".js" / "jsrc").write_text("set model.id project\nset limits.max_tool_iterations 9\n", encoding="utf-8")
    (project / ".js" / "jsrc.local").write_text("set model.id local\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("JS_MODEL", "env")
    monkeypatch.chdir(project)

    cfg = from_env(save_session=False, extras=["model.id=cli"])

    assert cfg.model == "cli"
    assert cfg.fetch_timeout_s == 20
    assert cfg.max_tool_iterations == 9
    assert cfg.prompt_roots[1:] == (
        global_js / "agents",
        project / ".js" / "agents",
    )
    assert cfg.prompt_roots[0].name == "prompts"


def test_default_fetch_timeout_and_template_cover_compact_wiki_artifact(tmp_path):
    out = settings.collect_settings(config_paths=[], env={})
    assert out["limits"]["fetch_timeout_s"] == settings.DEFAULT_FETCH_TIMEOUT_S
    assert out["limits"]["inline_code_timeout_s"] == settings.DEFAULT_INLINE_CODE_TIMEOUT_S
    target = tmp_path / "jsrc"
    settings.write_default_template(target)
    text = target.read_text(encoding="utf-8")
    template_keys = {
        line.split()[1]
        for line in text.splitlines()
        if line.startswith("#set ")
    }
    assert {
        "limits.inline_code_timeout_s",
        "compact.context_window",
        "compact.tail_tokens",
        "wiki.aliases",
        "artifact.dir",
    } <= template_keys


def test_prompt_spec_uses_most_specific_agent_and_stacks_agents_files(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    project = tmp_path / "project"
    (repo / "prompts" / "worker").mkdir(parents=True)
    (home / ".js" / "agents" / "worker").mkdir(parents=True)
    (project / ".js" / "agents" / "worker").mkdir(parents=True)
    (repo / "prompts" / "worker" / "01.md").write_text("REPO", encoding="utf-8")
    (home / ".js" / "agents" / "worker" / "01.md").write_text("GLOBAL", encoding="utf-8")
    (project / ".js" / "agents" / "worker" / "01.md").write_text("PROJECT", encoding="utf-8")
    for root, name in [(home / ".js", "GLOBAL"), (project, "PROJECT")]:
        (root / "AGENTS.md").write_text(f"{name} AGENTS", encoding="utf-8")
        (root / "AGENTS.local.md").write_text(f"{name} LOCAL", encoding="utf-8")

    spec = persona.load_agent_prompt_spec(
        "worker",
        repo_prompts_root=repo / "prompts",
        global_agents_root=home / ".js" / "agents",
        project_agents_root=project / ".js" / "agents",
        agents_files=[home / ".js" / "AGENTS.md", home / ".js" / "AGENTS.local.md", project / "AGENTS.md", project / "AGENTS.local.md"],
    )

    assert "PROJECT" in spec.system
    assert "GLOBAL\n" not in spec.system and "REPO\n" not in spec.system
    assert "GLOBAL AGENTS\n\nGLOBAL LOCAL\n\nPROJECT AGENTS\n\nPROJECT LOCAL" in spec.system


def test_agent_discovery_unions_roots_with_project_shadowing(tmp_path):
    repo = tmp_path / "repo"
    glob = tmp_path / "global"
    proj = tmp_path / "project"
    for root, body in [(repo, "repo"), (glob, "global"), (proj, "project")]:
        (root / "same").mkdir(parents=True)
        (root / "same" / "01.md").write_text(body, encoding="utf-8")
    (glob / "global_only").mkdir()
    (glob / "global_only" / "01.md").write_text("g", encoding="utf-8")
    (proj / "project_only").mkdir()
    (proj / "project_only" / "01.md").write_text("p", encoding="utf-8")

    reg = build_default_registry(prompts_root=[repo, glob, proj])

    names = set(reg.by_name)
    assert {"same", "global_only", "project_only"} <= names




def test_compaction_mark_rebuilds_summary_and_safe_tail_without_rewriting(tmp_path):
    session = tmp_path / "s.jsonl"
    msgs = [
        {"role":"user","content":"old"},
        {"role":"assistant","content":"","tool_calls":[{"id":"c1","type":"function","function":{"name":"read","arguments":"{}"}}]},
        {"role":"tool","tool_call_id":"c1","name":"read","content":"ok"},
        {"role":"assistant","content":"done"},
    ]
    for m in msgs:
        memory.append_message(session, m)
    before = session.read_text(encoding="utf-8")
    memory.append_compaction_mark(session, summary="Summary", keep_from=1)
    rebuilt = memory.load_messages(session)

    assert rebuilt[0] == {"role":"user", "content":"<compaction-summary>\nSummary\n</compaction-summary>"}
    assert [m["role"] for m in rebuilt[1:]] == ["assistant", "tool", "assistant"]
    assert session.read_text(encoding="utf-8").startswith(before)


def _compact_test_cfg(tmp_path, compact: dict) -> Config:
    return Config(
        agent_id="compact",
        agent_dir=tmp_path / ".js" / "sessions" / "compact",
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "compact",
        session_file=tmp_path / ".js" / "sessions" / "compact" / "s.jsonl",
        prompts_dir=tmp_path / "prompts",
        settings={"compact": compact},
    )


def test_compact_messages_invalid_numeric_settings_fall_back(monkeypatch, tmp_path):
    cfg = _compact_test_cfg(
        tmp_path,
        {
            "chars_per_token": "bad-ratio",
            "tail_tokens": "bad-tail",
            "min_savings_tokens": "bad-min",
        },
    )
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "done"},
    ]
    monkeypatch.setattr(runtime, "_summarize_for_compaction", lambda *a, **kw: "Summary")

    result = runtime.compact_messages(cfg, "SYSTEM", messages, forced=True)

    assert result.startswith("compacted:")
    assert messages[0]["content"] == "<compaction-summary>\nSummary\n</compaction-summary>"



def test_summarize_invalid_summary_max_tokens_falls_back(monkeypatch, tmp_path):
    captured: list[object] = []

    def summarize_stub(cfg, model, messages, focus, guidance):
        captured.append(cfg.settings.get("compact", {}).get("summary_max_tokens", "missing"))
        return "Summary"

    monkeypatch.setattr(runtime, "_summarize_for_compaction", summarize_stub)

    for raw in ("bad-max", True):
        cfg = _compact_test_cfg(tmp_path, {"summary_max_tokens": raw})
        actual = runtime._summarize_for_compaction(cfg, "offline-test-model", [], "", "")
        assert actual == "Summary", f"expected 'Summary' for {raw}"

    # The fallback in _compact_int_setting returns 4096 when max_value=8192 for bad values
    assert captured == ["bad-max", True]


def test_compact_pre_hook_ignores_malformed_and_blank_values(monkeypatch, tmp_path):
    calls: list[str] = []

    def run_stub(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError(f"pre_hook should not run for malformed value: {cmd!r}")

    monkeypatch.setattr(runtime, "_run_capped", run_stub)

    for raw in (["echo hi"], {"cmd": "echo hi"}, 123, "   "):
        cfg = _compact_test_cfg(tmp_path, {"pre_hook": raw})
        assert runtime._run_compact_pre_hook(cfg) == ""

    assert calls == []


def test_compact_pre_hook_trims_valid_command(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def run_stub(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return CappedProcessResult(returncode=0, stdout=b"hook guidance\n", stderr=b"")

    monkeypatch.setattr(runtime, "_run_capped", run_stub)
    cfg = _compact_test_cfg(tmp_path, {"pre_hook": "  echo guidance  "})

    assert runtime._run_compact_pre_hook(cfg) == "hook guidance"
    assert captured["cmd"][-2:] == ["-c", "echo guidance"]
    assert captured["timeout"] == 30


def test_compact_pre_hook_output_is_capped_and_marked(monkeypatch, tmp_path):
    def run_stub(cmd, **kwargs):
        assert kwargs["cap"] == 128
        return CappedProcessResult(
            returncode=0,
            stdout=b"x" * 128,
            stderr=b"",
            stdout_truncated=True,
        )

    monkeypatch.setattr(runtime, "_run_capped", run_stub)
    cfg = _compact_test_cfg(tmp_path, {"pre_hook": "yes"})
    cfg = Config(**{**cfg.__dict__, "max_bash_output_bytes": 128})

    actual = runtime._run_compact_pre_hook(cfg)

    assert actual.startswith("x" * 128)
    assert "[truncated: limits.max_bash_output_bytes (128) reached]" in actual

def test_compact_model_same_is_normalized_and_malformed_values_fall_back(monkeypatch, tmp_path):
    seen_models: list[str] = []

    def summarize_stub(cfg, model, messages, focus, guidance):
        seen_models.append(model)
        return f"Summary from {model}"

    monkeypatch.setattr(runtime, "_summarize_for_compaction", summarize_stub)

    for raw in (" SAME ", "same", 123):
        cfg = _compact_test_cfg(tmp_path, {"model": raw})
        messages = [{"role": "user", "content": "old"}]
        runtime.compact_messages(cfg, "SYSTEM", messages, forced=True)

    assert seen_models == ["offline-test-model", "offline-test-model", "offline-test-model"]
