"""The prompt history: one file for every run, entries with ts, cwd, session,
agent and text; Up browses the current directory first; the incremental
search finds a prompt from any directory, session or agent."""

from __future__ import annotations

import asyncio
import json
import os

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.search import SearchDirection, SearchState

from js import prompt_history as ph
from js.config import from_env


def _history(path, cwd="/proj", session="s1", agent="a1", **kwargs) -> ph.PromptHistory:
    return ph.PromptHistory(path, lambda: ph.Origin(cwd=cwd, session=session, agent=agent), **kwargs)


def _write(path, *entries: tuple[float, str, str]) -> None:
    for ts, cwd, text in entries:
        ph.append_entry(path, ph.Entry(ts=ts, cwd=cwd, session="s", agent="a", text=text))


def test_a_stored_prompt_is_one_json_line_with_its_origin(tmp_path):
    path = tmp_path / "state" / "history.jsonl"
    history = _history(path, cwd="/proj", session="20260929T1-ab", agent="coder")

    history.store_string("fix the tests\nand lint")

    [line] = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert set(record) == {"ts", "cwd", "session", "agent", "text"}
    assert record["cwd"] == "/proj"
    assert record["session"] == "20260929T1-ab"
    assert record["agent"] == "coder"
    assert record["text"] == "fix the tests\nand lint"
    assert isinstance(record["ts"], float)


def test_a_blank_prompt_is_not_stored(tmp_path):
    path = tmp_path / "history.jsonl"

    _history(path).store_string("   ")

    assert not path.exists()


def test_every_session_appends_to_the_same_file(tmp_path):
    path = tmp_path / "history.jsonl"
    _history(path, session="one", agent="a").store_string("first")
    _history(path, session="two", agent="b").store_string("second")

    entries = ph.read_entries(path)

    assert [(e.session, e.agent, e.text) for e in entries] == [("one", "a", "first"), ("two", "b", "second")]
    assert list(_history(path).load_history_strings()) == ["second", "first"]


def test_up_offers_the_current_directorys_prompts_first_newest_first(tmp_path):
    entries = [
        ph.Entry(1, "/proj", "s", "a", "proj old"),
        ph.Entry(2, "/other", "s", "a", "other old"),
        ph.Entry(3, "/proj", "s", "a", "proj new"),
        ph.Entry(4, "/other", "s", "a", "other new"),
    ]

    assert ph.browse_order(entries, "/proj", cwd_first=True) == [
        "proj new", "proj old", "other new", "other old"]
    assert ph.browse_order(entries, "/proj", cwd_first=False) == [
        "other new", "proj new", "other old", "proj old"]


def test_a_repeated_prompt_is_offered_once_at_its_first_place(tmp_path):
    entries = [
        ph.Entry(1, "/other", "s", "a", "make"),
        ph.Entry(2, "/proj", "s", "a", "make"),
        ph.Entry(3, "/other", "s", "a", "ls"),
    ]

    assert ph.browse_order(entries, "/proj", cwd_first=True) == ["make", "ls"]


def test_reading_skips_broken_lines_and_keeps_the_newest(tmp_path):
    path = tmp_path / "history.jsonl"
    _write(path, (3.0, "/p", "c"), (1.0, "/p", "a"))
    with path.open("a", encoding="utf-8") as f:
        f.write("not json\n{\"ts\": 9}\n[1, 2]\n")
    _write(path, (2.0, "/p", "b"))

    assert [e.text for e in ph.read_entries(path)] == ["a", "b", "c"]
    assert [e.text for e in ph.read_entries(path, limit=2)] == ["b", "c"]


def test_the_old_per_agent_history_files_are_folded_in_once(tmp_path):
    state = tmp_path / "state"
    (state / "coder").mkdir(parents=True)
    old = state / "coder" / "history"
    old.write_text(
        "\n# 2026-01-02 03:04:05.000000\n+one line\n"
        "\n# 2026-01-02 03:04:06.000000\n+two\n+lines\n",
        encoding="utf-8",
    )
    path = state / "history.jsonl"
    _write(path, (1.0, "/p", "older than all"))

    assert ph.import_legacy(path, state) == 2
    assert ph.import_legacy(path, state) == 0

    entries = ph.read_entries(path)
    assert [(e.agent, e.text) for e in entries[1:]] == [("coder", "one line"), ("coder", "two\nlines")]
    assert entries[1].ts < entries[2].ts
    assert not old.exists()


def test_loading_the_history_folds_in_the_old_files(tmp_path):
    state = tmp_path / "state"
    (state / "coder").mkdir(parents=True)
    (state / "coder" / "history").write_text("\n# 2026-01-02 03:04:05\n+legacy\n", encoding="utf-8")

    history = _history(state / "history.jsonl", state_root=state)

    assert list(history.load_history_strings()) == ["legacy"]


def test_the_history_loads_only_max_entries(tmp_path):
    path = tmp_path / "history.jsonl"
    _write(path, *((float(i), "/proj", f"p{i}") for i in range(5)))

    assert list(_history(path, limit=3).load_history_strings()) == ["p4", "p3", "p2"]


def _loaded_buffer(history: ph.PromptHistory) -> Buffer:
    async def load() -> Buffer:
        buffer = Buffer(history=history)
        buffer.load_history_if_not_yet_loaded()
        await buffer._load_history_task
        return buffer

    return asyncio.run(load())


def test_up_in_the_input_buffer_walks_this_directory_first(tmp_path):
    path = tmp_path / "history.jsonl"
    _write(path, (1.0, "/proj", "proj old"), (2.0, "/other", "other"), (3.0, "/proj", "proj new"))
    buffer = _loaded_buffer(_history(path, cwd="/proj", cwd_first=True))

    seen = []
    for _ in range(3):
        buffer.history_backward()
        seen.append(buffer.text)

    assert seen == ["proj new", "proj old", "other"]


def test_the_incremental_search_finds_a_prompt_from_any_directory(tmp_path):
    path = tmp_path / "history.jsonl"
    _write(path, (1.0, "/elsewhere", "cargo build --release"), (2.0, "/proj", "ls"), (3.0, "/proj", "make"))
    buffer = _loaded_buffer(_history(path, cwd="/proj"))

    buffer.apply_search(SearchState("CARGO", SearchDirection.BACKWARD, ignore_case=True),
                        include_current_position=False)

    assert buffer.text == "cargo build --release"


def test_config_puts_the_history_in_state_unless_history_file_says(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("JS_"):
            monkeypatch.delenv(name)

    assert from_env(save_session=False).history_file == tmp_path / ".js" / "state" / "history.jsonl"

    monkeypatch.setenv("JS_HISTORY_FILE", "~/hist.jsonl")
    assert from_env(save_session=False).history_file == tmp_path / "hist.jsonl"
