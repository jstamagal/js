"""The session picker's state and keys, and how js opens it: `/session` in
the REPL and a bare `js --session`. The state is driven with `press`; one
test draws the prompt_toolkit layout on a short screen. Every test runs in the tmp HOME the
conftest installs."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from js import cli, runtime, session_picker, session_store, session_text
from js import memory as M
from js import messages as msgs
from js import session_query as Q
from js.session_catalog import first_metadata, record_session_start
from js.session_index import Hit

from test_repl_info_cmds import make_cfg

HOME = "/home/op"
CWD = "/home/op/js"
NOW = datetime(2026, 9, 29, 12, 0).timestamp()


def _ts(*parts: int) -> float:
    return datetime(*parts).timestamp()


def _session(path: str, **fields) -> Q.Session:
    base = dict(agent="defaultagent", cwd=CWD, mode="repl", started=_ts(2026, 9, 29, 8, 0),
                last=_ts(2026, 9, 29, 9, 0), turns=5, messages=12, tool_calls=4, replied=True,
                final_len=2000, models=("deepseek-v4-flash",))
    base.update(fields)
    return Q.Session(path=path, **base)


class _Row:
    def __init__(self, number: int, who: str, text: str) -> None:
        self.number, self.who, self.ts, self.model = number, who, None, None
        self.id = f"id{number}"
        self.text = text

    def heading(self) -> str:
        return self.text


def _picker(sessions, **kwargs) -> session_picker.SessionPicker:
    kwargs.setdefault("rows", lambda path: [_Row(n, "you", f"{path} {n}") for n in (1, 2, 3)])
    return session_picker.SessionPicker(sessions, cwd=CWD, home=HOME, now=NOW, **kwargs)


def _listed(picker) -> list[str]:
    return [session.path for session in picker.shown()]


def _body_lines(picker) -> list[str]:
    fragments, _ = picker.body()
    return "".join(text for _, text in fragments).split("\n")


def _type(picker, text: str) -> None:
    for char in text:
        picker.press(char)


# --- the default screen ----------------------------------------------------------


def test_the_list_opens_newest_first_across_dirs_and_agents_with_the_cursor_on_the_newest():
    picker = _picker([
        _session("old", started=_ts(2026, 9, 1, 8, 0), cwd="/srv", agent="research"),
        _session("new", started=_ts(2026, 9, 29, 9, 0), cwd=HOME),
        _session("mid", started=_ts(2026, 9, 17, 8, 0)),
    ])
    assert _listed(picker) == ["new", "mid", "old"]
    assert picker.current().path == "new"


def test_rows_from_the_current_dir_carry_the_dot_and_the_cursor_row_the_arrow():
    picker = _picker([
        _session("here", started=_ts(2026, 9, 29, 9, 0)),
        _session("there", started=_ts(2026, 9, 29, 8, 0), cwd="/srv"),
        _session("here2", started=_ts(2026, 9, 29, 7, 0)),
    ])
    lines = _body_lines(picker)
    assert [line[:1] for line in lines[:3]] == [">", " ", "•"]


def test_empty_quick_subagent_and_script_sessions_are_hidden_until_a():
    sessions = [
        _session("normal"),
        _session("empty", replied=False, started=_ts(2026, 9, 29, 7, 0)),
        _session("quick", turns=1, tool_calls=1, final_len=40, started=_ts(2026, 9, 29, 6, 0)),
        _session("child", mode="subagent", started=_ts(2026, 9, 29, 5, 0)),
        _session("commit", mode="commit", started=_ts(2026, 9, 29, 4, 0)),
    ]
    picker = _picker(sessions)
    assert _listed(picker) == ["normal"]
    picker.press("a")
    assert _listed(picker) == ["normal", "empty", "quick", "child", "commit"]
    picker.press("a")
    assert _listed(picker) == ["normal"]


def test_branches_are_nested_under_their_parent():
    picker = _picker([
        _session("parent", started=_ts(2026, 9, 17, 10, 0)),
        _session("branch", branch_of="parent", branch_point=31, started=_ts(2026, 9, 17, 11, 40)),
        _session("newer", started=_ts(2026, 9, 29, 8, 0)),
    ])
    assert [(item.session.path, item.depth) for item in picker.items] == [
        ("newer", 0), ("parent", 0), ("branch", 1)]


def test_v_cycles_flat_by_dir_by_agent_and_keeps_the_cursor_on_its_session():
    picker = _picker([_session("a", cwd=HOME), _session("b", cwd=CWD, started=_ts(2026, 9, 28, 8, 0))])
    picker.press("down")
    assert picker.current().path == "b"
    views = []
    for _ in range(3):
        picker.press("v")
        views.append(picker.view)
        assert picker.current().path == "b"
    assert views == ["dir", "agent", "flat"]
    picker.press("v")
    assert [item.group for item in picker.items if item.session is None] == ["[~]", "[~/js]"]


def test_enter_resumes_the_highlighted_session():
    picker = _picker([_session("a"), _session("b", started=_ts(2026, 9, 28, 8, 0))])
    picker.press("j")
    choice = picker.press("enter")
    assert (choice.action, choice.session.path) == (session_picker.RESUME, "b")


@pytest.mark.parametrize("key", ["escape", "q", "c-c"])
def test_esc_q_and_ctrl_c_close_with_no_choice(key):
    assert _picker([_session("a")]).press(key) is session_picker.CLOSED


def test_b_on_a_session_whose_file_is_gone_shows_an_empty_list():
    # ALL-CLAIMS.md:493: FileNotFoundError out of the key handler killed the app.
    def gone(path):
        raise FileNotFoundError(path)

    picker = _picker([_session("a")], rows=gone)
    picker.press("b")
    assert picker.screen == "messages"
    assert picker.message_rows == []


def test_root_cwd_sessions_carry_the_here_dot():
    picker = session_picker.SessionPicker(
        [_session("a", cwd="/"), _session("b", cwd=HOME, started=_ts(2026, 9, 28, 8, 0))],
        cwd="/", home=HOME, now=NOW)
    picker.press("down")
    assert any(line.startswith("•") for line in _body_lines(picker))


@pytest.mark.parametrize("unknown", [None, ""])
def test_a_session_with_no_known_cwd_is_not_marked_here_under_root(unknown):
    # shinda #439: an unknown cwd must not read as "/".
    picker = session_picker.SessionPicker(
        [_session("b", cwd=HOME), _session("a", cwd=unknown, started=_ts(2026, 9, 28, 8, 0))],
        cwd="/", home=HOME, now=NOW)
    assert not any(line.startswith("•") for line in _body_lines(picker))


# --- messages and info ---------------------------------------------------------


def test_b_lists_messages_enter_branches_at_the_row_and_r_resumes_at_the_end():
    picker = _picker([_session("a")])
    picker.press("b")
    assert picker.screen == "messages"
    assert picker.message_cursor == 2
    picker.press("up")
    choice = picker.press("enter")
    assert (choice.action, choice.session.path, choice.message, choice.message_id) == (
        session_picker.BRANCH, "a", 2, "id2")
    choice = picker.press("r")
    assert (choice.action, choice.message) == (session_picker.RESUME, None)


def test_esc_from_messages_returns_to_the_list_with_both_positions_kept():
    picker = _picker([_session("a"), _session("b", started=_ts(2026, 9, 28, 8, 0))])
    picker.press("down")
    picker.press("b")
    picker.press("home")
    assert picker.press("escape") is None
    assert (picker.screen, picker.current().path) == ("list", "b")
    picker.press("b")
    assert picker.message_cursor == 0


def test_i_shows_the_path_model_stamps_token_count_and_branch_parent():
    parent = str(session_store.paths.sessions_root() / "-home-op-js" / "2026-09-17T1001-abcd.jsonl")
    picker = _picker([_session("/s/branch.jsonl", branch_of=parent, branch_point=31,
                               model_changes=(("deepseek-v4-flash", 2), ("mimo", 31)),
                               last_stamp={"model": "mimo", "provider": "xiaomi"})],
                     tokens=lambda path: 12345)
    picker.press("i")
    assert picker.screen == "info"
    values = dict(picker.info_rows())
    assert values[msgs.SESSIONS_INFO_FILE.text()] == "/s/branch.jsonl"
    assert "mimo" in values[msgs.SESSIONS_INFO_MODELS.text()]
    assert "deepseek-v4-flash" in values[msgs.SESSIONS_INFO_MODELS.text()]
    assert "12,345" in values[msgs.SESSIONS_INFO_TOKENS.text()]
    branch = values[msgs.SESSIONS_INFO_BRANCH.text()]
    assert "2026-09-17T1001-abcd" in branch and "#0031" in branch
    picker.press("escape")
    assert picker.screen == "list"


def test_tool_rows_are_labelled_by_the_models_text_or_else_the_call(tmp_path):
    path = session_store.reserve(session_store.folder_for(tmp_path))
    stamp = M.stamp_for("deepseek-v4-flash", "deepseek", None)
    M.append_message(path, {"role": "user", "content": "why is it slow"})
    M.append_message(path, {"role": "assistant", "content": "sniff the fbcon timings", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": json.dumps(
            {"command": "dmesg | rg fbcon\necho done"})}}]}, stamp)
    M.append_message(path, {"role": "tool", "tool_call_id": "c1", "content": "exit=0\nSECRET OUTPUT"})
    M.append_message(path, {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c2", "type": "function", "function": {"name": "shell", "arguments": json.dumps(
            {"command": "cat /proc/fb"})}}]}, stamp)
    M.append_message(path, {"role": "tool", "tool_call_id": "c2", "content": "exit=0\nMORE OUTPUT"})
    lines = Q.message_lines(session_text.rows(path))
    assert "tool:shell" in lines[1] and "sniff the fbcon timings" in lines[1]
    assert "tool:shell" in lines[2] and "$ cat /proc/fb" in lines[2]
    assert not any("OUTPUT" in line for line in lines)
    assert "deepseek-v4-flash" in lines[1] and "deepseek-v4-flash" not in lines[2]


# --- search --------------------------------------------------------------------


def test_slash_types_a_query_that_filters_live_and_shows_how_it_was_read():
    picker = _picker([_session("few", turns=2), _session("many", turns=20, started=_ts(2026, 9, 28, 8, 0))])
    picker.press("/")
    assert picker.screen == "search"
    _type(picker, ">10")
    assert _listed(picker) == ["many"]
    assert Q.describe(picker.query) in "".join(text for _, text in picker.subtitle())
    picker.press("enter")
    assert (picker.screen, _listed(picker)) == ("list", ["many"])
    picker.press("escape")
    assert (picker.query_text, _listed(picker)) == ("", ["few", "many"])
    assert picker.press("escape") is session_picker.CLOSED


def test_words_rank_by_the_search_scores_and_each_hit_shows_its_matching_line():
    seen = {}

    def search(expression):
        seen["expression"] = expression
        return {"b": -5.0, "a": -1.0}

    def lines(expression, scores):
        return {path: Hit(score, 14, "you", None, "the niri thing", ((4, 8),)) for path, score in scores.items()}

    picker = _picker([_session("a"), _session("b", started=_ts(2026, 9, 1, 8, 0)), _session("c")],
                     search=search, lines=lines)
    picker.press("/")
    _type(picker, "niri")
    assert seen["expression"] == '"niri"*'
    assert _listed(picker) == ["b", "a"]
    body = _body_lines(picker)
    assert "#0014" in body[1] and "the niri thing" in body[1]


def test_quick_sessions_are_searched_only_with_a():
    quick = _session("quick", turns=1, tool_calls=0, final_len=10)
    picker = _picker([quick, _session("normal")], search=lambda expression: {"quick": -2.0, "normal": -1.0})
    picker.press("/")
    _type(picker, "gpu")
    assert _listed(picker) == ["normal"]
    picker.press("enter")
    picker.press("a")
    assert _listed(picker) == ["quick", "normal"]


def test_backspace_and_ctrl_u_edit_the_query():
    picker = _picker([_session("a")])
    picker.press("/")
    _type(picker, "niri mb")
    picker.press("backspace")
    assert picker.query_text == "niri m"
    picker.press("c-w")
    assert picker.query_text == "niri "
    picker.press("c-u")
    assert picker.query_text == ""


# --- how js opens it -------------------------------------------------------------


def _write_session(cwd: Path, agent: str = "defaultagent") -> Path:
    cwd.mkdir(parents=True, exist_ok=True)
    path = session_store.reserve(session_store.folder_for(cwd))
    record_session_start(path, cwd=cwd, agent=agent, model="deepseek-v4-flash", mode="repl")
    M.append_message(path, {"role": "user", "content": "hello"})
    M.append_message(path, {"role": "assistant", "content": "hi"}, M.stamp_for("mimo", "xiaomi", None))
    return path


def _choice(path: Path, cwd: Path, agent: str = "defaultagent", action=session_picker.RESUME, message=None,
            message_id=None, command=()):
    session = Q.Session(path=str(path.resolve()), agent=agent, cwd=str(cwd.resolve()), command=tuple(command))
    return session_picker.Choice(action, session, message, message_id)


def test_slash_session_opens_the_picker_and_a_chosen_session_ends_the_repl_to_switch(tmp_path, monkeypatch):
    other = _write_session(tmp_path / "other", agent="research")
    opened = {}

    def pick(cwd, query=""):
        opened["query"] = query
        return _choice(other, tmp_path / "other", agent="research")

    monkeypatch.setattr(session_picker, "pick_session", pick)
    cfg = make_cfg(tmp_path)
    state = {"messages": [], "running": True}
    assert cli._handle_command("/session niri", state, cfg) is True
    assert opened["query"] == "niri"
    assert state["running"] is False
    target = state["switch_session"]
    assert (target.path, target.agent, target.cwd) == (other.resolve(), "research", str((tmp_path / "other").resolve()))


def test_slash_session_on_the_current_session_or_closed_keeps_the_repl(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.session_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.session_file.write_text("", encoding="utf-8")
    state = {"messages": [], "running": True}
    monkeypatch.setattr(session_picker, "pick_session", lambda cwd, query="": None)
    cli._handle_command("/session", state, cfg)
    monkeypatch.setattr(session_picker, "pick_session",
                        lambda cwd, query="": _choice(cfg.session_file, tmp_path))
    cli._handle_command("/session", state, cfg)
    assert state["running"] is True and "switch_session" not in state


def test_a_branch_choice_creates_the_branch_in_the_parents_folder(tmp_path):
    parent = _write_session(tmp_path / "proj")
    first = session_text.rows(parent)[0]
    target = cli._session_target(_choice(parent, tmp_path / "proj", action=session_picker.BRANCH,
                                         message=first.number, message_id=first.id))
    assert target.path != parent and target.path.parent == parent.parent
    start = first_metadata(target.path)
    assert start["branched_from"] == {"session": str(parent.resolve()), "message": first.id}
    assert [m["role"] for m in M.load_replay_messages(target.path)] == ["user"]
    assert session_text.render(target.path).splitlines()[3].endswith("#0001")


def test_the_switch_reexecs_js_in_the_sessions_dir_with_its_agent(tmp_path, monkeypatch):
    target = cli._SessionTarget(tmp_path / "s.jsonl", str(tmp_path), "research")
    ran = {}

    def execv(program, argv):
        ran.update(program=program, argv=argv, cwd=os.getcwd())

    monkeypatch.setattr(cli.os, "execv", execv)
    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "cwd", Path.cwd())
    monkeypatch.chdir(Path.cwd())
    cli._exec_session(target, blocking=True)
    assert ran["cwd"] == str(tmp_path)
    assert ran["argv"][ran["argv"].index("--session") + 1] == str(tmp_path / "s.jsonl")
    assert ran["argv"][ran["argv"].index("-a") + 1] == "research"
    assert "--blocking" in ran["argv"]


def test_a_session_started_under_c_resumes_jailed_in_its_dir(tmp_path, monkeypatch):
    path = _write_session(tmp_path / "proj")
    target = cli._session_target(_choice(path, tmp_path / "proj", command=["js", "-C", "proj"]))
    assert target.jailed
    ran = {}
    monkeypatch.setattr(cli.os, "execv", lambda program, argv: ran.update(argv=argv))
    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "cwd", Path.cwd())
    monkeypatch.chdir(Path.cwd())
    cli._exec_session(target, blocking=False)
    assert ran["argv"][ran["argv"].index("-C") + 1] == str((tmp_path / "proj").resolve())
    assert not cli._session_target(_choice(path, tmp_path / "proj", command=["js", "--commit"])).jailed


class _Terminal:
    def isatty(self) -> bool:
        return True

    def read(self) -> str:
        return ""


def test_bare_session_needs_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(session_picker, "pick_session", lambda cwd, query="": pytest.fail("opened"))
    assert cli.main(["--session"]) == 2
    assert msgs.SESSIONS_NEEDS_TERMINAL.text() in capsys.readouterr().err


def test_bare_session_closed_with_no_choice_exits_cleanly(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(sys, "stdout", _Terminal())
    monkeypatch.setattr(session_picker, "pick_session", lambda cwd, query="": None)
    assert cli.main(["--session"]) == 0


def test_bare_session_resumes_the_choice_with_its_own_agent_and_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "proj"
    path = _write_session(session_dir, agent="research")
    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(sys, "stdout", _Terminal())
    monkeypatch.setattr(session_picker, "pick_session", lambda cwd, query="": _choice(path, session_dir, "research"))
    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "cwd", Path.cwd())
    monkeypatch.chdir(tmp_path)
    seen = {}

    def config(session, **kwargs):
        seen.update(session=session, agent=kwargs.get("agent_id"), cwd=os.getcwd())
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "_from_env", config)
    monkeypatch.setattr(cli, "_warn_missing_binaries", lambda: None)
    assert cli.main(["--session"]) == 2
    assert seen == {"session": str(path.resolve()), "agent": "research", "cwd": str(session_dir.resolve())}


def test_bare_session_enters_the_jail_of_a_session_started_under_c(tmp_path, monkeypatch):
    session_dir = tmp_path / "proj"
    path = _write_session(session_dir)
    monkeypatch.setattr(sys, "stdin", _Terminal())
    monkeypatch.setattr(sys, "stdout", _Terminal())
    monkeypatch.setattr(session_picker, "pick_session",
                        lambda cwd, query="": _choice(path, session_dir, command=["js", "-C", str(session_dir)]))
    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "cwd", Path.cwd())
    monkeypatch.chdir(tmp_path)
    entered = {}

    def enter(root, **_settings):
        entered["root"] = root
        raise cli._jail.JailError("no bwrap here")

    monkeypatch.setattr(cli._jail, "enter", enter)
    assert cli.main(["--session"]) == 2
    assert entered["root"] == session_dir.resolve()


# --- the highlighted session's last lines ----------------------------------------


def _preview_picker(messages: list[dict], sessions=None) -> session_picker.SessionPicker:
    return _picker(sessions or [_session("a")], messages=lambda path: messages)


def _call(call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "shell", "arguments": "{}"}}


def test_two_messages_that_open_alike_preview_the_later_one():
    picker = _preview_picker([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "Checking now\nold tail", "tool_calls": [_call("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "Checking now", "tool_calls": [_call("c2")]},
        {"role": "tool", "tool_call_id": "c2", "content": "ok"},
    ])

    body = "\n".join(_body_lines(picker))

    assert "old tail" not in body


def test_text_beside_a_tool_call_still_waiting_for_its_result_is_previewed():
    picker = _preview_picker([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "📌 old diagnosis"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "📌 new diagnosis", "tool_calls": [_call("c1")]},
    ])

    body = "\n".join(_body_lines(picker))

    assert "📌 new diagnosis" in body
    assert "old diagnosis" not in body


def test_a_compacted_session_previews_what_its_transcript_still_holds(tmp_path):
    """Compaction drops summarized history from the model's replay, not from
    the session; the preview reads the session. A reattached file is js's,
    not the operator's."""
    path = tmp_path / "s.jsonl"
    M.append_message(path, {"role": "user", "content": "fix the nfs mount"})
    M.append_message(path, {"role": "assistant", "content": "📌 mount fixed in fstab"})
    M.append_compaction_mark(path, summary="the operator fixed a mount", keep_from=2,
                             rehydrated={"role": "user", "content": "REATTACHED FILE BODY"})
    M.append_message(path, {"role": "assistant", "content": "anything else?"})
    picker = _picker([_session(str(path))], messages=session_picker._messages)

    body = "\n".join(_body_lines(picker))

    assert "fix the nfs mount" in body
    assert "📌 mount fixed in fstab" in body
    assert "REATTACHED FILE BODY" not in body


