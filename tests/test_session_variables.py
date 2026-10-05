from dataclasses import replace

import ai
import ai.types.usage
import pytest

from js import cli, logins, persona, runtime
from js.config import from_env
from js.model_client import ModelStreamResult
from js.promptexpand import expand_prompt


def test_builtin_values_expand_without_environment_variables(monkeypatch):
    monkeypatch.setenv("CURRENT_SESSION", "not-the-session")
    assert expand_prompt(
        "session=%%CURRENT_SESSION%%; unknown=%%UNKNOWN%%",
        variables={"CURRENT_SESSION": "2026-10-04T1947-31ef"},
    ) == "session=2026-10-04T1947-31ef; unknown=%%UNKNOWN%%"


def test_builtin_values_expand_inside_existing_inline_and_fenced_commands():
    text = "!{sh printf '%s' '%%CURRENT_SESSION%%'}\n```!sh\nprintf '%s' '%%CURRENT_SESSION_MODEL%%'\n```"
    assert expand_prompt(
        text, allow_code=True,
        variables={"CURRENT_SESSION": "31ef", "CURRENT_SESSION_MODEL": "opus:latest"},
    ) == "31ef\nopus:latest"


def test_builtin_values_and_command_output_are_not_rescanned(tmp_path):
    marker = tmp_path / "executed"
    directive = f"!{{sh touch {marker}}}"
    assert expand_prompt(
        "%%CURRENT_SESSION%%", allow_code=True,
        variables={"CURRENT_SESSION": directive},
    ) == directive
    assert expand_prompt(
        "!{sh printf '%s' '%%CURRENT_SESSION%%'}", allow_code=True,
        variables={"CURRENT_SESSION": "%%CURRENT_SESSION_MODEL%%", "CURRENT_SESSION_MODEL": "opus:latest"},
    ) == "%%CURRENT_SESSION_MODEL%%"
    assert not marker.exists()


def test_builtin_values_obey_prompt_escapes():
    assert expand_prompt(
        r"\%%CURRENT_SESSION%% `%%CURRENT_SESSION%%` %%CURRENT_SESSION%%",
        variables={"CURRENT_SESSION": "31ef"},
    ) == "%%CURRENT_SESSION%% `%%CURRENT_SESSION%%` 31ef"


def test_ordinary_system_files_have_current_session_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / ".js" / "agents" / "troop-matriarch"
    agent.mkdir(parents=True)
    (agent / "01-troop.md").write_text(
        "%%CURRENT_SESSION%%\n%%CURRENT_SESSION_FULLPATH%%\n"
        "%%CURRENT_SESSION_AGENT%%\n%%CURRENT_SESSION_MODEL%%\n", encoding="utf-8",
    )
    session = tmp_path / "2026-10-04T1947-31ef.jsonl"
    cfg = replace(
        from_env(save_session=False, agent_id="troop-matriarch"),
        session_file=session, model="opus:latest",
    )
    assert persona.load_configured_prompt_spec(cfg).system == (
        f"2026-10-04T1947-31ef\n{session}\ntroop-matriarch\nopus:latest\n"
    )


@pytest.mark.parametrize("cli_model", [None, "ollama/cli-model"])
def test_system_variable_uses_the_model_selected_for_the_run(tmp_path, monkeypatch, cli_model):
    monkeypatch.chdir(tmp_path)
    logins.save_login(logins.Login(provider_id="ollama"))
    agent = tmp_path / ".js" / "agents" / "voice"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("model: ollama/agent-model\n", encoding="utf-8")
    (agent / "01-role.md").write_text("model=%%CURRENT_SESSION_MODEL%%", encoding="utf-8")
    cfg = replace(from_env(save_session=False, agent_id="voice"), explicit_model=False)
    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    seen = []

    def stream(**kwargs):
        seen.append((kwargs["model_id"], kwargs["messages"][0].parts[0].text))
        return ModelStreamResult(
            text="ok", tool_calls=[], reasoning="",
            usage=ai.types.usage.Usage(input_tokens=0, output_tokens=1),
            finish_reason="stop", assistant_message=ai.assistant_message("ok"),
        )

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    assert cli._run_prompt("hello", model=cli_model, save=False) == 0
    model = "agent-model" if cli_model is None else "cli-model"
    assert seen == [(model, f"model={model}\n")]
