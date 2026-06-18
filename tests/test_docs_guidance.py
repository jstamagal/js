from __future__ import annotations

from pathlib import Path


def test_user_guide_describes_current_session_layout_and_compaction():
    text = Path("docs/user-guide.md").read_text(encoding="utf-8")

    assert "sessions/<agent_id>/<session>.jsonl" in text
    assert "~/.js" not in text
    assert "`compaction:{...}`" in text
    assert "`/compact [focus]`" in text
    assert "js --compact <session>" in text
    assert "`[compact].auto`" in text
    assert "expecting the harness to summarize old turns automatically" not in text


def test_models_and_providers_docs_match_current_model_and_provider_defaults():
    text = Path("docs/models-and-providers.md").read_text(encoding="utf-8")
    config_text = Path("docs/configuration-and-sessions.md").read_text(encoding="utf-8")

    assert "`deepseek/deepseek-v4-flash`" in text
    assert "`-m` / `--model` overrides the effective configured/env model for that run" in text
    assert "| `JS_MODEL` | env override for `[model].id` |" in config_text
    assert "`-m` / `--model` overrides the effective configured/env model for the selected\nrun or session." in config_text
    assert "repeated `--extra key.path=value`" in text
    assert "[provider.extra]" not in text
    assert "JS_PROVIDER" in text
    assert "JS_BASE_URL" in text
    assert "JS_API_KEY" in text
    assert "provider.id" in config_text
    assert "provider.base_url" in config_text
    assert "provider.api_key" in config_text
    assert "openai/opencode-go/deepseek-v4-pro" not in text
    assert "js --login openai-codex" in text
    assert "| `openai-codex` | ChatGPT/Codex OAuth provider" in text
    assert "OpenAI Codex OAuth is not advertised" not in text


def test_top_level_guidance_describes_model_env_alias_precedence():
    for path in ("README.md",):
        text = " ".join(Path(path).read_text(encoding="utf-8").split())

        assert "`JS_MODEL` overrides config" in text


def test_top_level_guidance_describes_runtime_layout():
    text = " ".join(Path("README.md").read_text(encoding="utf-8").split())

    assert "platform data `sessions/<agent_id>/<session>.jsonl`" in text
    assert "platform config `agents/`" in text
    assert "`skills/`" in text
    assert "platform data `state/`" in text
    assert "each agent has isolated session state" in text
    assert "append-only JSONL" in text
    assert "control marks" in text
    assert "~/.js" not in text
    assert "migrat" not in text.lower()


def test_user_guide_describes_model_flag_as_effective_config_override():
    text = Path("docs/user-guide.md").read_text(encoding="utf-8")

    assert "`-m` / `--model` overrides the effective configured/env model only for that\nrun." in text
    assert "Use another model for one run" in text


def test_drain_docs_describe_configured_model_precedence():
    text = Path("docs/drain.md").read_text(encoding="utf-8")

    assert "models.dev for the active model's" in text
    assert "same effective model that `js` would use" not in text
    assert "`JS_MODEL`" in text
    assert "`--model` overriding all of them" in text


def test_changelog_does_not_advertise_old_default_model():
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert "openai/opencode-go/deepseek-v4-pro" not in text
    assert "Replaced LiteLLM with the Vercel AI Python SDK" in text
    assert "Removed `[provider.extra]" in text
    assert "`[provider] id/base_url/api_key`" in text
    assert "via `JS_MODEL`" not in text
    assert "`litellm_proxy` pytest marker" in text
    assert "only set in the completion call for `openai/`-prefixed models" not in text


def test_config_source_comments_do_not_imply_js_model_sets_provider_base():
    text = Path("js/config.py").read_text(encoding="utf-8")

    assert "explicit js provider id" in text
    assert "OPENAI_API_BASE/JS_MODEL" not in text


def test_config_source_precedence_comment_uses_canonical_settings_wording():
    text = " ".join(Path("js/config.py").read_text(encoding="utf-8").split())

    assert "_settings.CANONICAL_CONFIG_PRECEDENCE" not in text
    assert "built-in default < config.toml < JS_* env" not in text
    assert "built-in default < ~/.js/config.toml" not in text


