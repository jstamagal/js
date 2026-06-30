"""ctrl+c during a turn must keep the partial work (and survive a reload), not
wipe it — except when the turn produced nothing, where the bare prompt is dropped."""

from __future__ import annotations

from js import cli
from js.memory import load_messages


def _session_file(tmp_path):
    found = list((tmp_path / ".local" / "share" / "js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _drive_repl(monkeypatch, tmp_path, run_turn_stub):
    """Run the interactive loop for exactly one user line, then EOF to exit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.lines = iter(["please do the thing"])

        def prompt(self, *_args, **_kwargs):
            return next(self.lines)  # StopIteration after one line breaks the loop

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    # StopIteration from the stubbed prompt() bubbles as RuntimeError out of the
    # generator; catch the clean exit paths and ignore the loop-terminator.
    try:
        return cli.main([])
    except (RuntimeError, StopIteration):
        return 0


def test_interrupt_keeps_partial_work_across_reload(monkeypatch, tmp_path):
    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        # Simulate a turn that did real work (an assistant tool_call) before ^C.
        messages.append({
            "role": "assistant",
            "content": "starting",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "read", "arguments": "{}"}}],
        })
        raise KeyboardInterrupt

    _drive_repl(monkeypatch, tmp_path, run_turn_stub)

    reloaded = load_messages(_session_file(tmp_path))
    # User prompt + the partial assistant turn survive; the orphaned tool_call is
    # healed with a synthetic result on load (so the next turn is valid).
    assert [m["role"] for m in reloaded] == ["user", "assistant", "tool"]
    assert reloaded[0]["content"] == "please do the thing"
    assert reloaded[1]["tool_calls"][0]["id"] == "call_1"
    assert "interrupted" in reloaded[2]["content"].lower()


def test_interrupt_with_no_work_drops_the_bare_prompt(monkeypatch, tmp_path):
    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        raise KeyboardInterrupt  # stopped before the model produced anything

    _drive_repl(monkeypatch, tmp_path, run_turn_stub)

    # Nothing worth keeping → the user prompt is rolled back on reload too.
    assert load_messages(_session_file(tmp_path)) == []
