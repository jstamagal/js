"""What one turn on a session leaves in the session file, at each place that
runs one: a `Conversation`, a swarm agent's opener and wake turns, a
subagent's task turn and a turn typed into its window. Each turn calls a tool,
then completes, fails, or is cancelled; the tool's batch is on disk either
way. The between-turn compaction check runs after a completed turn where the
caller has one: a Conversation after each turn, a swarm agent before each
sleep, a subagent never."""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_subagent_isolation import make_cfg, prompt_dir
from test_swarm import _agent_dir, _call, _events, _spec, _text

from js import compaction, context_window, runtime, session_store, swarm, swarm_run
from js.config import Config
from js.conversation import Conversation
from js.toolkit import ToolContext, subagents

OUTCOMES = ("complete", "raise", "cancel")


@pytest.fixture(autouse=True)
def offline_model_metadata(monkeypatch):
    monkeypatch.setattr(runtime.model_metadata, "accepts_image_input", lambda *a, **k: False)
    monkeypatch.setattr(context_window, "resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)


@pytest.fixture
def checks(monkeypatch) -> list[int]:
    """One entry per between-turn compaction check: the history length it saw."""
    ran: list[int] = []

    async def check(cfg, auto, context, system, messages, resolve_window, emit=None, log_dir=None):
        ran.append(len(messages))
        return compaction.AutoCompactOutcome()

    monkeypatch.setattr(compaction, "maybe_auto_compact_async", check)
    return ran


class _Model:
    """Answers ``before`` calls with "ok", then calls the tool `probe`, then
    answers "done", raises, or is cancelled. ``on_call`` sees each call's number."""

    def __init__(self, outcome: str, *, before: int = 0, on_call=None) -> None:
        self.outcome = outcome
        self.before = before
        self.on_call = on_call
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        step = self.calls - self.before
        if step <= 0:
            return _text("ok")
        if step == 1:
            return _call("probe", f"probe-{self.calls}")
        if self.outcome == "raise":
            raise RuntimeError("model down")
        if self.outcome == "cancel":
            raise asyncio.CancelledError()
        return _text("done")


def _records(session: Path) -> list[dict]:
    """The message and mark records of ``session``, in file order."""
    lines = session.read_text(encoding="utf-8").splitlines()
    return [r for r in map(json.loads, lines) if r.get("kind") in ("message", "mark")]


def _roles(session: Path) -> list[str]:
    return [r["message"]["role"] if r["kind"] == "message" else "mark" for r in _records(session)]


def _user_texts(session: Path) -> list[str]:
    return [str(r["message"]["content"]) for r in _records(session)
            if r["kind"] == "message" and r["message"]["role"] == "user"]


def _stamp(session: Path) -> dict:
    """The stamp every assistant record of ``session`` carries."""
    stamps = {json.dumps(r.get("stamp"), sort_keys=True) for r in _records(session)
              if r["kind"] == "message" and r["message"]["role"] == "assistant"}
    assert len(stamps) == 1
    return json.loads(stamps.pop())


def _tool_turn(outcome: str) -> list[str]:
    """The roles a turn that calls `probe` once saves after its user message."""
    return ["assistant", "tool", "assistant"] if outcome == "complete" else ["assistant", "tool"]


def _last_answer(session: Path) -> str:
    return str(_records(session)[-1]["message"]["content"])


# --------------------------------------------------------------------------
# Conversation.ask
# --------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", OUTCOMES)
def test_a_conversation_turn(monkeypatch, tmp_path, checks, outcome):
    monkeypatch.setattr(runtime.model_client, "stream_model_async", _Model(outcome))
    session = tmp_path / "sessions" / "chat.jsonl"
    cfg = Config(model="offline-test-model", trace=False, session_file=session,
                 settings={"COMPACTION_AUTO": True})
    chat = Conversation(cfg, "SYS")

    ask = chat.ask("look", suppress_output=True)
    if outcome == "complete":
        assert asyncio.run(ask) == "done"
    else:
        with pytest.raises(RuntimeError if outcome == "raise" else asyncio.CancelledError):
            asyncio.run(ask)

    assert _roles(session) == ["user", *_tool_turn(outcome)]
    assert _user_texts(session) == ["look"]
    assert _stamp(session)["model"] == "offline-test-model"
    assert len(checks) == (1 if outcome == "complete" else 0)


# --------------------------------------------------------------------------
# a swarm agent: the opener turn, and a turn a bus message wakes it into
# --------------------------------------------------------------------------

@pytest.mark.parametrize("turn", ["opener", "wake"])
@pytest.mark.parametrize("outcome", OUTCOMES)
def test_a_swarm_agent_turn(monkeypatch, tmp_path, checks, outcome, turn):
    _agent_dir(tmp_path, monkeypatch)
    room = swarm.Room(tmp_path / "bus")
    before = 1 if turn == "wake" else 0

    def on_call(n: int) -> None:
        if before and n == 1:
            room.send("b", "a", "ping")                  # wakes the agent after its opener
        if outcome == "complete" and n == before + 2:
            room.send("steer", "a", "enough", kind="stop")  # read when it next sleeps

    monkeypatch.setattr(runtime.model_client, "stream_model_async", _Model(outcome, before=before, on_call=on_call))
    out = io.StringIO()
    code = swarm_run.run(_spec(tmp_path, [{"name": "a", "agent": "voice", "opener": "go", "session": "s-a",
                                           "cwd": str(tmp_path)}]), out=out)

    events = _events(out)
    session = Path(next(e["session"] for e in events if e["type"] == "agent_start"))
    reason = next(e["reason"] for e in events if e["type"] == "agent_end")
    assert (code, reason) == {"complete": (0, "stopped"), "raise": (1, "error"),
                              "cancel": (0, "cancelled")}[outcome]
    opener = ["user", "assistant"] if turn == "wake" else []
    assert _roles(session) == [*opener, "user", *_tool_turn(outcome)]
    texts = _user_texts(session)
    assert texts[0] == "go"
    if turn == "wake":
        assert len(texts) == 2 and "ping" in texts[1]
    assert _stamp(session)["model"] == "offline-test-model"
    # The check runs before each sleep: after the opener when it sleeps, and
    # after a completed turn.
    assert len(checks) == (1 if turn == "wake" else 0) + (1 if outcome == "complete" else 0)


# --------------------------------------------------------------------------
# a subagent: its task turn, and a turn typed into its window
# --------------------------------------------------------------------------

def _parent(tmp_path: Path) -> ToolContext:
    prompts = prompt_dir(tmp_path, "worker")
    context = ToolContext(cwd=tmp_path)
    context.config = make_cfg(tmp_path, "parent", prompts.parent / "parent")
    return context


def _child_session(context: ToolContext) -> Path:
    (session,) = sorted(session_store.subagent_folder(Path(context.config.session_file)).glob("*.jsonl"))
    return session


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_a_subagent_task_turn(monkeypatch, tmp_path, checks, outcome):
    monkeypatch.setattr(runtime.model_client, "stream_model_async", _Model(outcome))
    context = _parent(tmp_path)

    result = subagents.task(["work"], agent_id="worker", context=context)

    if outcome == "complete":
        assert result == "done"
    else:
        assert result.startswith("ERROR")
        assert ("RuntimeError" if outcome == "raise" else "CancelledError") in result
    session = _child_session(context)
    assert _roles(session) == ["user", *_tool_turn(outcome)]
    assert _user_texts(session) == ["work"]
    assert _stamp(session)["model"] == "offline-test-model"
    assert checks == []


class _Window:
    """A subagent window that keeps the turn a typed line would start."""

    number = 2

    def __init__(self) -> None:
        self.resume = None

    def telemetry(self, debug_log):
        return runtime.Telemetry(debug_log=debug_log)

    def running(self, context) -> None:
        pass

    @contextmanager
    def writing(self):
        yield

    def steer(self) -> None:
        return None

    def finished(self, resume) -> None:
        self.resume = resume


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_a_turn_typed_into_a_subagent_window(monkeypatch, tmp_path, checks, outcome):
    monkeypatch.setattr(runtime.model_client, "stream_model_async", _Model(outcome, before=1))
    context = _parent(tmp_path)
    window = _Window()

    async def run() -> None:
        token = subagents.window_opener.set(lambda launch: window)
        try:
            assert await subagents.task_async(tasks=["work"], agent_id="worker", context=context) == "ok"
        finally:
            subagents.window_opener.reset(token)
        await window.resume("more")

    if outcome == "complete":
        asyncio.run(run())
    else:
        with pytest.raises(RuntimeError if outcome == "raise" else asyncio.CancelledError):
            asyncio.run(run())

    session = _child_session(context)
    assert _roles(session) == ["user", "assistant", "user", *_tool_turn(outcome)]
    assert _user_texts(session) == ["work", "more"]
    if outcome == "complete":
        assert _last_answer(session) == "done"
    assert _stamp(session)["model"] == "offline-test-model"
    assert checks == []
