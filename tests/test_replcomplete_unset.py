import pytest
from prompt_toolkit.document import Document

from js.replcomplete import JsCompleter


@pytest.fixture
def completer():
    return JsCompleter(lambda: {"set": "set", "fset": "fset", "keys": "keys"},
                       setting_keys=["EFFORT", "MODEL"], fset_names=["COLOR_OK"],
                       value_enums={"EFFORT": ["low", "high"], "-EFFORT": ["forbidden"]})


@pytest.mark.parametrize(("text", "expected"), [
    ("/set -EFF", "-EFFORT"), ("/set -eff", "-effort"),
    ("/fset -COLOR_O", "-COLOR_OK"), ("/fset -color_o", "-color_ok"),
    ("/set EFF", "EFFORT"), ("/set eff", "effort"),
    ("/fset COLOR_O", "COLOR_OK"), ("/fset color_o", "color_ok"),
])
def test_unset_and_positive_key_completion(completer, text, expected):
    token = text.split()[-1]
    assert completer.candidates(text) == ([expected], len(token))
    completions = list(completer.get_completions(Document(text), None))
    assert len(completions) == 1
    assert completions[0].start_position == -len(token)
    assert text[:len(text) + completions[0].start_position] + completions[0].text == text[:-len(token)] + expected


@pytest.mark.parametrize("text", ["/set -EFFORT ", "/set -EFFORT f", "/fset -COLOR_OK "])
def test_unset_key_never_completes_value_enum(completer, text):
    assert completer.candidates(text)[0] == []


def test_positive_value_enum_and_keys_behavior_unchanged(completer):
    assert completer.candidates("/set EFFORT h") == (["high"], 1)
    assert completer.candidates("/keys -EFF") == ([], 4)
    assert completer.candidates("/set -") == (["-EFFORT", "-MODEL"], 1)
