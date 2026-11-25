"""The project namespace: sessions are filed by the project a run starts in
(its root, else the start directory), each start record keeps the directory
it started in, a name prefers the session started here, sessions filed under
a start directory before stay findable, and prompts see the project as
%%CURRENT_PROJECT%% and %%CURRENT_PROJECT_ROOT%%."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import ai
import pytest

from js import cli, context_window, memory as M, paths, persona, runtime, session_store
from js.config import from_env
from js.memory import load_messages
from js.model_client import ModelStreamResult
from js.session_catalog import first_metadata, record_session_start


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(runtime.model_metadata, "accepts_image_input", lambda *a, **k: False)
    monkeypatch.setattr(context_window, "resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)
    for name in ("JS_DEFAULT_AGENT", "JS_SESSION", "JS_MODEL"):
        monkeypatch.delenv(name, raising=False)
    result = ModelStreamResult(
        text="OK", tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=2),
        finish_reason="stop", assistant_message=ai.assistant_message("OK"),
    )
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **_kwargs: result)


@pytest.fixture
def mono(tmp_path):
    root = tmp_path / "monorepo"
    (root / ".git").mkdir(parents=True)
    package = root / "packages" / "dingus"
    package.mkdir(parents=True)
    return root.resolve(), package.resolve()


def _sessions() -> list[Path]:
    root = paths.sessions_root()
    return sorted(path for path in root.rglob("*.jsonl")) if root.is_dir() else []


def test_the_namespace_is_the_project_root_else_the_directory(tmp_path, mono):
    root, package = mono
    loose = tmp_path / "loose"
    loose.mkdir()
    assert paths.project_namespace(package) == root
    assert paths.project_namespace(root) == root
    assert paths.project_namespace(loose) == loose.resolve()


def test_every_directory_of_a_project_files_sessions_in_the_root_folder(monkeypatch, mono):
    root, package = mono
    for directory in (root, package):
        monkeypatch.chdir(directory)
        assert cli.main(["-p", "hello"]) == 0

    found = _sessions()
    assert len(found) == 2
    assert {path.parent for path in found} == {session_store.folder_for(root)}
    assert sorted(first_metadata(path)["cwd"] for path in found) == sorted([str(root), str(package)])


def test_a_named_session_is_one_session_across_the_project(monkeypatch, mono):
    root, package = mono
    monkeypatch.chdir(package)
    assert cli.main(["--session", "notes", "-p", "one"]) == 0
    monkeypatch.chdir(root)
    assert cli.main(["--session", "notes", "-p", "two"]) == 0

    [session] = _sessions()
    assert [m["content"] for m in load_messages(session)] == ["one", "OK", "two", "OK"]


def _generated(folder: Path, stem: str, cwd: Path) -> Path:
    path = folder / f"{stem}.jsonl"
    folder.mkdir(parents=True, exist_ok=True)
    record_session_start(path, cwd=cwd, agent="defaultagent", model="m")
    M.append_message(path, {"role": "user", "content": str(cwd)})
    M.append_message(path, {"role": "assistant", "content": "OK"})
    return path


def test_a_tail_two_project_sessions_share_takes_the_one_started_here(monkeypatch, mono):
    root, package = mono
    folder = session_store.folder_for(root)
    at_root = _generated(folder, "2026-11-24T1000-beef", root)
    in_package = _generated(folder, "2026-11-24T1100-beef", package)

    monkeypatch.chdir(package)
    assert cli.main(["--session", "beef", "-p", "more"]) == 0
    assert load_messages(in_package)[-2]["content"] == "more"
    monkeypatch.chdir(root)
    assert cli.main(["--session", "beef", "-p", "more"]) == 0
    assert load_messages(at_root)[-2]["content"] == "more"


def test_a_session_filed_under_its_start_directory_stays_findable(monkeypatch, tmp_path, mono):
    root, package = mono
    old = _generated(session_store.folder_for(package), "2026-11-01T0900-cafe", package)
    # The same tail elsewhere: the start directory's own folder comes before it.
    _generated(session_store.folder_for(tmp_path / "elsewhere"), "2026-11-01T0800-cafe", tmp_path / "elsewhere")

    monkeypatch.chdir(package)
    assert cli.main(["--session", "cafe", "-p", "again"]) == 0

    assert load_messages(old)[-2]["content"] == "again"


def test_prompts_see_the_project_name_and_root(monkeypatch, mono):
    root, package = mono
    monkeypatch.chdir(package)
    agent = root / ".js" / "agents" / "scout"
    agent.mkdir(parents=True)
    (agent / "01-prompt.md").write_text("%%CURRENT_PROJECT%%\n%%CURRENT_PROJECT_ROOT%%\n", encoding="utf-8")
    cfg = replace(from_env(save_session=False, agent_id="scout"), model="m")

    assert persona.load_configured_prompt_spec(cfg).system == f"monorepo\n{root}\n"


def test_outside_a_project_the_prompt_project_is_the_directory(monkeypatch, tmp_path):
    work = tmp_path / "scratch"
    agent = work / ".js" / "agents" / "scout"
    agent.mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(paths, "global_agents_dir", lambda: agent.parent)
    (agent / "01-prompt.md").write_text("%%CURRENT_PROJECT%% %%CURRENT_PROJECT_ROOT%%\n", encoding="utf-8")
    cfg = replace(from_env(save_session=False, agent_id="scout"), model="m")

    assert persona.load_configured_prompt_spec(cfg).system == f"scratch {work.resolve()}\n"


@pytest.mark.parametrize("marker", [".isolate", ".js/.isolate"])
def test_an_isolated_directory_outside_git_is_its_own_namespace(monkeypatch, tmp_path, marker):
    scratch = (tmp_path / "scratch").resolve()
    sub = scratch / "notes" / "today"
    sub.mkdir(parents=True)
    (scratch / marker).parent.mkdir(parents=True, exist_ok=True)
    (scratch / marker).touch()
    assert paths.project_namespace(sub) == scratch

    for directory in (scratch, sub):
        monkeypatch.chdir(directory)
        assert cli.main(["-p", "hello"]) == 0

    found = _sessions()
    assert len(found) == 2
    assert {path.parent for path in found} == {session_store.folder_for(scratch)}
    assert sorted(first_metadata(path)["cwd"] for path in found) == sorted([str(scratch), str(sub)])


def test_prompts_see_an_isolated_directory_as_the_project(monkeypatch, tmp_path):
    scratch = (tmp_path / "scratch").resolve()
    sub = scratch / "sub"
    agent = scratch / ".js" / "agents" / "scout"
    agent.mkdir(parents=True)
    sub.mkdir()
    (scratch / ".isolate").touch()
    (agent / "01-prompt.md").write_text("%%CURRENT_PROJECT%% %%CURRENT_PROJECT_ROOT%%\n", encoding="utf-8")
    monkeypatch.chdir(sub)
    cfg = replace(from_env(save_session=False, agent_id="scout"), model="m")

    assert persona.load_configured_prompt_spec(cfg).system == f"scratch {scratch}\n"
