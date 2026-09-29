"""Sessions filed by start directory, the .txt beside each record, the stamp
on every assistant message, start metadata, branches, titles, and --session
lookup across folders. Every test runs in the tmp HOME the conftest installs."""

from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path

import ai
import pytest

from js import cli, runtime, session_store, supervisor
from js import memory as M
from js.config import from_env
from js.memory import load_messages
from js.model_client import ModelStreamResult, ModelToolCall
from js.session_catalog import (
    append_title,
    branch_session,
    first_metadata,
    last_stamp,
    record_session_start,
    session_title,
)
from js.toolkit import ToolContext
from js.toolkit.meta import task
from repl_driver import LineSession


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(runtime.model_metadata, "accepts_image_input", lambda *a, **k: False)
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)
    for name in ("JS_AGENT", "JS_SESSION", "JS_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _text(text: str) -> ModelStreamResult:
    return ModelStreamResult(
        text=text, tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop", assistant_message=ai.assistant_message(text),
    )


def _call(label: str, name: str, arguments: dict) -> ModelStreamResult:
    return ModelStreamResult(
        text=label, tool_calls=[ModelToolCall(id="call-1", name=name, arguments=json.dumps(arguments))],
        reasoning="", usage=ai.types.usage.Usage(input_tokens=0, output_tokens=1),
        finish_reason="tool_calls", assistant_message=ai.assistant_message(label),
    )


def _replies(monkeypatch, *results: ModelStreamResult) -> None:
    queue = list(results)

    def stream(**_kwargs):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)


def _all_sessions() -> list[Path]:
    root = session_store.paths.sessions_root()
    return sorted(root.rglob("*.jsonl")) if root.is_dir() else []


def _dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- layout ------------------------------------------------------------------


def test_the_folder_is_the_start_path_with_slashes_and_underscores_as_dashes():
    assert session_store.slug("/home/ronald_rump/js") == "-home-ronald-rump-js"
    assert session_store.slug("/") == "-"


def test_a_new_session_lands_in_its_start_directory_folder_under_a_dated_name(monkeypatch, tmp_path):
    work = _dir(tmp_path, "my_project")
    monkeypatch.chdir(work)
    _replies(monkeypatch, _text("OK"))

    assert cli.main(["-p", "hello"]) == 0

    [session] = _all_sessions()
    assert session.parent == session_store.paths.sessions_root() / session_store.slug(work)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{4}-[0-9a-f]{4}", session.stem)
    assert session.with_suffix(".txt").is_file()


# --- the .txt transcript -----------------------------------------------------