def test_a_signed_reply_kept_by_compaction_then_rolled_back_is_not_previewed(tmp_path):
    """Compaction replaces a signed reply with a copy without its signature;
    the rollback that takes the copy out takes the reply out."""
    path = tmp_path / "s.jsonl"
    M.append_message(path, {"role": "user", "content": "why is it slow"})
    M.append_message(path, {"role": "assistant", "content": "📌 old diagnosis",
                            "reasoning_parts": [{"type": "thinking", "signature": "sig"}],
                            "reasoning_from": {"provider": "anthropic", "model": "m"}})
    M.append_compaction_mark(path, summary="asked why it is slow", keep_from=0)
    M.append_mark(path, "rollback_to:2")
    M.append_message(path, {"role": "assistant", "content": "current reply"})
    picker = _picker([_session(str(path))], messages=session_picker._messages)

    body = "\n".join(_body_lines(picker))

    assert "current reply" in body
    assert "old diagnosis" not in body


def test_a_session_from_before_the_record_envelope_is_previewed(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"role": "user", "content": "old question"}) + "\n"
                    + json.dumps({"role": "assistant", "content": "📌 old answer"}) + "\n", encoding="utf-8")
    picker = _picker([_session(str(path))], messages=session_picker._messages)

    body = "\n".join(_body_lines(picker))

    assert "old question" in body
    assert "📌 old answer" in body


def test_an_open_picker_previews_a_session_file_that_changed(tmp_path):
    path = tmp_path / "s.jsonl"
    M.append_message(path, {"role": "user", "content": "first"})
    picker = _picker([_session(str(path))], messages=session_picker._messages)
    _body_lines(picker)
    M.append_message(path, {"role": "user", "content": "second"})

    assert "second" in "\n".join(_body_lines(picker))


def test_session_picker_accepts_its_view_and_streams(monkeypatch):
    from types import SimpleNamespace
    from js import picker

    source, target = object(), object()
    seen = []
    state = SimpleNamespace(application=lambda **kwargs: seen.append(kwargs) or "app")
    monkeypatch.setattr(picker, "run_modal", lambda app: app)
    assert session_picker.pick_session("/work", input=source, output=target,
                                       load_picker=lambda *args: state) == "app"
    assert seen == [{"input": source, "output": target}]
