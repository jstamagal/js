"""Work the harness does once and reuses gives what doing it each time gives."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.syntax import Syntax

from js import (
    capped_process,
    context_budget,
    fset,
    model_metadata,
    screen,
    setcmd,
    skills,
    text_layout,
    tool_args,
    tool_display,
)

RESULTS = {
    "read": ({"file_path": "src/app.py"}, "\n".join(f"{n}|\tvalue_{n} = {n}  # été" for n in range(1, 60))),
    "shell": ({"command": "printf hi"}, "shell=sh\nexit=0\n--- stdout ---\nhi\n\x1b[31mred\x1b[0m\n"),
    "write": ({"file_path": "notes.md", "content": "# one\n\ntwo\n"}, "wrote notes.md (3 lines)"),
    "fs_search": ({"pattern": "def ", "path": "."}, "a.py:1:def f():\nb.py:9:def g():\n"),
}


@pytest.mark.parametrize("name", sorted(RESULTS))
@pytest.mark.parametrize("level", [1, 2, 3])
def test_an_exchange_rendered_without_colour_is_the_coloured_one_with_its_colour_dropped(name, level):
    args, result = RESULTS[name]
    styled = tool_display.render_exchange(name, args, result, level=level)
    plain = tool_display.render_exchange(name, args, result, level=level, plain=True)
    assert tool_display.for_transcript(plain) == tool_display.for_transcript(styled)


@pytest.mark.parametrize("path", ["a.py", "lib/b.c", "m.h", "index.html", "Makefile", "r.rs", "p.pl", "d.m",
                                  "notes.md", "data.json", "noext", "z.unknownext", "CMakeLists.txt"])
@pytest.mark.parametrize("body", ["int main(){}\n", "<html>{% block %}</html>", "@interface Foo\n@end\n", "foo :- bar.\n"])
def test_the_lexer_for_a_path_is_the_one_pygments_guesses(path, body):
    assert tool_display._path_lexer({"file_path": path}, body) == Syntax.guess_lexer(path, body)


def test_several_colours_from_one_read_are_each_colour_looked_up_alone():
    names = ["COLOR_TOOL", "COLOR_ACCENT", "COLOR_OK", "COLOR_ERROR", "COLOR_PANEL", "PALETTE_03"]
    assert fset.hexes(names) == [fset.hex_for(name) for name in names]


@pytest.mark.parametrize("text", ["", "plain ascii", "x" * 300, "tab\there", "café", "漢字 wide", "bell\x07"])
def test_cells_counts_columns_the_same_for_ascii_and_any_other_text(text):
    assert text_layout.cells(text) == sum(text_layout.get_cwidth(char) for char in fset.plain(text).expandtabs(text_layout.TAB))


@pytest.mark.parametrize("text", ["short", "exactly ten", "a much longer line that has to wrap somewhere"])
def test_a_line_that_fits_is_one_row_ending_at_its_end(text):
    cuts = text_layout.cut_points(text, len(text))
    assert cuts == [len(text)]
    assert text_layout.cut_points(text, 10)[-1] == len(text)


def test_a_config_line_applies_its_setting_and_carries_no_confirmation():
    store: dict = {}
    result = setcmd.apply_config_line(store, "set SCROLLBACK_LINES_MAX 123")
    assert result.changed and result.changed_keys == ["SCROLLBACK_LINES_MAX"] and result.lines == []
    assert setcmd.apply_set({}, "SCROLLBACK_LINES_MAX", "123").lines
    unset = setcmd.apply_config_line(store, "set -SCROLLBACK_LINES_MAX", baseline={})
    assert unset.changed and "SCROLLBACK_LINES_MAX" not in json.dumps(store)


def test_schema_error_still_names_bad_arguments_and_a_bad_schema():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert tool_args.schema_error({"n": 1}, schema) is None
    assert "integer" in tool_args.schema_error({"n": "one"}, schema)
    assert tool_args.schema_error({"n": 2}, schema) is None
    assert tool_args.schema_error({}, {"type": "no-such-type"}) is not None


def test_wait_exit_ends_with_the_process_and_gives_none_while_it_runs():
    quick = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    assert capped_process._wait_exit(quick, 10) == 3
    slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = time.monotonic()
        assert capped_process._wait_exit(slow, 0.2) is None
        assert time.monotonic() - started < 5
    finally:
        slow.kill()
        assert capped_process._wait_exit(slow, 10) is not None


def test_a_capped_run_returns_its_output_as_soon_as_the_process_ends():
    started = time.monotonic()
    result = capped_process.run_capped(["sh", "-c", "printf out; printf err >&2"], timeout=10, cwd=None, cap=10_000)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"out", b"err")
    assert time.monotonic() - started < capped_process.READER_GRACE_S


def test_catalog_rows_are_kept_and_read_back_until_the_database_changes(tmp_path, monkeypatch):
    database = tmp_path / "catalog.db"
    database.write_bytes(b"one")
    monkeypatch.setattr(model_metadata.modelsdotdev_data, "_database_path", lambda: database)
    monkeypatch.setattr(model_metadata, "_custom_db_path", lambda: tmp_path / "none.db")
    monkeypatch.setattr(model_metadata, "_rows_file", lambda: tmp_path / "cache" / "rows.json")
    monkeypatch.setattr(model_metadata, "_release_dates", dict)
    reads: list[int] = []

    class Model:
        def __init__(self, name: str) -> None:
            self.provider_id, self.id, self.limits, self.family, self.modalities = "maker", name, None, None, None

    def iter_models():
        reads.append(1)
        return iter([Model(f"model-{len(reads)}")])

    monkeypatch.setattr(model_metadata.modelsdotdev, "iter_models", iter_models)
    try:
        model_metadata._all_models.cache_clear()
        first = model_metadata._all_models()
        model_metadata._all_models.cache_clear()
        assert model_metadata._all_models() == first and len(reads) == 1
        database.write_bytes(b"another, longer")
        model_metadata._all_models.cache_clear()
        assert [row.model_id for row in model_metadata._all_models()] == ["model-2"]
    finally:
        model_metadata.reset_caches()


def test_a_usage_anchor_holds_for_the_same_history_and_not_for_a_changed_one():
    history = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}]
    state = context_budget.TokenState()
    usage = type("Usage", (), {"input_tokens": 1000, "output_tokens": 10})()
    state.record_provider_usage(usage, message_count=2, messages=history)
    grown = [*history, {"role": "user", "content": "more"}]
    assert state.current_context_tokens(messages=grown)[2] is True
    copied = [dict(message) for message in grown]
    assert state.current_context_tokens(messages=copied)[2] is True
    rewritten = [{"role": "user", "content": "something else"}, *grown[1:]]
    assert state.current_context_tokens(messages=rewritten)[2] is False


def test_an_edited_skill_file_is_indexed_again(tmp_path):
    skill = tmp_path / "skills" / "tidy" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: tidy\ndescription: first words\n---\nbody\n", encoding="utf-8")
    assert skills._indexed(skill, "project").metadata.description == "first words"
    assert skills._indexed(skill, "project") is skills._indexed(skill, "project")
    skill.write_text("---\nname: tidy\ndescription: other words, longer\n---\nbody\n", encoding="utf-8")
    assert skills._indexed(skill, "project").metadata.description == "other words, longer"


def test_the_screen_redraw_cap_follows_its_setting():
    async def main() -> tuple[float | None, float | None]:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            async def on_line(line: str) -> None:
                return None

            app, _scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=None,
                on_line=on_line, on_interrupt=lambda: None, on_eof=lambda: None)
            capped = app.min_redraw_interval
            fset.bind_settings({"SCREEN_REDRAWS_MAX": 0})
            task = asyncio.ensure_future(app.run_async())
            await asyncio.sleep(0.1)
            app.invalidate()
            await asyncio.sleep(0.1)
            uncapped = app.min_redraw_interval
            app.exit()
            await task
            return capped, uncapped

    try:
        capped, uncapped = asyncio.run(main())
    finally:
        fset.bind_settings(None)
    assert capped is not None and 0 < capped <= 0.1
    assert uncapped is None


def test_a_parameter_description_is_what_its_file_says_now(tmp_path, monkeypatch):
    from js.toolkit import descriptions

    page = tmp_path / "thing.md"
    monkeypatch.setattr(descriptions, "_description_path", lambda name: page)
    page.write_text(f"The tool.\n{descriptions._PARAMETERS}\n### first\none\n\n### second\ntwo\n")
    assert descriptions.load_parameter_description("thing", "first") == "one"
    assert descriptions.load_parameter_description("thing", "second") == "two"
    with pytest.raises(ValueError):
        descriptions.load_parameter_description("thing", "third")
    page.write_text(f"The tool.\n{descriptions._PARAMETERS}\n### second\nchanged now\n")
    assert descriptions.load_parameter_description("thing", "second") == "changed now"
    with pytest.raises(ValueError):
        descriptions.load_parameter_description("thing", "first")
    page.write_text("")
    assert descriptions.load_parameter_description("thing", "second") == ""


def test_file_stamp_changes_with_the_file_and_is_none_without_it(tmp_path):
    from js import paths

    target = tmp_path / "f"
    assert paths.file_stamp(target) is None
    target.write_text("a")
    first = paths.file_stamp(target)
    target.write_text("ab")
    assert first is not None and paths.file_stamp(str(target)) != first


def _modules_after(code: str) -> set[str]:
    done = subprocess.run(
        [sys.executable, "-c", f"import sys\ntry:\n    {code}\nexcept SystemExit:\n    pass\n"
                               "print('\\n'.join(m for m in sys.modules if m.startswith('js.')), file=sys.stderr)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=True)
    return set(done.stderr.split())


def test_help_alone_does_not_load_the_run_modules():
    loaded = _modules_after("from js.entry import main; main(['--help'])")
    assert "js.command_line" in loaded
    assert "js.cli" not in loaded


def test_the_command_line_loads_no_mode_it_does_not_run():
    loaded = _modules_after("import js.cli")
    assert not loaded & {"js.repl", "js.bench", "js.commit_cli", "js.verify", "js.migrate_config", "js.loop_controller"}