def test_the_txt_has_the_fixed_header_numbered_rows_and_no_tool_output(monkeypatch, tmp_path):
    work = _dir(tmp_path, "work")
    monkeypatch.chdir(work)
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=work))
    command = "printf 'hid%s' den\necho second-line"
    _replies(monkeypatch, _call("look at it", "shell", {"command": command}), _text("done"))

    assert cli.main(["-p", "run it\nplease"]) == 0

    [session] = _all_sessions()
    lines = session.with_suffix(".txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("agent: defaultagent")
    assert f"dir: {work}" in lines[0] and "mode: -p" in lines[0]
    assert lines[1].startswith("models: ")
    assert lines[2].startswith("started: ") and "turns: 1" in lines[2]
    assert lines[3] == "branched-from: -"
    assert lines[4].startswith("tags: ")
    assert lines[5] == ""
    rows = lines[6:]
    assert rows[0].startswith("#0001 ") and rows[0].endswith("you  run it")
    assert rows[1].startswith(" ") and rows[1].strip() == "please"
    tool = next(row for row in rows if "tool:shell" in row)
    assert tool.startswith("#0003 ")
    assert "look at it" in tool and "$ printf 'hid%s' den" in tool and "exit 0" in tool
    assert re.search(r"\d+B$", tool)
    assert rows[-1].startswith("#0004 ") and rows[-1].endswith("ape  done")
    text = "\n".join(lines)
    assert "hidden" not in text and "second-line" not in text
    # The output is in the record, at the same message number.
    records = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
    messages = [record["message"] for record in records if record.get("kind") == "message"]
    assert messages[2]["role"] == "tool" and "hidden" in messages[2]["content"]


def test_the_txt_follows_the_record_as_the_session_goes_on(monkeypatch, tmp_path):
    monkeypatch.chdir(_dir(tmp_path, "work"))
    _replies(monkeypatch, _text("first answer"))
    assert cli.main(["--session", "ongoing", "-p", "one"]) == 0
    _replies(monkeypatch, _text("second answer"))

    assert cli.main(["--session", "ongoing", "-p", "two"]) == 0

    [session] = _all_sessions()
    txt = session.with_suffix(".txt").read_text(encoding="utf-8")
    assert "turns: 2" in txt.splitlines()[2]
    rows = [line for line in txt.splitlines() if line.startswith("#")]
    assert [row.split()[0] for row in rows] == ["#0001", "#0002", "#0003", "#0004"]
    assert rows[-1].endswith("second answer")


def test_the_txt_drops_rows_a_rollback_took_back_and_names_each_model_change(tmp_path):
    session = tmp_path / "s.jsonl"
    question = {"role": "user", "content": "question"}
    M.append_message(session, question)
    M.append_message(session, {"role": "assistant", "content": "withdrawn"}, stamp=M.stamp_for("m1", "p", None))
    M.persist_messages(session, [question])
    M.append_message(session, {"role": "assistant", "content": "kept"}, stamp=M.stamp_for("m1", "p", None))
    M.append_message(session, {"role": "user", "content": "again"})
    M.append_message(session, {"role": "assistant", "content": "other"}, stamp=M.stamp_for("m2", "p", None))

    lines = session.with_suffix(".txt").read_text(encoding="utf-8").splitlines()

    rows = [line for line in lines if line.startswith("#")]
    assert [row.split()[0] for row in rows] == ["#0001", "#0003", "#0004", "#0005"]
    assert "withdrawn" not in "\n".join(lines)
    assert "m1" in lines[1] and "m2" in lines[1] and "#0005" in lines[1]


def test_a_wipe_takes_the_txt_with_it(tmp_path):
    session = tmp_path / "s.jsonl"
    M.append_message(session, {"role": "user", "content": "gone"})

    M.wipe(session)

    assert not session.with_suffix(".txt").exists()


# --- subagent runs -------------------------------------------------------------


def test_a_subagent_run_is_filed_under_its_parent_session(monkeypatch, tmp_path):
    work = _dir(tmp_path, "work")
    monkeypatch.chdir(work)
    parent = from_env()
    context = ToolContext(cwd=work)
    context.config = parent
    _replies(monkeypatch, _text("child done"))

    assert "child done" in task(["do the thing"], agent_id="defaultagent", context=context)

    children = sorted(session_store.subagent_folder(parent.session_file).glob("*.jsonl"))
    assert len(children) == 1
    assert children[0].name.startswith("task-")
    assert children[0].with_suffix(".txt").is_file()
    metadata = first_metadata(children[0])
    assert metadata["mode"] == "subagent"
    assert metadata["parent_session"] == str(parent.session_file)
    assert sorted(p.name for p in parent.sessions_dir.glob("*.jsonl")) == [parent.session_file.name]


# --- stamps and resume -----------------------------------------------------------


def _repl(monkeypatch, argv, lines, models):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(lines))

    def turn(cfg, system, messages, *a, **k):
        models.append(cfg.model)
        messages.append({"role": "assistant", "content": f"on {cfg.model}"})

    monkeypatch.setattr(cli.runtime, "run_turn", turn)
    assert cli.main(["--blocking", *argv]) == 0


def test_a_session_resumes_on_the_model_it_was_switched_to(monkeypatch, tmp_path):
    monkeypatch.chdir(_dir(tmp_path, "work"))
    models: list[str] = []
    _repl(monkeypatch, ["--model", "model-x"], ["hello", "/model model-y", "switched"], models)
    [session] = _all_sessions()
    assert models == ["model-x", "model-y"]

    _repl(monkeypatch, ["--session", session.stem], ["resumed"], models)

    assert models[-1] == "model-y"
    stamps = [json.loads(line).get("stamp") for line in session.read_text(encoding="utf-8").splitlines()
              if json.loads(line).get("kind") == "message" and json.loads(line)["message"]["role"] == "assistant"]
    assert [stamp["model"] for stamp in stamps] == ["model-x", "model-y", "model-y"]
    assert last_stamp(session)["model"] == "model-y"