def test_settings_source_describes_cli_extras_as_current_override_layer():
    text = Path("js/settings.py").read_text(encoding="utf-8")

    assert "environment variables and CLI extras" in text
    assert "future CLI extras layer" not in text


def test_settings_collect_docstring_describes_ordered_config_paths():
    text = Path("js/settings.py").read_text(encoding="utf-8")

    assert "built-in defaults < config_paths in order < env < CLI extras" in text
    assert "built-in default < file < env < CLI" not in text


def test_settings_exports_canonical_config_precedence_for_generated_guidance():
    from js import settings

    assert settings.CANONICAL_CONFIG_PRECEDENCE == (
        "built-in defaults < platform config.toml < project .js/config.toml < "
        "project .js/config.local.toml < env vars < --extra CLI flag"
    )
    assert settings.TEMPLATE_CONFIG_PRECEDENCE == (
        "built-in defaults < this file < project .js/config.toml < "
        "project .js/config.local.toml < env vars < --extra CLI flag"
    )

    template = "\n".join(settings._template_lines())
    assert (
        f"# Precedence, lowest to highest: {settings.TEMPLATE_CONFIG_PRECEDENCE}."
        in template
    )

    config_source = Path("js/config.py").read_text(encoding="utf-8")
    assert "built-in default < ~/.js/config.toml" not in config_source


def test_prompt_agent_docs_describe_layered_agent_discovery():
    # only check docs that actually exist — a stale README link to a missing doc
    # must not snap this test (the dead link is a separate docs issue to fix once).
    doc_paths = [
        "README.md",
        "docs/user-guide.md",
        "docs/tools-reference.md",
        "docs/tool-system.md",
        "docs/technical-guide.md",
        "docs/subagents.md",
        "docs/porting-forge-tool-system-to-python.md",
    ]
    docs = {
        path: " ".join(Path(path).read_text(encoding="utf-8").split())
        for path in doc_paths
        if Path(path).exists()
    }
    assert docs, "expected at least one guidance doc to exist"

    layered = "repo `prompts/`, global `agents/` in the platform config dir, and project `.js/agents/`"
    for text in docs.values():
        assert layered in text
        assert "project scope wins over global, which wins over repo" in text.lower()

    stale = "\n".join(docs.values())
    assert "Agents live in `prompts/<agent_id>/*.md`" not in stale
    assert "generated agent tools from `prompts/<agent_id>`" not in stale
    assert "Generated agent tools come from directories under `prompts/`" not in stale
    assert "Prompt directories under `prompts/` become direct tools" not in stale
    assert "any `prompts/<agent_id>` directory" not in stale
    assert "Prompt directories become tools named after the directory" not in stale
    assert "prompts/<agent_id>/ 00-tools.md" not in stale


def test_top_level_guidance_mentions_provider_extra_overrides():
    for path in ("README.md",):
        text = " ".join(Path(path).read_text(encoding="utf-8").split())

        assert "Explicit `[provider] id/base_url/api_key` are opt-in only" in text
        assert "`JS_PROVIDER`" in text
        assert "`JS_BASE_URL`" in text
        assert "`JS_API_KEY`" in text
        assert "`[provider.extra]`" not in text
        assert "`--extra provider.extra" not in text


def test_top_level_guidance_describes_append_only_compaction_commands():
    text = " ".join(Path("README.md").read_text(encoding="utf-8").split())

    assert "`/compact [focus]`" in text
    assert "`/compact up to here`" in text
    assert "`js --compact <session>`" in text
    assert "append compaction marks" in text
    assert "rewriting history" in text


def test_top_level_guidance_describes_claude_provider_name_boundary():
    for path in ("README.md",):
        text = " ".join(Path(path).read_text(encoding="utf-8").split())

        assert "When the actual model string contains `claude`" in text
        assert "provider-facing" in text
        assert "session history stays canonical lowercase" in text


def test_top_level_guidance_rejects_legacy_tool_aliases():
    for path in ("README.md",):
        text = " ".join(Path(path).read_text(encoding="utf-8").split())

        assert "Do not reintroduce legacy aliases" in text
        for alias in (
            "`fs_read`",
            "`fs_write`",
            "`cat`",
            "`grep`",
            "`semantic_search`",
        ):
            assert alias in text
