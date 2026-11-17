"""The Continue command replays the resolved route and invocation overrides."""

import shlex

import ai
import pytest

from js import cli, logins, runtime
from js.model_client import ModelStreamResult


class Terminal:
    def isatty(self):
        return True


def _command(err):
    prefix = cli.msgs.CONTINUE_HINT.text(command="")
    line = next(line for line in err.splitlines() if prefix in line)
    return shlex.split(line.split(prefix, 1)[1])


def _requests(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kw: ModelStreamResult(
        text="answer", tool_calls=[], reasoning="", usage=None, finish_reason="stop",
        assistant_message=ai.assistant_message("answer")))
    requests = []
    original = runtime.run_turn_async
    async def run(cfg, system, messages, telemetry, **kw):
        requests.append({
            "model": kw["model_override"], "provider": kw["provider_id_override"],
            "url": kw["provider_base_url_override"],
            "effort": kw.get("reasoning_effort_override", cfg.reasoning_effort),
            "sampling": kw["sampling"].as_dict_nonnull(),
            "tools": kw["tool_registry"].openai_specs(), "system": system,
        })
        return await original(cfg, system, messages, telemetry, **kw)
    monkeypatch.setattr(runtime, "run_turn_async", run)
    return requests


@pytest.mark.parametrize("overrides", [
    ["--you-are", "Precise voice", "--tools", "read,fetch:lazy"],
    ["--chat"], ["--no-tools"],
    ["--system-prompt", "A system prompt", "--append-system-prompt", "Added instructions", "--no-env"],
])
def test_printed_continue_command_reproduces_custom_endpoint_and_overrides(tmp_path, monkeypatch, capsys, overrides):
    monkeypatch.chdir(tmp_path)
    requests = _requests(monkeypatch)
    logins.save_login(logins.Login(provider_id="other-login", sdk_provider_id="openai",
                                   provider_base_url="http://local.test/v1", provider_api_key="another-key"))
    assert cli.main(["-p", "first", "-u", "responses+http://model:sk-secret-secret-secret@local.test/v1",
                     "-r", "spoon", "--temperature", "0.65", "--top-k", "37",
                     "--dry-sequence-breakers", '["\\n", ":"]', *overrides]) == 0
    recipe = _command(capsys.readouterr().err)
    assert "sk-secret-secret-secret" not in shlex.join(recipe)
    assert recipe[recipe.index("-u") + 1] == "responses+http://model@local.test/v1"
    assert recipe[recipe.index("-r") + 1] == "spoon"
    assert cli.main([*recipe[1:], "-p", "second"]) == 0
    assert requests[0] == requests[1]


def test_continue_qualifies_saved_login_provider_and_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    requests = _requests(monkeypatch)
    agent = tmp_path / ".js" / "agents" / "recipe"
    agent.mkdir(parents=True)
    (agent / "01-prompt.md").write_text("Answer tersely.")
    (agent / "agent.yaml").write_text("tools: []\nsampling:\n  min_p: 0.07\n")
    logins.save_login(logins.Login(provider_id="my-login", sdk_provider_id="openai",
                                   provider_base_url="http://saved.test/v1", provider_api_key="key-private"))
    assert cli.main(["-a", "recipe", "-m", "my-login/model", "-r", "high", "-p", "first"]) == 0
    recipe = _command(capsys.readouterr().err)
    assert recipe[recipe.index("--model") + 1] == "my-login/model"
    assert recipe[recipe.index("--agent") + 1] == "recipe"
    assert "key-private" not in shlex.join(recipe)
    assert cli.main([*recipe[1:], "-p", "second"]) == 0
    assert requests[0] == requests[1]


@pytest.mark.parametrize("custom", [False, True])
def test_continue_keeps_an_explicit_home(custom, tmp_path, monkeypatch, capsys):
    from js import paths
    monkeypatch.setenv("JS_HOME", str(tmp_path / "chosen home"))
    monkeypatch.setattr(paths, "custom_home", lambda: custom, raising=False)
    cli._say_continue(runtime.Telemetry(None), "session", resume_prefix=None, agent=None, model=None)
    recipe = _command(capsys.readouterr().err)
    assert ("--home" in recipe) is custom
    if custom:
        assert recipe[recipe.index("--home") + 1] == str(tmp_path / "chosen home")