def test_the_async_repl_stamps_each_answer_with_the_model_it_ran_on(monkeypatch, tmp_path):
    from repl_driver import run_async

    monkeypatch.chdir(_dir(tmp_path, "work"))

    first_turn_started = asyncio.Event()

    async def turn(cfg, system, messages, telemetry, **kwargs):
        first_turn_started.set()
        messages.append({"role": "assistant", "content": f"on {cfg.model}"})

    async def script(on_line):
        await on_line("first")
        # A queued line takes the live model when its turn starts, so /model
        # waits until the first turn has started and ended.
        await first_turn_started.wait()
        for job in supervisor.get_current().jobs("turn"):
            await job.task
        await on_line("/model model-y")
        await on_line("second")

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn)
    cfg = from_env()

    run_async(monkeypatch, cfg, script, model="model-x")

    records = [json.loads(line) for line in cfg.session_file.read_text(encoding="utf-8").splitlines()]
    stamps = [record["stamp"]["model"] for record in records
              if record.get("kind") == "message" and record["message"]["role"] == "assistant"]
    assert stamps == ["model-x", "model-y"]
    assert last_stamp(cfg.session_file)["model"] == "model-y"


def test_a_prompt_run_resumes_on_the_last_stamp_too(monkeypatch, tmp_path):
    monkeypatch.chdir(_dir(tmp_path, "work"))
    seen: list[str] = []

    def stream(**kwargs):
        seen.append(kwargs.get("model_id"))
        return _text("OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    assert cli.main(["--session", "carry", "--model", "model-y", "-p", "one"]) == 0

    assert cli.main(["--session", "carry", "-p", "two"]) == 0

    assert seen == ["model-y", "model-y"]


def test_the_stamped_provider_rides_only_where_routing_takes_it():
    from types import SimpleNamespace

    on_deepseek = SimpleNamespace(model="deepseek-v4-flash", provider_id="deepseek")

    assert cli._resume_model_spec({"model": "deepseek-v4-flash", "provider": "deepseek"}, on_deepseek) is None
    assert cli._resume_model_spec({"model": "deepseek-v4-pro", "provider": "deepseek"}, on_deepseek) == \
        "deepseek/deepseek-v4-pro"
    # A stamped provider that is not the configured one and has no login
    # falls back to the configured model.
    assert cli._resume_model_spec({"model": "a/b", "provider": "no-such-provider"}, on_deepseek) is None
    assert cli._resume_model_spec({"model": "gpt-x", "provider": "openai"}, on_deepseek) is None
    assert cli._resume_model_spec({"model": "m"}, SimpleNamespace(model="m", provider_id=None)) is None


# --- start metadata ----------------------------------------------------------------


def test_each_start_records_its_mode_and_command_line(monkeypatch, tmp_path):
    monkeypatch.chdir(_dir(tmp_path, "work"))
    _replies(monkeypatch, _text("OK"))

    assert cli.main(["--session", "oneshot", "-p", "hi"]) == 0
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("piped words"))
    assert cli.main(["--session", "piped"]) == 0
    _repl(monkeypatch, ["--session", "chat"], ["hello"], [])

    folder = session_store.folder_for(Path.cwd())
    assert first_metadata(folder / "oneshot.jsonl")["mode"] == "-p"
    assert first_metadata(folder / "oneshot.jsonl")["command"] == ["js", "--session", "oneshot", "-p", "hi"]
    assert first_metadata(folder / "piped.jsonl")["mode"] == "pipe"
    assert first_metadata(folder / "chat.jsonl")["mode"] == "repl"


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_branch_records_its_parent_and_the_id_of_the_message_it_split_at(tmp_path):
    parent = session_store.reserve(session_store.folder_for(tmp_path))
    record_session_start(parent, cwd=tmp_path, agent="defaultagent", mode="repl")
    for index, role in enumerate(("user", "assistant", "user", "assistant"), 1):
        M.append_message(parent, {"role": role, "content": f"m{index}"}, stamp=M.stamp_for("m", "p", None))
    messages = [record for record in _lines(parent) if record["kind"] == "message"]
    split = messages[1]["id"]

    branch = branch_session(parent, split, cwd=tmp_path, agent="defaultagent", mode="repl")

    assert branch.parent == parent.parent and branch != parent
    assert [m["content"] for m in load_messages(branch)] == ["m1", "m2"]
    metadata = first_metadata(branch)
    assert metadata["branched_from"] == {"session": str(parent), "message": split}
    copied = [record for record in _lines(branch) if record["kind"] == "message"]
    assert [(r["id"], r["parent"]) for r in copied] == [(r["id"], r["parent"]) for r in messages[:2]]
    assert metadata["parent"] == split
    header = branch.with_suffix(".txt").read_text(encoding="utf-8").splitlines()[3]
    assert parent.stem in header and "#0002" in header
    assert split not in branch.with_suffix(".txt").read_text(encoding="utf-8")


