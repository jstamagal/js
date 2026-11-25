from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typesafe_sdk

from js.toolkit import jev
from js.toolkit.core import ToolContext


def ask(tmp_path, monkeypatch, answers, questions=({"question": "first?"},), **options):
    """The jev tool's reply when the TypeSafe server answers with answers."""
    client = MagicMock()
    client.__enter__.return_value.system_one.return_value = SimpleNamespace(answers=answers)
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", lambda **_kwargs: client)
    context = ToolContext(cwd=tmp_path, typesafe_api_key="fixture-key")
    return jev.jev("state", list(questions), context=context, **options)


def shown(tmp_path, monkeypatch, answer):
    """How the jev tool shows answer as its only question's answer."""
    return ask(tmp_path, monkeypatch, {"q1": answer}).removeprefix("1. ")


@pytest.mark.parametrize("kind", ["noul", "score"])
@pytest.mark.parametrize("value", [None, "not-a-number", [], 10 ** 1000], ids=["none", "text", "list", "overflow"])
@pytest.mark.parametrize("attribute_answer", [False, True])
def test_invalid_numeric_answer_is_model_visible_error(tmp_path, monkeypatch, kind, value, attribute_answer):
    answer = {"type": kind, kind: value}
    if attribute_answer:
        answer = SimpleNamespace(**answer)
    assert shown(tmp_path, monkeypatch, answer).startswith("ERROR:")


@pytest.mark.parametrize("kind", ["noul", "score"])
def test_missing_numeric_answer_is_model_visible_error(tmp_path, monkeypatch, kind):
    assert shown(tmp_path, monkeypatch, {"type": kind}).startswith("ERROR:")


@pytest.mark.parametrize("kind", ["noul", "score"])
@pytest.mark.parametrize("value", [0, 0.5, "0.75"])
def test_valid_numeric_answer_formatting_is_unchanged(tmp_path, monkeypatch, kind, value):
    answer = {"type": kind, kind: value, "legend": {"low": 0, "high": 1}}
    line = shown(tmp_path, monkeypatch, answer)
    assert not line.startswith("ERROR:")
    assert f"{float(value):.2f}" in line


def test_malformed_answer_does_not_hide_valid_neighbor(tmp_path, monkeypatch):
    valid = {"type": "noul", "noul": 0.75}
    result = ask(tmp_path, monkeypatch, {"q1": {"type": "noul"}, "q2": valid},
                 [{"question": "first?"}, {"question": "second?"}])

    lines = result.splitlines()
    assert lines[0].startswith("1. ERROR:")
    assert lines[1] == "2. " + shown(tmp_path, monkeypatch, valid)


def test_malformed_reversed_answer_does_not_abort_order_check(tmp_path, monkeypatch):
    answer = {"type": "choice", "choice": "yes", "confidence": 0.9, "probabilities": {"yes": 0.9}}
    question = {"question": "first?", "kind": "choice", "options": ["yes", "no"]}
    result = ask(tmp_path, monkeypatch, {"q1": answer, "q1_reversed": dict(answer, confidence="invalid")},
                 [question], check_order=True)

    assert result.splitlines()[0] == ask(tmp_path, monkeypatch, {"q1": answer}, [question])
    assert "ERROR:" in result.splitlines()[1]
