from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from js.toolkit import jev
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("kind", ["noul", "score"])
@pytest.mark.parametrize("value", [None, "not-a-number", [], 10 ** 1000], ids=["none", "text", "list", "overflow"])
@pytest.mark.parametrize("attribute_answer", [False, True])
def test_invalid_numeric_answer_is_model_visible_error(kind, value, attribute_answer):
    answer = {"type": kind, kind: value}
    if attribute_answer:
        answer = SimpleNamespace(**answer)
    assert jev._format(answer).startswith("ERROR:")


@pytest.mark.parametrize("kind", ["noul", "score"])
def test_missing_numeric_answer_is_model_visible_error(kind):
    assert jev._format({"type": kind}).startswith("ERROR:")


@pytest.mark.parametrize("kind", ["noul", "score"])
@pytest.mark.parametrize("value", [0, 0.5, "0.75"])
def test_valid_numeric_answer_formatting_is_unchanged(kind, value):
    answer = {"type": kind, kind: value, "legend": {"low": 0, "high": 1}}
    assert jev._format(answer) == jev._format_answer(answer)
    assert not jev._format(answer).startswith("ERROR:")


def test_malformed_answer_does_not_hide_valid_neighbor(tmp_path, monkeypatch):
    response = SimpleNamespace(answers={"q1": {"type": "noul"}, "q2": {"type": "noul", "noul": 0.75}})
    client = MagicMock()
    client.__enter__.return_value.system_one.return_value = response
    monkeypatch.setattr(jev, "_client", lambda *_args, **_kwargs: client)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")

    result = jev.jev("state", [{"question": "first?"}, {"question": "second?"}], context=ToolContext(cwd=tmp_path))

    lines = result.splitlines()
    assert lines[0].startswith("1. ERROR:")
    assert lines[1] == "2. " + jev._format(response.answers["q2"])


def test_malformed_reversed_answer_does_not_abort_order_check(tmp_path, monkeypatch):
    answer = {"type": "choice", "choice": "yes", "confidence": 0.9, "probabilities": {"yes": 0.9}}
    response = SimpleNamespace(answers={"q1": answer, "q1_reversed": dict(answer, confidence="invalid")})
    client = MagicMock()
    client.__enter__.return_value.system_one.return_value = response
    monkeypatch.setattr(jev, "_client", lambda *_args, **_kwargs: client)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")

    result = jev.jev("state", [{"question": "first?", "kind": "choice", "options": ["yes", "no"]}],
                     check_order=True, context=ToolContext(cwd=tmp_path))

    assert result.splitlines()[0] == "1. " + jev._format(answer)
    assert "ERROR:" in result.splitlines()[1]
