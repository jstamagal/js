import errno
import json

import pytest

from js import memory, runtime
from js.config import from_env
from js.toolkit import ToolContext
from repl_driver import run_blocking


def _config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JS_DEFAULT_AGENT", "voice")
    monkeypatch.setenv("JS_MODEL", "offline-test-model")
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    agent = tmp_path / ".js" / "agents" / "voice"
    agent.mkdir(parents=True)
    (agent / "00-prompt.md").write_text("SYSTEM")
    return from_env(session="conflict")


def test_blocking_repl_reloads_stale_session_and_accepts_next_turn(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    requests = []

    def turn(turn_cfg, system, messages, telemetry, **kwargs):
        requests.append([dict(message) for message in messages])
        messages.append({"role": "assistant", "content": f"reply {len(requests)}"})
        if len(requests) == 1:
            external = {"kind": "message", "version": 1, "ts": 1,
                        "message": {"role": "assistant", "content": "external answer"}}
            with cfg.session_file.open("a") as stream:
                stream.write(json.dumps(external) + "\n")

    monkeypatch.setattr(runtime, "run_turn", turn)

    state = run_blocking(cfg, ["conflicted question", "next question"])

    assert len(requests) == 2
    assert {"role": "assistant", "content": "external answer"} in requests[1]
    saved = memory.load_replay_messages(cfg.session_file)
    assert saved == state["messages"]
    assert saved[-2:] == [
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "reply 2"},
    ]
    assert not any(message.get("content") == "reply 1" for message in saved)


@pytest.mark.parametrize("error", [ValueError("ordinary failure"), OSError(errno.EIO, "disk failure")])
def test_blocking_repl_still_records_non_stale_turn_errors(tmp_path, monkeypatch, capsys, error):
    cfg = _config(tmp_path, monkeypatch)

    def turn(*args, **kwargs):
        raise error

    monkeypatch.setattr(runtime, "run_turn", turn)

    run_blocking(cfg, ["question"])

    assert str(error) in capsys.readouterr().out
    records = [json.loads(line) for line in cfg.session_file.read_text().splitlines()]
    assert any(record.get("marker", "").startswith("error:") for record in records)
