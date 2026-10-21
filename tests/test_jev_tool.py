"""jev: TypeSafe Jev questions as a native tool."""

from __future__ import annotations

from types import SimpleNamespace

from js.toolkit import ToolContext
from js.toolkit import jev


class _Client:
    def __init__(self, answers, seen):
        self._answers, self._seen = answers, seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def system_one(self, state, questions):
        self._seen.append((state, questions))
        return SimpleNamespace(answers=self._answers)


def test_jev_sends_state_and_questions_and_reports_each_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    seen: list = []
    answers = {
        "q1": SimpleNamespace(type="noul", noul=0.83),
        "q2": SimpleNamespace(type="choice", choice="oom", confidence=0.9,
                              probabilities={"oom": 0.9, "segv": 0.1}),
    }
    monkeypatch.setattr(jev, "_client", lambda model: _Client(answers, seen))
    context = ToolContext(cwd=tmp_path)

    out = jev.jev(
        '{"log": "killed"}',
        [{"question": "Did it crash?"},
         {"question": "Why?", "kind": "choice", "options": ["oom=killed for memory", "segv"]}],
        context=context,
    )

    state, asked = seen[0]
    assert state == {"log": "killed"}
    assert asked["q1"] == {"type": "noul", "instructions": "Did it crash?"}
    assert asked["q2"]["criteria"] == {"oom": "killed for memory", "segv": None}
    assert "0.83" in out
    assert "oom" in out and "0.90" in out


def test_jev_without_an_api_key_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(jev, "_client", lambda model: (_ for _ in ()).throw(AssertionError("called")))

    out = jev.jev("text", [{"question": "x?"}], context=ToolContext(cwd=tmp_path))

    assert out.startswith("ERROR:")


def test_registered_typesafe_key_reaches_client(tmp_path, monkeypatch):
    import typesafe_sdk
    from js import turn_settings

    seen = {}

    def client(**kwargs):
        seen.update(kwargs)
        return _Client({}, [])

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", client)
    context = ToolContext(cwd=tmp_path)
    turn_settings.install(context, SimpleNamespace(**turn_settings.project({"TYPESAFE_API_KEY": "configured-key"})))
    jev.jev("state", [{"question": "ready?"}], context=context)
    assert seen["api_key"] == "configured-key"
