"""Where a toolbox save lands and which sessions see it.

A save is local unless it says `global`: into the project's own `.js/toolbox`
when one exists, else onto the project's shelf in the home toolbox. `global`
goes flat at the top of the home toolbox. Under isolation the home is neither
read nor written."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from js import paths, session_store
from js.toolkit import ToolContext, toolbox as tbmod


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _context(cwd: Path) -> ToolContext:
    context = ToolContext(cwd=cwd)
    context.model = "test-model"
    context.kernel_verbosity = "quiet"
    return context


def _save(cwd: Path, name: str, body: str = "", **options) -> str:
    source = body or f"def {name}():\n    return {name!r}\n"
    return tbmod.toolbox(action="save", name=name, source=source, context=_context(cwd), **options)


def _shelf(project: Path) -> Path:
    return paths.global_toolbox_dir() / session_store.slug(project)


@pytest.fixture
def projects(tmp_path):
    return _repo(tmp_path / "work" / "alpha"), _repo(tmp_path / "work" / "beta")


def test_a_save_without_scope_lands_on_the_projects_shelf_and_only_that_project_sees_it(projects):
    alpha, beta = projects
    assert not _save(alpha, "parse_alpha_log").startswith("ERROR")

    assert (_shelf(alpha) / "parse_alpha_log.py").is_file()
    assert not (paths.global_toolbox_dir() / "parse_alpha_log.py").exists()
    assert not (alpha / ".js").exists()
    assert tbmod.discover(alpha)["parse_alpha_log"].scope == tbmod.SHELF
    assert "parse_alpha_log" not in tbmod.discover(beta)


def test_a_project_js_directory_without_a_toolbox_still_saves_to_the_shelf(projects):
    alpha, _ = projects
    (alpha / ".js").mkdir()
    (alpha / ".js" / "jsrc").write_text("", encoding="utf-8")

    _save(alpha, "shelved")

    assert (_shelf(alpha) / "shelved.py").is_file()
    assert not (alpha / ".js" / "toolbox").exists()


def test_a_subdirectory_of_the_project_saves_to_the_same_shelf(projects):
    alpha, _ = projects
    nested = alpha / "src" / "deep"
    nested.mkdir(parents=True)
    _save(nested, "nested_tool")
    assert (_shelf(alpha) / "nested_tool.py").is_file()
    assert "nested_tool" in tbmod.discover(alpha)


def test_a_project_with_its_own_toolbox_keeps_its_saves(projects):
    alpha, _ = projects
    own = alpha / ".js" / "toolbox"
    own.mkdir(parents=True)

    _save(alpha, "release_steps")

    assert (own / "release_steps.py").is_file()
    assert not _shelf(alpha).exists()
    assert tbmod.discover(alpha)["release_steps"].scope == tbmod.PROJECT


def test_a_global_save_lands_flat_in_the_home_toolbox_and_is_seen_everywhere(projects, tmp_path):
    alpha, beta = projects
    loose = tmp_path / "loose"
    loose.mkdir()

    _save(alpha, "everywhere", scope="global")

    assert (paths.global_toolbox_dir() / "everywhere.py").is_file()
    for cwd in (alpha, beta, loose):
        assert tbmod.discover(cwd)["everywhere"].scope == tbmod.GLOBAL


def test_a_session_in_the_home_directory_saves_to_the_home_toolbox(projects):
    home_directory = paths.user_home()
    alpha, _ = projects

    _save(home_directory, "home_tool")

    assert (paths.global_toolbox_dir() / "home_tool.py").is_file()
    assert tbmod.discover(home_directory)["home_tool"].scope == tbmod.GLOBAL
    assert "home_tool" in tbmod.discover(alpha)


def test_two_projects_with_one_name_do_not_share_a_shelf(tmp_path):
    first = _repo(tmp_path / "a" / "fork")
    second = _repo(tmp_path / "b" / "fork")

    _save(first, "fork_tool")

    assert _shelf(first) != _shelf(second)
    assert "fork_tool" in tbmod.discover(first)
    assert "fork_tool" not in tbmod.discover(second)


def test_an_explicit_local_save_shadows_a_global_tool_in_that_project_only(projects):
    alpha, beta = projects
    _save(alpha, "who", "def who():\n    return 'global'\n", scope="global")
    _save(alpha, "who", "def who():\n    return 'local'\n", scope="local")

    record = tbmod.discover(alpha)["who"]
    assert record.scope == tbmod.SHELF
    assert "'local'" in record.path.read_text(encoding="utf-8")
    assert tbmod.discover(beta)["who"].scope == tbmod.GLOBAL
    global_file = paths.global_toolbox_dir() / "who.py"
    assert "'global'" in global_file.read_text(encoding="utf-8")
    assert not (paths.global_toolbox_dir() / ".history").exists()


def test_history_and_restore_work_on_a_shelf_tool(projects):
    alpha, _ = projects
    _save(alpha, "adder", "def adder(a, b):\n    return a + b\n", note="first cut")
    _save(alpha, "adder", "def adder(a, b):\n    return 0\n", note="broke it")
    shelf = _shelf(alpha)

    report = tbmod.history_of(alpha, "adder")
    assert "[shelf]" in report and report.strip().endswith("restorable: r1")

    restored = tbmod.restore(alpha, "adder", 1)
    assert not restored.startswith("ERROR")
    record = tbmod.discover(alpha)["adder"]
    assert record.scope == tbmod.SHELF and record.path == shelf / "adder.py"
    assert record.revision == 3
    assert "return a + b" in record.path.read_text(encoding="utf-8")
    assert sorted(path.name for path in (shelf / ".history").iterdir()) == ["adder.r1.py", "adder.r2.py"]
    assert not (paths.global_toolbox_dir() / "adder.py").exists()


def test_a_scope_other_than_local_or_global_is_refused(projects):
    alpha, _ = projects
    report = _save(alpha, "nowhere", scope="project")
    assert report.startswith("ERROR")
    assert "nowhere" not in tbmod.discover(alpha)


def test_a_global_save_may_not_lean_on_a_local_tool(projects):
    alpha, _ = projects
    _save(alpha, "helper")
    body = "def caller():\n    return helper()\n"

    assert not _save(alpha, "caller", body).startswith("ERROR")
    refused = _save(alpha, "caller_everywhere", body.replace("caller", "caller_everywhere"), scope="global")
    assert refused.startswith("ERROR") and "helper" in refused
    assert not (paths.global_toolbox_dir() / "caller_everywhere.py").exists()


def test_a_global_save_may_call_a_global_tool_this_project_shadows(projects):
    alpha, _ = projects
    _save(alpha, "helper", scope="global")
    _save(alpha, "helper", scope="local")
    body = "def caller_everywhere():\n    return helper()\n"

    assert not _save(alpha, "caller_everywhere", body, scope="global").startswith("ERROR")


def test_an_isolated_directory_does_not_touch_the_home(tmp_path):
    root = _repo(tmp_path / "isolated")
    (root / ".isolate").write_text("", encoding="utf-8")
    home_box = paths.global_toolbox_dir()
    home_box.mkdir(parents=True)
    (home_box / "home_only.py").write_text("def home_only():\n    return 1\n", encoding="utf-8")
    before = sorted(home_box.rglob("*"))

    context = _context(root)
    context.toolbox_seed_examples = True
    listing = tbmod.toolbox(action="list", context=context)
    local = _save(root, "kept_here")
    refused = _save(root, "everywhere", scope="global")

    assert "home_only" not in listing
    assert (root / ".js" / "toolbox" / "kept_here.py").is_file()
    assert tbmod.discover(root)["kept_here"].scope == tbmod.PROJECT
    assert not local.startswith("ERROR")
    assert refused.startswith("ERROR")
    assert sorted(home_box.rglob("*")) == before


def test_a_save_under_a_global_tools_name_revises_the_global_tool(projects):
    alpha, beta = projects
    _save(alpha, "shared", "def shared():\n    return 'broken'\n", scope="global")

    _save(alpha, "shared", "def shared():\n    return 'fixed'\n")

    global_file = paths.global_toolbox_dir() / "shared.py"
    assert not _shelf(alpha).exists()
    assert (paths.global_toolbox_dir() / ".history" / "shared.r1.py").is_file()
    for cwd in (alpha, beta):
        record = tbmod.discover(cwd)["shared"]
        assert record.path == global_file and record.revision == 2
        assert "'fixed'" in record.path.read_text(encoding="utf-8")


def test_a_save_under_a_shelf_tools_name_revises_it_on_the_shelf(projects):
    alpha, _ = projects
    own = alpha / ".js" / "toolbox"
    _save(alpha, "shelved", "def shelved():\n    return 1\n")
    own.mkdir(parents=True)

    _save(alpha, "shelved", "def shelved():\n    return 2\n")

    record = tbmod.discover(alpha)["shelved"]
    assert record.path == _shelf(alpha) / "shelved.py" and record.revision == 2
    assert not (own / "shelved.py").exists()


def test_a_save_revises_the_nearest_tool_of_the_name(projects):
    alpha, beta = projects
    _save(alpha, "who", "def who():\n    return 'global'\n", scope="global")
    _save(alpha, "who", "def who():\n    return 'local'\n", scope="local")

    _save(alpha, "who", "def who():\n    return 'local fixed'\n")

    assert tbmod.discover(alpha)["who"].revision == 2
    assert tbmod.discover(alpha)["who"].scope == tbmod.SHELF
    assert "'global'" in tbmod.discover(beta)["who"].path.read_text(encoding="utf-8")


def test_a_fix_to_a_global_tool_may_not_lean_on_a_local_tool(projects):
    alpha, _ = projects
    _save(alpha, "shared", scope="global")
    _save(alpha, "helper")

    refused = _save(alpha, "shared", "def shared():\n    return helper()\n")

    assert refused.startswith("ERROR") and "helper" in refused
    assert tbmod.discover(alpha)["shared"].revision == 1


def test_a_new_name_still_lands_locally_beside_global_tools(projects):
    alpha, _ = projects
    _save(alpha, "existing", scope="global")
    _save(alpha, "brand_new")
    assert tbmod.discover(alpha)["brand_new"].scope == tbmod.SHELF


def _worktree(main: Path, where: Path, name: str) -> Path:
    """A linked worktree of ``main`` at ``where``, laid out the way git lays one out."""
    admin = main / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "HEAD").write_text("ref: refs/heads/" + name + "\n", encoding="utf-8")
    where.mkdir(parents=True)
    (where / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (admin / "gitdir").write_text(str(where / ".git") + "\n", encoding="utf-8")
    return where


@pytest.fixture
def repository(tmp_path):
    main = _repo(tmp_path / "src" / "repo")
    (main / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    first = _worktree(main, tmp_path / "trees" / "repo-one", "repo-one")
    second = _worktree(main, tmp_path / "trees" / "repo-two", "repo-two")
    return main, first, second


def test_a_worktree_saves_to_its_repositorys_shelf_which_outlives_it(repository):
    main, first, second = repository
    assert paths.worktree_repository(first) == main

    _save(first, "from_a_worktree")
    shutil.rmtree(first)

    assert (_shelf(main) / "from_a_worktree.py").is_file()
    for cwd in (main, second):
        assert tbmod.discover(cwd)["from_a_worktree"].scope == tbmod.SHELF


def test_a_worktrees_own_toolbox_still_wins(repository):
    _, first, _ = repository
    own = first / ".js" / "toolbox"
    own.mkdir(parents=True)
    _save(first, "kept_in_tree")
    assert (own / "kept_in_tree.py").is_file()


def test_a_worktree_whose_repository_is_gone_uses_its_own_shelf(repository):
    main, first, _ = repository
    shutil.rmtree(main)

    assert paths.worktree_repository(first) is None
    assert not _save(first, "orphan").startswith("ERROR")
    assert (_shelf(first) / "orphan.py").is_file()


def test_a_main_checkout_and_a_submodule_are_not_worktrees(tmp_path):
    main = _repo(tmp_path / "plain")
    module_git = main / ".git" / "modules" / "sub"
    module_git.mkdir(parents=True)
    (module_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    sub = main / "sub"
    sub.mkdir()
    (sub / ".git").write_text(f"gitdir: {module_git}\n", encoding="utf-8")

    assert paths.worktree_repository(main) is None
    assert paths.worktree_repository(sub) is None
