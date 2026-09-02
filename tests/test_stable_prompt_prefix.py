"""A session sends one system prompt for its whole life, and asks providers to
cache the prefix that every turn resends."""

from __future__ import annotations

from js import cli, memory
from js.memory import append_system_prompt, load_messages, load_system_prompt
from js.model_client import _build_inference_params
from js.sampling import Sampling


def test_recorded_system_prompt_round_trips(tmp_path):
    session = tmp_path / "session.jsonl"

    append_system_prompt(session, "SYSTEM ONE")

    assert load_system_prompt(session) == "SYSTEM ONE"


def test_no_recorded_system_prompt_reads_as_absent(tmp_path):
    session = tmp_path / "session.jsonl"
    memory.append_message(session, {"role": "user", "content": "hi"})

    assert load_system_prompt(session) is None


def test_a_multiline_prompt_survives_the_round_trip(tmp_path):
    session = tmp_path / "session.jsonl"
    system = 'line one\nline "two"\n\ttabbed\n'

    append_system_prompt(session, system)

    assert load_system_prompt(session) == system


def _repl(monkeypatch, tmp_path, argv, lines=()):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.lines = iter(lines)

        def prompt(self, *_args, **_kwargs):
            try:
                return next(self.lines)
            except StopIteration:
                raise EOFError from None

    seen: list[str] = []

    def record_turn(cfg, system, *a, **k):
        seen.append(system)

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", record_turn)
    try:
        cli.main(argv)
    except (RuntimeError, StopIteration):
        pass
    return seen


def _only_session(tmp_path):
    found = list((tmp_path / ".local" / "share" / "js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _agent_prompt(tmp_path, body):
    prompts = tmp_path / ".config" / "js" / "agents" / "defaultagent"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "01-prompt.md").write_text(body, encoding="utf-8")


def test_a_resumed_session_sends_the_prompt_it_started_with(monkeypatch, tmp_path):
    _agent_prompt(tmp_path, "ORIGINAL PROMPT\n")
    first = _repl(monkeypatch, tmp_path, [], lines=["one"])

    # The agent prompt changes on disk between runs, as a live clock in it would.
    _agent_prompt(tmp_path, "REBUILT PROMPT\n")
    second = _repl(monkeypatch, tmp_path, ["--session", _only_session(tmp_path).stem], lines=["two"])

    assert "ORIGINAL PROMPT" in first[0]
    assert second[0] == first[0]


def test_prompt_drift_is_reported_as_a_message_not_a_new_prefix(monkeypatch, tmp_path):
    _agent_prompt(tmp_path, "ORIGINAL PROMPT\n")
    _repl(monkeypatch, tmp_path, [], lines=["one"])
    session_file = _only_session(tmp_path)

    _agent_prompt(tmp_path, "REBUILT PROMPT\n")
    _repl(monkeypatch, tmp_path, ["--session", session_file.stem])

    contents = [m.get("content") for m in load_messages(session_file) if m.get("role") == "user"]
    assert any("prompt files changed" in str(c) for c in contents)


def test_an_unchanged_prompt_reports_no_drift(monkeypatch, tmp_path):
    _agent_prompt(tmp_path, "STEADY PROMPT\n")
    _repl(monkeypatch, tmp_path, [], lines=["one"])
    session_file = _only_session(tmp_path)

    _repl(monkeypatch, tmp_path, ["--session", session_file.stem])

    contents = [m.get("content") for m in load_messages(session_file) if m.get("role") == "user"]
    assert not any("prompt files changed" in str(c) for c in contents)


def test_requests_ask_for_prompt_caching():
    params = _build_inference_params(
        Sampling(), None, reasoning=None, output=None, extra_body={},
    )

    assert params is not None
    assert params.cache is not None
