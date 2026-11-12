"""Argument usage labels retain the separator consumed by styled words()."""

import pytest

from js import messages


def test_usage_and_help_separate_label_from_program():
    parser = messages.ArgumentParser(prog="target")
    parser.add_argument("--value")

    assert parser.format_usage().startswith("Usage: target ")
    assert parser.format_help().startswith("Usage: target ")


def test_argument_error_separates_usage_label(tmp_path, capsys):
    parser = messages.ArgumentParser(prog="target")

    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--unknown"])

    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out
    assert output.err.startswith("Usage: target ")


def test_localized_usage_label_does_not_change_global_words_trimming(monkeypatch):
    prefix = messages.Message("Aufruf: ")
    monkeypatch.setattr(messages, "ARGS_USAGE_PREFIX", prefix)

    assert prefix.words() == "Aufruf:"
    assert messages.ArgumentParser(prog="target").format_usage().startswith("Aufruf: target ")


@pytest.mark.parametrize("prefix", ["Synopsis> ", "custom:", ""])
def test_explicit_formatter_prefix_is_not_modified(prefix):
    formatter = messages._HelpFormatter(prog="target")
    formatter.add_usage(None, [], [], prefix=prefix)

    assert formatter.format_help() == prefix + "target\n"


def test_usage_wrapping_keeps_program_separate_from_label():
    class NarrowFormatter(messages._HelpFormatter):
        def __init__(self, prog):
            super().__init__(prog, width=38)

    parser = messages.ArgumentParser(prog="target", formatter_class=NarrowFormatter)
    for name in ("first-long-argument", "second-long-argument", "third-long-argument"):
        parser.add_argument("--" + name)
    usage = parser.format_usage()

    assert usage.startswith("Usage: target ")
    assert len(usage.splitlines()) > 1
    assert all(line.startswith(" ") for line in usage.splitlines()[1:])