def test_a_branch_at_an_id_the_parent_lacks_is_refused(tmp_path):
    parent = session_store.reserve(session_store.folder_for(tmp_path))
    M.append_message(parent, {"role": "user", "content": "m1"})

    with pytest.raises(ValueError):
        branch_session(parent, "00000000", cwd=tmp_path)
    assert list(parent.parent.glob("*.jsonl")) == [parent]


# --- record ids ------------------------------------------------------------------


def test_every_record_gets_a_unique_id_and_the_parent_on_its_path(tmp_path):
    session = session_store.reserve(session_store.folder_for(tmp_path))
    record_session_start(session, cwd=tmp_path, agent="defaultagent")
    M.append_system_prompt(session, "sys")
    M.append_message(session, {"role": "user", "content": "one"})
    M.persist_messages(session, [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
                       stamp=M.stamp_for("m", "p", None))
    append_title(session, "a title")
    M.persist_messages(session, [{"role": "user", "content": "one"}, {"role": "assistant", "content": "other"}])

    records = _lines(session)
    ids = [record["id"] for record in records]
    assert all(re.fullmatch(r"[0-9a-f]{8}", record_id) for record_id in ids)
    assert len(set(ids)) == len(ids)
    path = [record for record in records if record["kind"] in ("message", "mark")]
    assert path[0]["parent"] is None
    assert [record["parent"] for record in path[1:]] == [record["id"] for record in path[:-1]]
    assert any(record.get("marker", "").startswith("rollback_to:") for record in path)
    # A start or title record hangs off the path record before it.
    for index, record in enumerate(records):
        if record["kind"] in ("session_metadata", "title"):
            before = [r["id"] for r in records[:index] if r["kind"] in ("message", "mark")]
            assert record["parent"] == (before[-1] if before else None)


def test_an_id_the_file_already_holds_is_drawn_again(tmp_path, monkeypatch):
    session = session_store.reserve(session_store.folder_for(tmp_path))
    M.append_message(session, {"role": "user", "content": "one"})
    taken = _lines(session)[0]["id"]
    draws = iter([taken, taken, "bbbbbbbb"])
    monkeypatch.setattr(session_store, "secrets", type("S", (), {"token_hex": staticmethod(lambda _n: next(draws))}))

    session_store.append(session, {"kind": "mark", "version": 1, "ts": 1.0, "marker": "x"})

    assert [(r["id"], r["parent"]) for r in _lines(session)][1] == ("bbbbbbbb", taken)


def test_ids_chain_across_writers_to_the_same_file(tmp_path):
    session = session_store.reserve(session_store.folder_for(tmp_path))
    M.append_message(session, {"role": "user", "content": "one"})
    # Another process appends behind this one's back.
    with session.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"id": "0123abcd", "parent": _lines(session)[-1]["id"], "kind": "message",
                                 "version": 1, "ts": 1.0, "message": {"role": "assistant", "content": "two"}}) + "\n")

    M.append_message(session, {"role": "user", "content": "three"})

    assert _lines(session)[-1]["parent"] == "0123abcd"


def test_a_torn_last_line_does_not_swallow_the_next_record(tmp_path):
    session = session_store.reserve(session_store.folder_for(tmp_path))
    M.append_message(session, {"role": "user", "content": "one"})
    with session.open("a", encoding="utf-8") as stream:
        stream.write('{"id":"deadbeef","kind":"mess')

    M.append_message(session, {"role": "user", "content": "two"})

    assert [m["content"] for m in load_messages(session)] == ["one", "two"]


