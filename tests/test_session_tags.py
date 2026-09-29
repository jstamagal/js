"""Session tags (js.session_tags): the tag list, what Jev is sent, which tags a
session keeps, the `tags` record and where it shows, and the sweep that tags
ended sessions and retags them when the list changes. The TypeSafe request is
replaced at `session_tags.judge`, or at the SDK's HTTP transport; nothing
reaches the network. Every test runs in the tmp HOME the conftest installs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx2
import pytest
import typesafe_sdk

from js import cli
from js import memory as M
from js import paths, session_index, session_store, session_tags, session_text
from js import session_query as Q
from js.session_catalog import acquire_session, branch_session, record_session_start

STAMP = M.stamp_for("deepseek-v4-flash", "deepseek", "high")
TAG_LIST = """\
js: the js harness itself
nfs / mounts: NFS exports and mounts
gpu / vram: GPUs and their memory
banter:
"""


def _session(cwd: Path, *, mode: str = "repl") -> Path:
    cwd.mkdir(parents=True, exist_ok=True)
    path = session_store.reserve(session_store.folder_for(cwd))
    record_session_start(path, cwd=cwd, agent="defaultagent", model="deepseek-v4-flash", mode=mode)
    return path


def _say(path: Path, user: str, reply: str) -> None:
    M.append_message(path, {"role": "user", "content": user})
    M.append_message(path, {"role": "assistant", "content": reply}, STAMP)


def _call(path: Path, label: str, output: str, call_id: str) -> None:
    M.append_message(path, {"role": "assistant", "content": label, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": "shell", "arguments": json.dumps({"command": "ls"})}}]}, STAMP)
    M.append_message(path, {"role": "tool", "tool_call_id": call_id, "content": f"exit=0\n{output}"})


def _long_session(cwd: Path) -> Path:
    path = _session(cwd)
    _say(path, "the nfs mount hangs", "the export is stale")
    _say(path, "remount it", "done, it answers again")
    return path


def _quick_session(cwd: Path) -> Path:
    path = _session(cwd, mode="-p")
    _say(path, "whats my gpu temp", "54C")
    return path


def _options(**overrides) -> session_tags.Options:
    return session_tags.Options(**{**vars(session_tags.Options.from_settings(None)), **overrides})


class FakeJev:
    """Stands in for the TypeSafe request: scores per tag name, and a record
    of every request."""

    def __init__(self, monkeypatch, scores: dict[str, float] | None = None, error: Exception | None = None):
        self.scores = scores or {}
        self.error = error
        self.requests: list[tuple[dict, dict]] = []
        monkeypatch.setattr(session_tags, "judge", self)

    def __call__(self, state, asked, *, model):
        self.requests.append((state, asked))
        if self.error is not None:
            raise self.error
        return {key: self.scores.get(question["instructions"]["tag"]["name"], 0.0)
                for key, question in asked.items()}


@pytest.fixture
def tag_list(tmp_path, monkeypatch):
    monkeypatch.setenv(session_tags.API_KEY_ENV, "test-key")
    path = paths.tags_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TAG_LIST, encoding="utf-8")
    return path


def _entry(path: Path) -> dict:
    return {entry["path"]: entry for entry in session_index.catalog()}[str(path.resolve())]


# --- the tag list -------------------------------------------------------------------


def test_the_tag_list_is_one_name_and_description_per_line(tmp_path):
    path = tmp_path / "tags.yaml"
    path.write_text(TAG_LIST, encoding="utf-8")
    assert session_tags.load_tags(path) == [
        session_tags.Tag("js", "the js harness itself"),
        session_tags.Tag("nfs / mounts", "NFS exports and mounts"),
        session_tags.Tag("gpu / vram", "GPUs and their memory"),
        session_tags.Tag("banter", ""),
    ]


def test_no_tag_list_file_means_no_tags(tmp_path):
    assert session_tags.load_tags(tmp_path / "missing.yaml") == []


@pytest.mark.parametrize("text", ["- js\n- nfs\n", "js: [a, b]\n", "js: {a: b}\n", "js: [unclosed\n"])
def test_a_tag_list_that_is_not_name_description_lines_is_refused(tmp_path, text):
    path = tmp_path / "tags.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(session_tags.TagListError):
        session_tags.load_tags(path)


def test_the_list_digest_changes_with_a_name_or_a_description():
    base = [session_tags.Tag("js", "the harness")]
    assert session_tags.list_digest(base) == session_tags.list_digest(list(base))
    assert session_tags.list_digest(base) != session_tags.list_digest([session_tags.Tag("js", "other")])
    assert session_tags.list_digest(base) != session_tags.list_digest([session_tags.Tag("jsx", "the harness")])


# --- what Jev is sent -------------------------------------------------------------


def test_the_state_is_the_last_operator_and_model_messages_without_tool_output(tmp_path):
    path = _session(tmp_path / "proj")
    _say(path, "first question", "first answer")
    _call(path, "looking at the mount", "SECRET TOOL OUTPUT", "c1")
    _say(path, "second question", "second answer")
    sent = session_tags.conversation(session_text.rows(path), count=10, chars=2000)
    assert sent == [
        {"from": "operator", "text": "first question"},
        {"from": "model", "text": "first answer"},
        {"from": "model", "text": "looking at the mount"},
        {"from": "operator", "text": "second question"},
        {"from": "model", "text": "second answer"},
    ]
    assert session_tags.conversation(session_text.rows(path), count=2, chars=2000) == sent[-2:]


def test_the_text_of_a_message_with_several_calls_is_sent_once(tmp_path):
    path = _session(tmp_path / "proj")
    M.append_message(path, {"role": "user", "content": "check both"})
    M.append_message(path, {"role": "assistant", "content": "checking", "tool_calls": [
        {"id": f"c{n}", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
        for n in (1, 2)]}, STAMP)
    for n in (1, 2):
        M.append_message(path, {"role": "tool", "tool_call_id": f"c{n}", "content": "exit=0\nout"})
    sent = session_tags.conversation(session_text.rows(path), count=10, chars=2000)
    assert [message["text"] for message in sent] == ["check both", "checking"]


def test_a_long_message_is_cut_to_the_character_limit(tmp_path):
    path = _session(tmp_path / "proj")
    _say(path, "x" * 5000, "short")
    sent = session_tags.conversation(session_text.rows(path), count=10, chars=100)
    assert len(sent[0]["text"]) == 100


def test_each_tag_is_one_noul_question_the_sdk_sends(monkeypatch, tmp_path):
    tags = session_tags.load_tags(Path(_write(tmp_path / "tags.yaml", TAG_LIST)))
    asked = session_tags.questions(tags)
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx2.Response(200, json={
            "model": "jev-test",
            "answers": {key: {"type": "noul", "noul": 0.25 * index}
                        for index, key in enumerate(body["questions"])},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    real = typesafe_sdk.TypeSafeClient
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient",
                        lambda **kwargs: real(api_key="test-key", transport=httpx2.MockTransport(handler), **kwargs))
    state = {"conversation": [{"from": "operator", "text": "hi"}]}
    answers = session_tags.judge(state, asked, model="jev-latest")
    assert answers == {"t0": 0.0, "t1": 0.25, "t2": 0.5, "t3": 0.75}
    (body,) = seen
    assert body["state"] == state and body["model"] == "jev-latest"
    assert [question["type"] for question in body["questions"].values()] == ["noul"] * 4
    assert [question["instructions"]["tag"]["name"] for question in body["questions"].values()] == [
        tag.name for tag in tags]


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- which tags a session keeps ----------------------------------------------------


def test_tags_at_or_over_the_threshold_are_kept_highest_first_at_most_top():
    scores = {"a": 0.61, "b": 0.99, "c": 0.6, "d": 0.8, "e": 0.59}
    assert session_tags.choose(scores, threshold=0.6, top=3) == ["b", "d", "a"]
    assert session_tags.choose(scores, threshold=0.6, top=10) == ["b", "d", "a", "c"]
    assert session_tags.choose(scores, threshold=0.95, top=3) == ["b"]
    assert session_tags.choose({"a": 0.2}, threshold=0.6, top=3) == []


def test_a_tagged_session_shows_its_tags_in_the_text_the_catalog_and_the_tag_filter(tmp_path, monkeypatch):
    FakeJev(monkeypatch, {"nfs / mounts": 0.97, "js": 0.7, "gpu / vram": 0.1})
    path = _long_session(tmp_path / "proj")
    other = _long_session(tmp_path / "other")
    tags = session_tags.load_tags(_write(tmp_path / "tags.yaml", TAG_LIST))

    assert session_tags.tag_session(path, tags, _options()) == ["nfs / mounts", "js"]

    assert "tags: nfs / mounts · js" in session_store.text_path(path).read_text(encoding="utf-8").splitlines()
    assert _entry(path)["tags"] == ["nfs / mounts", "js"]
    sessions = [Q.Session.from_summary(entry) for entry in session_index.catalog()]
    chosen = Q.select(sessions, Q.parse_query("tag:nfs", now=0, home="/", cwd="/"), show_all=False)
    assert [session.path for session in chosen] == [str(path.resolve())]
    assert str(other.resolve()) not in [session.path for session in chosen]
    assert "nfs / mounts" in Q.tags_text(chosen[0])


def test_the_tags_record_does_not_move_the_sessions_last_activity(tmp_path, monkeypatch):
    FakeJev(monkeypatch, {"js": 0.9})
    path = _long_session(tmp_path / "proj")
    before = _entry(path)["last"]
    session_tags.tag_session(path, session_tags.load_tags(_write(tmp_path / "t.yaml", TAG_LIST)), _options())
    assert _entry(path)["last"] == before


def test_the_tags_record_is_not_replayed_or_copied_into_a_branch(tmp_path, monkeypatch):
    FakeJev(monkeypatch, {"js": 0.9})
    path = _long_session(tmp_path / "proj")
    replayed = M.load_messages(path)
    session_tags.tag_session(path, session_tags.load_tags(_write(tmp_path / "t.yaml", TAG_LIST)), _options())
    assert M.load_messages(path) == replayed
    last_message = [row.id for row in session_text.rows(path)][-1]
    branch = branch_session(path, last_message, cwd=tmp_path / "proj")
    assert _entry(branch)["tags"] == []


def test_settings_decide_the_threshold_the_top_and_the_window(tmp_path, monkeypatch):
    jev = FakeJev(monkeypatch, {"js": 0.9, "banter": 0.5, "nfs / mounts": 0.95})
    path = _long_session(tmp_path / "proj")
    options = session_tags.Options.from_settings(
        {"tags": {"threshold": 0.4, "max": 2, "messages": 1, "file": str(tmp_path / "t.yaml")}})
    assert options.file == str(tmp_path / "t.yaml")
    kept = session_tags.tag_session(path, session_tags.load_tags(_write(tmp_path / "t.yaml", TAG_LIST)), options)
    assert kept == ["nfs / mounts", "js"]
    (state, _asked), = jev.requests
    assert state["conversation"] == [{"from": "model", "text": "done, it answers again"}]


def test_the_tags_file_setting_defaults_to_the_home_tag_list():
    assert session_tags.Options.from_settings(None).file == str(paths.tags_file())


# --- the sweep ----------------------------------------------------------------------


def test_the_sweep_tags_shown_sessions_and_leaves_quick_ones(tmp_path, monkeypatch, tag_list):
    jev = FakeJev(monkeypatch, {"nfs / mounts": 0.9, "gpu / vram": 0.9})
    long = _long_session(tmp_path / "proj")
    quick = _quick_session(tmp_path / "proj")

    assert session_tags.sweep(_options()) == 1

    assert len(jev.requests) == 1
    assert _entry(long)["tags"] == ["gpu / vram", "nfs / mounts"]
    assert _entry(quick)["tags"] == []
    assert '"kind":"tags"' not in quick.read_text(encoding="utf-8")


def test_a_tagged_session_is_not_tagged_again_until_something_changes(tmp_path, monkeypatch, tag_list):
    jev = FakeJev(monkeypatch, {"js": 0.9})
    path = _long_session(tmp_path / "proj")
    session_tags.sweep(_options())
    assert session_tags.sweep(_options()) == 0
    assert len(jev.requests) == 1

    _say(path, "one more thing", "sure")
    assert session_tags.sweep(_options()) == 1
    assert len(jev.requests) == 2


def test_editing_the_tag_list_retags(tmp_path, monkeypatch, tag_list):
    jev = FakeJev(monkeypatch, {"js": 0.9, "hardware": 0.95})
    path = _long_session(tmp_path / "proj")
    session_tags.sweep(_options())
    assert _entry(path)["tags"] == ["js"]

    tag_list.write_text(TAG_LIST + "hardware: machines and parts\n", encoding="utf-8")
    assert session_tags.sweep(_options()) == 1
    assert _entry(path)["tags"] == ["hardware", "js"]
    assert len(jev.requests) == 2


def test_a_session_open_in_another_process_is_left_for_later(tmp_path, monkeypatch, tag_list):
    jev = FakeJev(monkeypatch, {"js": 0.9})
    path = _long_session(tmp_path / "proj")
    with acquire_session(path):
        assert session_tags.sweep(_options()) == 0
    assert jev.requests == []
    assert session_tags.sweep(_options()) == 1


def test_no_api_key_means_no_request_and_no_log(tmp_path, monkeypatch, tag_list, capsys):
    monkeypatch.delenv(session_tags.API_KEY_ENV)
    jev = FakeJev(monkeypatch, {"js": 0.9})
    path = _long_session(tmp_path / "proj")
    assert session_tags.sweep(_options()) == 0
    assert jev.requests == []
    assert _entry(path)["tags"] == []
    assert not (paths.logs_root() / "tags.log").exists()
    assert capsys.readouterr() == ("", "")


def test_a_failed_request_ends_the_sweep_with_one_log_line(tmp_path, monkeypatch, tag_list, capsys):
    jev = FakeJev(monkeypatch, error=typesafe_sdk.TypeSafeAPIConnectionError("down"))
    _long_session(tmp_path / "a")
    _long_session(tmp_path / "b")
    assert session_tags.sweep(_options()) == 0
    assert len(jev.requests) == 1
    assert len((paths.logs_root() / "tags.log").read_text(encoding="utf-8").splitlines()) == 1
    assert capsys.readouterr() == ("", "")


def test_a_broken_tag_list_is_logged_and_tags_nothing(tmp_path, monkeypatch, tag_list):
    jev = FakeJev(monkeypatch, {"js": 0.9})
    _long_session(tmp_path / "proj")
    tag_list.write_text("- not\n- a mapping\n", encoding="utf-8")
    assert session_tags.sweep(_options()) == 0
    assert jev.requests == []
    assert (paths.logs_root() / "tags.log").is_file()


# --- starting a sweep ---------------------------------------------------------------


def _popen_calls(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: calls.append(argv))
    return calls


def test_a_sweep_starts_detached_with_the_live_settings(monkeypatch, tag_list):
    calls = _popen_calls(monkeypatch)
    session_tags.start_sweep({"tags": {"threshold": 0.8}})
    (argv,) = calls
    assert argv[1:3] == ["-m", "js.session_tags"]
    options = session_tags.Options.from_json(argv[3])
    assert options.threshold == 0.8
    assert options.file == str(tag_list)


def test_no_sweep_starts_without_an_api_key_or_a_tag_list(monkeypatch, tag_list):
    calls = _popen_calls(monkeypatch)
    monkeypatch.delenv(session_tags.API_KEY_ENV)
    session_tags.start_sweep(None)
    monkeypatch.setenv(session_tags.API_KEY_ENV, "test-key")
    tag_list.unlink()
    session_tags.start_sweep(None)
    assert calls == []


def test_ending_a_session_starts_a_sweep(monkeypatch, tmp_path):
    started: list = []
    monkeypatch.setattr(cli.session_tags, "start_sweep", lambda settings: started.append(settings))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    from repl_driver import LineSession

    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(["hello"]))
    monkeypatch.setattr(cli.runtime, "run_turn", lambda *a, **k: None)
    cli.main(["--blocking"])
    assert len(started) == 1
    assert isinstance(started[0], dict) and "tags" in started[0]


# --- the settings -------------------------------------------------------------------


@pytest.mark.parametrize(("key", "raw", "ok"), [
    ("tags.threshold", "0.75", True), ("tags.threshold", "1.5", False), ("tags.threshold", "-0.1", False),
    ("tags.max", "3", True), ("tags.max", "0", False),
    ("tags.messages", "0", False), ("tags.message_chars", "0", False),
])
def test_tag_settings_are_validated(key, raw, ok):
    from js import settings

    _value, error = settings.coerce_value(settings.spec_for(key), raw)
    assert (error is None) == ok