def test_replay_ignores_ids_and_parents(tmp_path):
    session = session_store.reserve(session_store.folder_for(tmp_path))
    live = [{"role": "user", "content": "one"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
            {"role": "user", "content": "interrupted"}]
    M.persist_messages(session, live)
    M.persist_messages(session, [*live[:2], {"role": "tool", "tool_call_id": "c1", "content": "ok"},
                                 {"role": "assistant", "content": "done"}])
    M.append_compaction_mark(session, summary="so far", keep_from=2)
    M.append_message(session, {"role": "user", "content": "after"})
    bare = session.with_name("bare.jsonl")
    bare.write_text("".join(json.dumps({k: v for k, v in record.items() if k not in ("id", "parent")}) + "\n"
                            for record in _lines(session)), encoding="utf-8")

    assert "id" not in _lines(bare)[0]
    assert M.load_replay_messages(session) == M.load_replay_messages(bare)
    assert M.load_replay_messages(session)[0]["content"].startswith("<compaction-summary>")


def test_name_pins_a_title_on_the_session(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(_dir(tmp_path, "work"))
    _repl(monkeypatch, ["--session", "titled"], ["/name fbcon lag", "hello"], [])
    capsys.readouterr()

    session = session_store.folder_for(Path.cwd()) / "titled.jsonl"
    assert session_title(session) == "fbcon lag"
    assert cli.main(["--list", "--json"]) == 0
    [record] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert record["title"] == "fbcon lag"


# --- --session lookup ------------------------------------------------------------------


def test_session_name_resumes_from_any_directory(monkeypatch, tmp_path):
    _replies(monkeypatch, _text("OK"))
    monkeypatch.chdir(_dir(tmp_path, "a"))
    assert cli.main(["--session", "shared", "-p", "one"]) == 0
    monkeypatch.chdir(_dir(tmp_path, "b"))

    assert cli.main(["--session", "shared", "-p", "two"]) == 0

    [session] = _all_sessions()
    assert session.parent == session_store.folder_for(tmp_path / "a")
    assert [m["content"] for m in load_messages(session)] == ["one", "OK", "two", "OK"]


def test_a_name_in_two_other_folders_is_refused_with_both_paths(monkeypatch, tmp_path, capsys):
    found = []
    for name in ("a", "b"):
        path = session_store.folder_for(_dir(tmp_path, name)) / "dup.jsonl"
        M.append_message(path, {"role": "user", "content": name})
        M.append_message(path, {"role": "assistant", "content": "OK"})
        found.append(path)
    _replies(monkeypatch, _text("OK"))
    monkeypatch.chdir(_dir(tmp_path, "c"))

    assert cli.main(["--session", "dup", "-p", "which"]) == 2

    err = capsys.readouterr().err
    assert str(found[0]) in err and str(found[1]) in err
    assert _all_sessions() == sorted(found)
    # The current directory's folder comes first, so there it is not ambiguous.
    monkeypatch.chdir(tmp_path / "a")
    assert cli.main(["--session", "dup", "-p", "mine"]) == 0
    assert [m["content"] for m in load_messages(found[0])][-2:] == ["mine", "OK"]


def test_a_unique_tail_of_a_generated_name_resumes_it(monkeypatch, tmp_path):
    _replies(monkeypatch, _text("OK"))
    monkeypatch.chdir(_dir(tmp_path, "a"))
    assert cli.main(["-p", "one"]) == 0
    [session] = _all_sessions()
    monkeypatch.chdir(_dir(tmp_path, "b"))

    assert cli.main(["--session", session.stem[-4:], "-p", "two"]) == 0

    assert _all_sessions() == [session]
    assert [m["content"] for m in load_messages(session)] == ["one", "OK", "two", "OK"]


def test_a_bare_session_flag_is_the_pickers_and_starts_nothing(monkeypatch, tmp_path):
    monkeypatch.chdir(_dir(tmp_path, "work"))

    assert cli.main(["--session"]) == 2

    assert _all_sessions() == []


def test_last_finds_the_latest_session_from_another_directory(monkeypatch, tmp_path):
    _replies(monkeypatch, _text("OK"))
    monkeypatch.chdir(_dir(tmp_path, "a"))
    assert cli.main(["-p", "one"]) == 0
    monkeypatch.chdir(_dir(tmp_path, "b"))

    assert cli.main(["--last", "-p", "two"]) == 0

    [session] = _all_sessions()
    assert [m["content"] for m in load_messages(session)] == ["one", "OK", "two", "OK"]
