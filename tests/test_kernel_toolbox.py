"""The two-tool mode: a persistent IPython kernel and the toolbox on top of it.

The kernel tests here start REAL kernels and run REAL code in them. They are
guarded by a skip only so a box without jupyter_client still reports a clean
suite; on a synced env they run for real, including killing a kernel mid-cell.
"""

from __future__ import annotations

import importlib.util
import io
import json
import threading
import time
from pathlib import Path

import pytest

from js import config as jsconfig, messages as msgs, paths, settings as jssettings
from js.toolkit import ToolContext, python_kernel, toolbox as tbmod
from js.toolkit.descriptions import render_tool_name_sections
from js.toolkit.registry import build_default_registry

HAVE_KERNEL = all(
    importlib.util.find_spec(name) is not None
    for name in ("jupyter_client", "ipykernel")
)
needs_kernel = pytest.mark.skipif(HAVE_KERNEL is False,
                                  reason="jupyter_client/ipykernel not installed")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """A ToolContext whose toolbox writes into tmp_path, never the real config dir."""
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    monkeypatch.setattr(paths, "global_toolbox_dir", lambda: tmp_path / "config" / "toolbox")
    context = ToolContext(cwd=work)
    context.model = "test-model"
    context.kernel_verbosity = "quiet"
    context.kernel_wait_seconds = 1
    yield context
    session = getattr(context, "kernel_session", None)
    if session is not None:
        session.shutdown()


def stderr_of(monkeypatch, fn):
    """Run fn with the rich console pointed at a buffer; return what it rendered."""
    buffer = io.StringIO()

    def fake_console():
        from rich.console import Console

        return Console(file=buffer, width=100, force_terminal=False,
                       color_system=None, highlight=False)

    from js import kernel_display

    monkeypatch.setattr(kernel_display, "_console", fake_console)
    result = fn()
    return result, buffer.getvalue()


# ---------------------------------------------------------------------------
# toolbox on disk — no kernel needed
# ---------------------------------------------------------------------------

# A tool file's provenance header: one comment line holding a JSON object.
HEADER_PREFIX = "# js-toolbox: "


def header_line(meta: dict) -> str:
    return HEADER_PREFIX + json.dumps(meta, separators=(",", ":"), sort_keys=True)


def saved_header(path: Path) -> dict:
    [line] = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(HEADER_PREFIX)]
    return json.loads(line[len(HEADER_PREFIX):])


def save_global(cwd: Path, name: str, body: str, **options) -> str:
    """A save to the top of the home toolbox, where `toolbox_dirs(cwd)[0]` is."""
    return tbmod.write_revision(cwd, name, body, scope="global", **options)


def project_toolbox(cwd: Path) -> Path:
    """The project's own `.js/toolbox`, made: local saves now go there."""
    directory = Path(cwd) / ".js" / "toolbox"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def archived_revisions(record) -> list[int]:
    """The revisions of `record` kept in its directory's .history."""
    archived = (record.path.parent / ".history").glob(f"{record.name}.r*.py")
    return sorted(int(path.name.removeprefix(f"{record.name}.r").removesuffix(".py")) for path in archived)


@pytest.mark.parametrize("definition", [
    "def sample():\n    return 1\n",
    "async def sample():\n    return 1\n",
    "class sample:\n    value = 1\n",
])
def test_history_source_fallback_keeps_decorators(definition, capsys):
    source = "@decorate\n" + definition
    namespace = {"In": [source]}
    exec(tbmod._SOURCE_PROBE % {"name": "sample"}, namespace)
    payload = json.loads(capsys.readouterr().out.removeprefix("__JS_SRC__"))

    def decorate(target):
        target.decorated = True
        return target

    restored = {"decorate": decorate}
    exec(payload["source"], restored)
    assert restored["sample"].decorated is True


@pytest.mark.parametrize("bad_entry", ["oops", None, 7, ["nested"]])
def test_malformed_history_entries_preserve_listing_and_valid_revisions(ctx, bad_entry):
    cwd = Path(ctx.cwd)
    directory = tbmod.toolbox_dirs(cwd)[0]
    directory.mkdir(parents=True)
    valid = {"revision": 1, "model": "first", "date": "2026-01-01", "note": "original"}
    original = header_line({"revision": 2, "history": [bad_entry, valid]})
    original += "\n\ndef sample():\n    return 1\n"
    target = directory / "sample.py"
    target.write_text(original, encoding="utf-8")
    save_global(cwd, "other", "def other():\n    return 2\n", model="other-model")

    listing = tbmod.describe(cwd)
    assert tbmod.discover(cwd)["sample"].history == [valid]
    assert "sample r2" in listing and "first" in listing
    assert "other r1" in listing
    assert "original" in tbmod.history_of(cwd, "sample")

    report = save_global(cwd, "sample", "def sample():\n    return 3\n", model="next")
    assert "sample r3" in report
    saved = saved_header(target)
    assert saved["history"][0] == valid
    assert [entry["model"] for entry in saved["history"]] == ["first", "next"]
    assert (directory / ".history" / "sample.r2.py").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("model", [5, ["a"], {"x": 1}])
def test_non_string_history_models_do_not_break_listing_or_saving(ctx, model):
    cwd = Path(ctx.cwd)
    directory = tbmod.toolbox_dirs(cwd)[0]
    directory.mkdir(parents=True)
    history = [{"revision": 1, "model": model}]
    target = directory / "sample.py"
    target.write_text(header_line({"revision": 1, "history": history})
                      + "\n\ndef sample():\n    return 1\n", encoding="utf-8")

    assert "sample r1 [global] unknown" in tbmod.describe(cwd)
    report = save_global(cwd, "sample", "def sample():\n    return 2\n", model="next")
    assert "authors: unknown -> next" in report
    assert tbmod.discover(cwd)["sample"].history[0] == history[0]
    assert "sample r2 [global] unknown -> next" in tbmod.describe(cwd)


def test_save_writes_a_parseable_provenance_header_with_model_note_and_revision(ctx):
    save_global(Path(ctx.cwd), "adder", "def adder(a, b):\n    return a + b\n",
                         model="qwen", note="first cut", stamp=time.time())

    path = tbmod.toolbox_dirs(Path(ctx.cwd))[0] / "adder.py"
    header = saved_header(path)

    assert header["name"] == "adder"
    assert header["revision"] == 1
    assert header["history"] == [
        {"revision": 1, "model": "qwen", "date": time.strftime("%Y-%m-%d"),
         "note": "first cut"}
    ]
    assert "def adder(a, b):" in path.read_text(encoding="utf-8")


def test_second_save_bumps_the_revision_and_archives_the_previous_body(ctx):
    cwd = Path(ctx.cwd)
    save_global(cwd, "adder", "def adder(a, b):\n    return a + b\n", model="qwen")
    report = save_global(cwd, "adder", "def adder(a, b):\n    return int(a) + int(b)\n",
                                  model="fable", note="coerce strings")

    directory = tbmod.toolbox_dirs(cwd)[0]
    archived = (directory / ".history" / "adder.r1.py").read_text(encoding="utf-8")
    current = (directory / "adder.py").read_text(encoding="utf-8")

    assert report.startswith("refined adder r2 [global]")
    assert "authors: qwen -> fable" in report
    assert "return a + b" in archived
    assert "return int(a) + int(b)" in current
    assert tbmod.discover(cwd)["adder"].revision == 2
    assert tbmod.discover(cwd)["adder"].authors == ["qwen", "fable"]


def test_recreating_deleted_tool_continues_above_archived_revisions(ctx):
    cwd = Path(ctx.cwd)
    save_global(cwd, "adder", "def adder():\n    return 1\n")
    save_global(cwd, "adder", "def adder():\n    return 2\n")
    directory = tbmod.toolbox_dirs(cwd)[0]
    archive = directory / ".history" / "adder.r1.py"
    original = archive.read_bytes()
    (directory / "adder.py").unlink()

    save_global(cwd, "adder", "def adder():\n    return 3\n")
    assert tbmod.discover(cwd)["adder"].revision == 2
    save_global(cwd, "adder", "def adder():\n    return 4\n")
    assert archive.read_bytes() == original
    assert "return 3" in (directory / ".history" / "adder.r2.py").read_text()


def test_revision_exceeds_archived_max_even_with_lower_live_header(ctx):
    cwd = Path(ctx.cwd)
    save_global(cwd, "adder", "def adder():\n    return 1\n")
    directory = tbmod.toolbox_dirs(cwd)[0]
    archive = directory / ".history" / "adder.r9.py"
    archive.parent.mkdir()
    archive.write_text("old revision nine")

    save_global(cwd, "adder", "def adder():\n    return 2\n")
    assert tbmod.discover(cwd)["adder"].revision == 10
    assert archive.read_text() == "old revision nine"


def test_revision_numbering_does_not_mix_global_and_project_archives(ctx):
    cwd = Path(ctx.cwd)
    project_dir = project_toolbox(cwd)
    for scope in ("global", "local"):
        tbmod.write_revision(cwd, "adder", "def adder():\n    return 1\n", scope=scope)
    global_dir = tbmod.toolbox_dirs(cwd)[0]
    archive = global_dir / ".history" / "adder.r9.py"
    archive.parent.mkdir()
    archive.write_text("global revision nine")

    tbmod.write_revision(cwd, "adder", "def adder():\n    return 2\n")
    assert saved_header(project_dir / "adder.py")["revision"] == 2
    assert archive.read_text() == "global revision nine"


def test_save_never_overwrites_an_existing_archive(ctx):
    cwd = Path(ctx.cwd)
    save_global(cwd, "adder", "def adder():\n    return 1\n")
    directory = tbmod.toolbox_dirs(cwd)[0]
    target = directory / "adder.py"
    original = target.read_bytes()
    save_global(cwd, "adder", "def adder():\n    return 2\n")
    archive = directory / ".history" / "adder.r1.py"
    target.write_bytes(original.replace(b"return 1", b"return 99"))
    before = target.read_bytes()

    report = save_global(cwd, "adder", "def adder():\n    return 3\n")
    assert report.startswith("ERROR")
    assert archive.read_bytes() == original
    assert target.read_bytes() == before


def test_restore_writes_the_old_body_as_a_new_revision_so_history_only_grows(ctx):
    cwd = Path(ctx.cwd)
    save_global(cwd, "adder", "def adder(a, b):\n    return a + b\n", model="qwen")
    save_global(cwd, "adder", "def adder(a, b):\n    return 0\n",
                         model="fable", note="broke it")

    report = tbmod.restore(cwd, "adder", 1, model="fable")

    record = tbmod.discover(cwd)["adder"]
    assert report.startswith("refined adder r3 [global]")
    assert record.revision == 3
    assert "return a + b" in record.path.read_text(encoding="utf-8")
    assert [entry["note"] for entry in record.history] == [
        "", "broke it", "rolled back to r1"]
    assert archived_revisions(record) == [1, 2]


def test_history_reports_every_revision_and_what_can_be_restored(ctx):
    cwd = Path(ctx.cwd)
    save_global(cwd, "adder", "def adder(a, b):\n    return a + b\n",
                         model="qwen", note="first cut")
    save_global(cwd, "adder", "def adder(a, b):\n    return b + a\n",
                         model="fable", note="commutative, obviously")

    report = tbmod.history_of(cwd, "adder")

    assert "adder r2 [global]" in report
    assert "r1 " in report and "qwen: first cut" in report
    assert "r2 " in report and "fable: commutative, obviously" in report
    assert report.strip().endswith("restorable: r1")


def test_discover_records_a_syntax_error_as_broken_and_still_indexes_the_rest(ctx):
    cwd = Path(ctx.cwd)
    directory = tbmod.toolbox_dirs(cwd)[0]
    directory.mkdir(parents=True)
    (directory / "good.py").write_text("def good():\n    return 1\n", encoding="utf-8")
    (directory / "bad.py").write_text("def bad(:\n    pass\n", encoding="utf-8")

    found = tbmod.discover(cwd)

    assert set(found) == {"good", "bad"}
    assert found["good"].error == ""
    assert "syntax error line 1" in found["bad"].error
    assert "BROKEN" in tbmod.describe(cwd)
    assert "good r1 [global]" in tbmod.describe(cwd)


def test_a_project_tool_shadows_a_global_tool_of_the_same_name(ctx):
    cwd = Path(ctx.cwd)
    global_dir, project_dir = tbmod.toolbox_dirs(cwd)[0], project_toolbox(cwd)
    for directory, body in ((global_dir, "return 'global'"), (project_dir, "return 'project'")):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "who.py").write_text(f"def who():\n    {body}\n", encoding="utf-8")

    record = tbmod.discover(cwd)["who"]

    assert record.scope == "project"
    assert "return 'project'" in record.path.read_text(encoding="utf-8")


def test_toolbox_rejects_a_body_that_does_not_parse_before_touching_disk(ctx):
    cwd = Path(ctx.cwd)
    tbmod.write_revision(cwd, "adder", "def adder():\n    return 1\n", model="qwen")

    report = tbmod.write_revision(cwd, "adder", "def adder(:\n    pass\n", model="fable")

    assert report.startswith("ERROR: 'adder' does not parse: line 1")
    assert "return 1" in tbmod.discover(cwd)["adder"].path.read_text(encoding="utf-8")
    assert tbmod.discover(cwd)["adder"].revision == 1


def test_the_shipped_example_is_seeded_once_and_an_edited_copy_is_left_alone(ctx):
    cwd = Path(ctx.cwd)

    first = tbmod.toolbox(action="list", context=ctx)
    path = tbmod.toolbox_dirs(cwd)[0] / "word_frequencies.py"

    assert "word_frequencies r1 [global] js" in first
    assert path.is_file()

    edited = "def word_frequencies(text, limit=1):\n    return [('mine', limit)]\n"
    path.write_text(edited, encoding="utf-8")
    tbmod.toolbox(action="list", context=ctx)

    assert path.read_text(encoding="utf-8") == edited


# ---------------------------------------------------------------------------
# degradation, capping, verbosity — no kernel needed
# ---------------------------------------------------------------------------


def test_a_missing_jupyter_package_becomes_one_error_naming_it(ctx, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: None if name == "jupyter_client" else object())

    result = python_kernel.kernel(code="1 + 1", context=ctx)

    assert result.startswith("ERROR: the kernel tool needs the jupyter_client package")
    assert "pyproject.toml" in result
    assert "just install" in result and "just sync" in result
    assert "Traceback" not in result


def test_save_without_a_kernel_says_to_pass_the_source_instead(ctx, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: None if name == "ipykernel" else object())

    result = tbmod.toolbox(action="save", name="adder", context=ctx)

    assert "ipykernel" in result
    assert "save needs the definition passed explicitly in `source`" in result


def test_toolbox_save_accepts_an_explicit_source_with_no_kernel_at_all(ctx, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    result = tbmod.toolbox(action="save", name="adder", note="by hand",
                           source="def adder(a, b):\n    return a + b\n", context=ctx)

    assert result.startswith("saved adder r1 [shelf]")
    assert tbmod.discover(Path(ctx.cwd))["adder"].history[0]["note"] == "by hand"


def test_toolbox_save_refuses_source_without_the_named_definition(ctx):
    cwd = Path(ctx.cwd)
    original = "def adder(a, b):\n    return a + b\n"
    tbmod.write_revision(cwd, "adder", original)
    path = tbmod.discover(cwd)["adder"].path
    before = path.read_bytes()

    result = tbmod.toolbox(action="save", name="adder",
                           source="def other():\n    return 1\n", context=ctx)

    assert path.read_bytes() == before
    assert result.startswith("ERROR")
    assert tbmod.discover(cwd)["adder"].revision == 1
    assert archived_revisions(tbmod.discover(cwd)["adder"]) == []


def test_model_facing_cap_uses_the_js_limit_knob_and_names_it_in_the_marker(ctx):
    ctx.max_tool_result_bytes = 200

    capped = python_kernel.cap_for_model("x" * 5000, ctx).model

    assert len(capped.encode("utf-8")) <= 200
    assert "[truncated: TOOL_RESULT_MAX (200) reached]" in capped


def test_model_facing_cap_of_zero_cuts_nothing(ctx):
    ctx.max_tool_result_bytes = 0
    text = "x" * 300_000

    assert python_kernel.cap_for_model(text, ctx) == text


def test_per_call_verbosity_beats_the_config_knob_which_beats_normal(ctx):
    ctx.kernel_verbosity = "quiet"

    assert python_kernel.resolve_verbosity(ctx, "verbose") == "verbose"
    assert python_kernel.resolve_verbosity(ctx, "") == "quiet"
    assert python_kernel.resolve_verbosity(ctx, "nonsense") == "quiet"
    ctx.kernel_verbosity = "bogus"
    assert python_kernel.resolve_verbosity(ctx, "") == "normal"


def test_verbose_splits_the_streams_that_normal_merges(ctx, monkeypatch):
    def render(level):
        _r, screen = stderr_of(monkeypatch, lambda: python_kernel.render_execution(
            ctx, level=level, code="x=1", stdout="OUT-TEXT\n", stderr="ERR-TEXT\n",
            display="DISPLAY-TEXT\n", error="", elapsed=0.1, cell=1, added=[],
            removed=[], namespace=["f"], images=[], interrupted=False))
        return screen

    verbose, normal = render("verbose"), render("normal")

    assert "stdout" in verbose and "stderr" in verbose and "display" in verbose
    for label in ("OUT-TEXT", "ERR-TEXT", "DISPLAY-TEXT"):
        assert label in verbose and label in normal
    assert "out " in normal


def test_quiet_stays_silent_on_success_and_still_reports_an_error(ctx, monkeypatch):
    def render(error):
        _r, screen = stderr_of(monkeypatch, lambda: python_kernel.render_execution(
            ctx, level="quiet", code="x=1", stdout="hello\n", stderr="",
            display="", error=error, elapsed=0.1, cell=3, added=[], removed=[],
            namespace=["f"], images=[], interrupted=False))
        return screen

    assert render("") == ""
    assert msgs.KERNEL_CELL.text(cell=3) in render("ZeroDivisionError: division by zero")
    assert "ZeroDivisionError" in render("ZeroDivisionError: division by zero")


def test_a_multi_line_event_renders_one_line_per_line(ctx, monkeypatch):
    ctx.kernel_verbosity = "normal"

    _r, screen = stderr_of(monkeypatch, lambda: python_kernel.render_event(
        ctx, "normal", "TOOLBOX\nalpha r1 [global] qwen\nbeta r2 [project] fable"))

    assert screen.count("· ") == 3
    assert "· beta r2 [project] fable" in screen


def test_verbose_shows_kernel_lifecycle_that_normal_keeps_off_the_screen(ctx, monkeypatch):
    def render(level):
        _r, screen = stderr_of(monkeypatch, lambda: python_kernel.render_event(
            ctx, level, "kernel started in /tmp/x", verbose_only=True))
        return screen

    assert "kernel started in /tmp/x" in render("verbose")
    assert render("normal") == ""


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_both_tools_are_registered_with_their_model_facing_descriptions():
    registry = build_default_registry()

    assert registry.resolve("kernel").name == "kernel"
    assert registry.resolve("toolbox").name == "toolbox"
    descriptions = Path(python_kernel.__file__).resolve().parents[2] / "defaults" / "tool_descriptions"
    for name in ("kernel", "toolbox"):
        expected = (descriptions / f"{name}.md").read_text().strip()
        assert registry.resolve(name).description == expected


def test_the_kernel_description_changes_when_toolbox_shares_the_surface():
    text = build_default_registry().resolve("kernel").description

    with_toolbox = render_tool_name_sections(text, {"kernel", "toolbox"}, tool="kernel")
    alone = render_tool_name_sections(text, {"kernel"}, tool="kernel")

    assert "action=load" in with_toolbox
    assert "action=load" not in alone


def test_the_kernel_description_names_the_interpreter_cells_run_in():
    text = build_default_registry().resolve("kernel").description

    assert "sys.executable" in text
    assert "uv pip install --python" in text


def test_kernel_verbosity_is_a_real_config_knob_with_a_default(tmp_path, monkeypatch):
    keys = {spec.key for spec in jssettings.REGISTRY}
    assert {"KERNEL_VERBOSITY", "KERNEL_LINES_MAX"} <= keys

    monkeypatch.setenv("JS_KERNEL_VERBOSITY", "verbose")
    cfg = jsconfig.from_env(save_session=False, cwd=tmp_path)
    assert cfg.kernel_verbosity == "verbose"
    assert cfg.kernel_render_max_lines == jssettings.default_value("KERNEL_LINES_MAX")

    # a value outside the three levels falls back rather than reaching the render
    monkeypatch.setenv("JS_KERNEL_VERBOSITY", "shouting")
    assert jsconfig.from_env(save_session=False, cwd=tmp_path).kernel_verbosity == "normal"


# ---------------------------------------------------------------------------
# real kernels
# ---------------------------------------------------------------------------


@needs_kernel
def test_state_survives_between_calls_and_the_namespace_line_lists_it(ctx):
    first = python_kernel.kernel(code="def triple(n):\n    return n * 3\n", context=ctx)
    second = python_kernel.kernel(code="triple(14)", context=ctx)

    assert "DEFINED triple(n)" in first
    assert "NAMESPACE triple" in first
    assert "42" in second
    assert "NAMESPACE triple" in second


@needs_kernel
def test_the_kernel_listens_on_unix_sockets_that_shutdown_removes(ctx):
    """TCP ports are handed out racily between processes; the kernel's
    sockets live in a private directory instead, one per session."""
    assert "42" in python_kernel.kernel(code="6 * 7", context=ctx)
    session = ctx.kernel_session
    socket_dir = session.socket_dir
    connection = json.loads(Path(session.manager.connection_file).read_text(encoding="utf-8"))

    assert connection["transport"] == "ipc"
    assert Path(connection["ip"]).parent == socket_dir
    assert socket_dir.stat().st_mode & 0o077 == 0

    session.shutdown()
    assert not socket_dir.exists()


@needs_kernel
def test_cells_run_in_the_interpreter_running_js(ctx):
    import sys

    result = python_kernel.kernel(code="import sys\nprint(sys.executable)", context=ctx)

    assert sys.executable in result


@needs_kernel
def test_a_cell_awaits_at_top_level_without_a_loop_patch_and_has_no_nest_asyncio(ctx):
    awaited = python_kernel.kernel(code=(
        "import asyncio\n"
        "async def ping():\n"
        "    return 'pong'\n"
        "print(await ping())\n"
    ), context=ctx)
    missing = python_kernel.kernel(code="import nest_asyncio", context=ctx)

    assert "pong" in awaited and "NAMESPACE ping" in awaited
    assert "ModuleNotFoundError" in missing and "nest_asyncio" in missing


@needs_kernel
def test_waiting_on_the_kernels_own_loop_never_completes_but_await_does(ctx):
    """A cell body runs on the loop, so blocking for that loop cannot finish.

    The coroutine here returns immediately, so a TimeoutError proves the wait
    itself is the defect rather than anything slow: the thread that would run
    the coroutine is the one the cell blocked. `await` runs the same coroutine.
    """
    ctx.kernel_wait_seconds = 30
    blocked = python_kernel.kernel(code=(
        "import asyncio\n"
        "loop = asyncio.get_running_loop()\n"
        "async def q():\n"
        "    return 7\n"
        "print(asyncio.run_coroutine_threadsafe(q(), loop).result(timeout=0.3))\n"
    ), context=ctx)
    awaited = python_kernel.kernel(code="print(await q())", context=ctx)

    assert "TimeoutError" in blocked
    assert "7" in awaited


@needs_kernel
def test_a_cell_blocked_on_the_kernels_own_loop_returns_a_handle_and_recovers(ctx):
    """The #101 wedge shape: a sync wrapper waiting on this kernel's own loop."""
    ctx.kernel_wait_seconds = 0.2
    python_kernel.kernel(code=(
        "import asyncio\n"
        "def blocked():\n"
        "    loop = asyncio.get_event_loop()\n"
        "    return asyncio.run_coroutine_threadsafe(asyncio.sleep(0.5), loop).result()\n"
    ), context=ctx)

    wedged = python_kernel.kernel(code="blocked()", context=ctx)

    assert wedged.splitlines()[-1].endswith(" RUNNING")
    refused = python_kernel.kernel(code="print('next')", context=ctx)
    assert "the previous cell is still running (blocked())" in refused

    stopped = python_kernel.kernel(action="interrupt", context=ctx)
    assert "KeyboardInterrupt" in stopped
    assert stopped.splitlines()[-1].startswith("NAMESPACE ")

    after = python_kernel.kernel(
        code="import asyncio\nawait asyncio.sleep(0.1)\nprint('loop answers')", context=ctx)

    assert "loop answers" in after


@needs_kernel
def test_a_saved_async_tool_that_closes_its_client_leaves_no_unclosed_warning(ctx):
    """The cleanup pattern the kernel and toolbox descriptions ask for."""
    source = (
        "import sys\n"
        "\n"
        "class _Client:\n"
        "    def __init__(self):\n"
        "        self.closed = False\n"
        "\n"
        "    async def ping(self):\n"
        "        return 'pong'\n"
        "\n"
        "    async def aclose(self):\n"
        "        self.closed = True\n"
        "\n"
        "    def __del__(self):\n"
        "        if not self.closed:\n"
        "            print('Unclosed client session', file=sys.stderr)\n"
        "\n"
        "async def ask():\n"
        "    client = _Client()\n"
        "    try:\n"
        "        return await client.ping()\n"
        "    finally:\n"
        "        await client.aclose()\n"
    )
    tbmod.toolbox(action="save", name="ask", note="closes its client",
                  source=source, context=ctx)
    tbmod.toolbox(action="load", context=ctx)

    closed = python_kernel.kernel(code="print(await ask())", context=ctx)
    leaked = python_kernel.kernel(code=(
        "async def leaky():\n"
        "    client = _Client()\n"
        "    await client.ping()\n"
        "    del client\n"
        "print(await leaky())\n"
    ), context=ctx)

    assert "pong" in closed and "Unclosed" not in closed
    assert "Unclosed client session" in leaked


@needs_kernel
def test_the_namespace_listing_is_rederived_from_the_live_kernel_each_call(ctx):
    python_kernel.kernel(code="def gone():\n    pass\ndef stays():\n    pass\n", context=ctx)

    after = python_kernel.kernel(code="del gone", context=ctx)

    assert "GONE gone" in after
    assert "NAMESPACE stays" in after
    assert ctx.kernel_session.namespace == {"stays": "stays()"}


@needs_kernel
def test_a_wait_that_runs_out_interrupts_the_cell_and_leaves_the_namespace_intact(ctx):
    python_kernel.kernel(code="def survivor():\n    return 'alive'\n", context=ctx)
    ctx.kernel_wait_seconds = 0.2
    python_kernel.kernel(code="import time\ntime.sleep(60)", context=ctx)

    started = time.monotonic()
    result = python_kernel.kernel(action="wait", timeout=0.5, context=ctx)
    elapsed = time.monotonic() - started

    assert result.startswith("INTERRUPTED after 0.5s.")
    assert "KeyboardInterrupt" in result
    assert elapsed < 30
    assert result.splitlines()[-1].startswith("NAMESPACE ")
    assert "'alive'" in python_kernel.kernel(code="survivor()", context=ctx)


@needs_kernel
def test_a_cell_that_outlasts_the_wait_returns_a_handle_instead_of_blocking(ctx):
    ctx.kernel_wait_seconds = 0.2

    started = time.monotonic()
    result = python_kernel.kernel(code="import time\ntime.sleep(60)", context=ctx)
    elapsed = time.monotonic() - started

    handle = result.splitlines()[-1].removeprefix("HANDLE ").removesuffix(" RUNNING")
    assert elapsed < 20
    assert "still running" in result
    assert f"cell 1 is still running after 0.2s (handle {handle})." in result
    assert f"HANDLE {handle} RUNNING" in python_kernel.kernel(action="poll", context=ctx)

    stopped = python_kernel.kernel(action="interrupt", context=ctx)

    assert "KeyboardInterrupt" in stopped
    assert stopped.splitlines()[-1].startswith("NAMESPACE ")
    assert "1" in python_kernel.kernel(code="print(1)", context=ctx)


@needs_kernel
def test_a_second_cell_is_refused_while_the_first_is_still_running(ctx):
    ctx.kernel_wait_seconds = 0.2
    first = python_kernel.kernel(code="import time\ntime.sleep(60)", context=ctx)
    assert "still running" in first

    refused = python_kernel.kernel(code="print('second')", context=ctx)

    assert "ERROR: the previous cell is still running (import time); interrupt it or wait" in refused
    assert refused.splitlines()[-1].endswith("RUNNING")
    python_kernel.kernel(action="interrupt", context=ctx)


@needs_kernel
def test_output_a_cell_produces_after_the_call_returned_arrives_on_the_next_poll(ctx):
    # The cell prints only once the test opens the gate, after the call returned.
    ctx.kernel_wait_seconds = 0.2
    first = python_kernel.kernel(code=(
        "import os, time\n"
        "while not os.path.exists('gate'):\n"
        "    time.sleep(0.01)\n"
        "print('late output')"
    ), context=ctx)
    assert first.splitlines()[-1].endswith(" RUNNING")
    (Path(ctx.cwd) / "gate").touch()

    polls: list[str] = []
    deadline = time.monotonic() + 30
    while not polls or polls[-1].splitlines()[-1].endswith(" RUNNING"):
        assert time.monotonic() < deadline, polls
        polls.append(python_kernel.kernel(action="poll", context=ctx))

    assert "late output" in "\n".join(polls)
    assert "finished" in polls[-1]
    assert polls[-1].splitlines()[-1].startswith("NAMESPACE ")


@needs_kernel
def test_a_cell_abandoned_by_a_cancelled_turn_leaves_the_kernel_idle(ctx):
    """The runtime's cancel path: SIGINT the cell, then the next call runs."""
    ctx.kernel_wait_seconds = 60
    outcome: list[str] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            python_kernel.kernel(code="import time\ntime.sleep(30)", context=ctx)))
    worker.start()
    for _ in range(400):
        if python_kernel.busy_handle(ctx.kernel_session) is not None:
            break
        time.sleep(0.05)
    assert python_kernel.busy_handle(ctx.kernel_session) is not None

    assert python_kernel.interrupt_inflight(ctx) is True

    worker.join(30)
    assert not worker.is_alive()
    started = time.monotonic()
    result = python_kernel.kernel(code="print(1)", context=ctx)
    assert time.monotonic() - started < 10
    assert "1" in result
    assert result.splitlines()[-1].startswith("NAMESPACE ")


@needs_kernel
def test_an_interrupt_sent_before_the_kernel_starts_the_cell_still_stops_it(ctx):
    """The cancel path can signal a cell that is sent but not yet executing.

    The first cell ignores SIGINT and runs until the test opens its gate, after
    the interrupt was sent, so the signal lands before the kernel starts the
    second cell, the one the interrupt was meant for.

    A resent SIGINT that lands after the kernel announces the cell but before
    its code runs is caught by ipykernel itself, and the cell is dropped with
    no error message. Either way the 30-second cell stops.
    """
    python_kernel.kernel(code="x = 1", context=ctx)
    session = ctx.kernel_session
    blocker = session.submit("import os, signal, time\n"
                             "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                             "print('ignoring', flush=True)\n"
                             "while not os.path.exists('gate'):\n"
                             "    time.sleep(0.01)")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any(
            m["header"]["msg_type"] == "stream" for m in blocker.messages):
        python_kernel.pump(session, time.monotonic() + 0.1)
    assert not blocker.finished
    target = session.submit("import time\ntime.sleep(30)")

    assert python_kernel.interrupt_inflight(ctx) is True
    (Path(ctx.cwd) / "gate").touch()

    started = time.monotonic()
    python_kernel.collect_until(session, target, started + 20)
    assert target.finished
    assert time.monotonic() - started < 15
    assert all(m["content"].get("ename") == "KeyboardInterrupt"
               for m in target.messages if m["header"]["msg_type"] == "error")


@needs_kernel
def test_a_kernel_that_dies_mid_cell_is_reported_not_waited_on(ctx):
    python_kernel.kernel(code="x = 1", context=ctx)

    started = time.monotonic()
    result = python_kernel.kernel(code="import os\nos._exit(1)", timeout=120, context=ctx)
    elapsed = time.monotonic() - started

    assert result.startswith("ERROR: the kernel died during execution (cell 2)")
    assert "restart=true" in result
    assert elapsed < 60


@needs_kernel
def test_restart_clears_the_namespace_and_says_so(ctx):
    python_kernel.kernel(code="def doomed():\n    pass\n", context=ctx)

    report = python_kernel.kernel(restart=True, context=ctx)

    assert "kernel restarted; the namespace is empty" in report
    assert report.splitlines()[-1] == "NAMESPACE (none)"
    assert python_kernel.kernel(code="", context=ctx) == "NAMESPACE (none)"


@needs_kernel
def test_image_output_lands_in_an_artifact_file_named_in_the_result(ctx):
    result = python_kernel.kernel(code=(
        "import base64\n"
        "from IPython.display import display\n"
        "png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptV"
        "AAAACklEQVR4nGNiAAAABgADNjd8qAAAAABJRU5ErkJggg==')\n"
        "display({'image/png': base64.b64encode(png).decode()}, raw=True)\n"
    ), context=ctx)

    line = [row for row in result.splitlines() if row.startswith("IMAGE ")][0]
    saved = Path(line.removeprefix("IMAGE "))
    assert saved.is_file()
    assert saved.read_bytes().startswith(b"\x89PNG")
    assert saved.parent.parent == paths.kernel_state_root()
    assert not (Path(ctx.cwd) / ".js").exists()


@needs_kernel
def test_the_kernels_own_stderr_goes_to_a_log_file_not_the_operators_screen(ctx, monkeypatch, tmp_path):
    # An IPython startup file writes to the kernel process's stderr while the
    # kernel boots, outside any cell.
    ipython_dir = tmp_path / "ipython"
    startup = ipython_dir / "profile_default" / "startup"
    startup.mkdir(parents=True)
    (startup / "00-mark.py").write_text("import os\nos.write(2, b'KERNEL-BOOT-MARK\\n')\n", encoding="utf-8")
    monkeypatch.setenv("IPYTHONDIR", str(ipython_dir))

    _result, screen = stderr_of(monkeypatch, lambda: python_kernel.kernel(
        code="1 + 1", verbosity="verbose", context=ctx))

    log = ctx.kernel_session.log_path.read_text(encoding="utf-8")
    assert ctx.kernel_session.log_path.parent.parent == paths.kernel_state_root()
    assert "KERNEL-BOOT-MARK" in log
    assert "KERNEL-BOOT-MARK" not in screen


@needs_kernel
def test_a_tool_saved_from_the_kernel_loads_into_a_fresh_one_with_its_imports(ctx):
    cwd = Path(ctx.cwd)
    python_kernel.kernel(code=(
        "import statistics\n"
        "def spread(values):\n"
        "    return statistics.fmean(values)\n"
    ), context=ctx)

    saved = tbmod.toolbox(action="save", name="spread", note="mean of a list", context=ctx)
    body = (tbmod.save_layer(cwd).directory / "spread.py").read_text(encoding="utf-8")

    ctx.kernel_session.shutdown()
    ctx.kernel_session = None
    assert "NameError" in python_kernel.kernel(code="spread([1, 2, 3])", context=ctx)

    loaded = tbmod.toolbox(action="load", context=ctx)
    used = python_kernel.kernel(code="spread([2, 4, 6])", context=ctx)

    assert "hoisted into the file: import statistics" in saved
    assert "import statistics" in body
    assert "TOOLBOX loaded spread r1 [shelf] test-model" in loaded
    assert "4.0" in used


@needs_kernel
def test_saved_function_carries_aliased_imported_classes_and_functions(ctx):
    python_kernel.kernel(code=(
        "from pathlib import Path as FilePath\n"
        "from dataclasses import replace as copy_with\n"
        "from os.path import basename\n"
        "def revised(record, path):\n"
        "    return copy_with(record, name=basename(FilePath(path)))\n"
    ), context=ctx)

    tbmod.toolbox(action="save", name="revised", context=ctx)
    directory = tbmod.save_layer(Path(ctx.cwd)).directory
    restored = {}
    body = (directory / "revised.py").read_text()
    assert "from pathlib import Path as FilePath" in body
    assert "from os.path import basename" in body
    exec(body, restored)

    from dataclasses import dataclass

    @dataclass
    class Record:
        name: str

    assert restored["revised"](Record("old"), "/tmp/new") == Record("new")


@needs_kernel
def test_dynamic_redefinition_never_saves_the_earlier_cell(ctx):
    python_kernel.kernel(code="def current(): return 1", context=ctx)
    tbmod.toolbox(action="save", name="current", context=ctx)
    python_kernel.kernel(code=(
        "from functools import cache, wraps\n"
        "exec('@cache\\n@wraps(current)\\ndef current(): return 2')"
    ), context=ctx)

    report = tbmod.toolbox(action="save", name="current", context=ctx)

    record = tbmod.discover(Path(ctx.cwd))["current"]
    assert report.startswith("ERROR") and "source" in report
    assert record.revision == 1
    assert archived_revisions(record) == []


@needs_kernel
def test_class_save_requires_the_source_of_the_live_class(ctx):
    python_kernel.kernel(code="class Thing: v = 1", context=ctx)
    tbmod.toolbox(action="save", name="Thing", context=ctx)
    record = tbmod.discover(Path(ctx.cwd))["Thing"]
    restored = {}
    exec(record.path.read_text(), restored)
    assert restored["Thing"].v == 1

    python_kernel.kernel(code="exec('class Thing: v = 2')", context=ctx)
    report = tbmod.toolbox(action="save", name="Thing", context=ctx)
    assert report.startswith("ERROR") and "source" in report
    assert tbmod.discover(Path(ctx.cwd))["Thing"].revision == 1


@needs_kernel
def test_load_reports_a_broken_tool_by_name_and_still_loads_the_healthy_ones(ctx):
    directory = tbmod.toolbox_dirs(Path(ctx.cwd))[0]
    directory.mkdir(parents=True)
    (directory / "healthy.py").write_text("def healthy():\n    return 'ok'\n", encoding="utf-8")
    (directory / "unparseable.py").write_text("def unparseable(:\n", encoding="utf-8")
    (directory / "exploding.py").write_text("raise RuntimeError('boom at import')\n",
                                            encoding="utf-8")

    report = tbmod.toolbox(action="load", context=ctx)

    assert "TOOLBOX loaded healthy r1 [global]" in report
    assert "TOOLBOX BROKEN unparseable: syntax error line 1" in report
    assert "TOOLBOX BROKEN exploding: RuntimeError: boom at import" in report
    assert "'ok'" in python_kernel.kernel(code="healthy()", context=ctx)


@needs_kernel
def test_a_tool_whose_module_level_calls_a_sibling_loads_whatever_its_name(ctx):
    """Alphabetical order decides nothing: the sibling may sort after the caller."""
    directory = tbmod.toolbox_dirs(Path(ctx.cwd))[0]
    directory.mkdir(parents=True)
    (directory / "leaf.py").write_text("def leaf(n):\n    return n + 1\n", encoding="utf-8")
    (directory / "amodulelevel.py").write_text("VALUE = leaf(3)\n", encoding="utf-8")
    (directory / "zsolo.py").write_text("SOLO = never_defined(1)\n", encoding="utf-8")

    report = tbmod.toolbox(action="load", context=ctx)

    assert "TOOLBOX loaded " in report
    assert "amodulelevel r1 [global]" in report
    assert "leaf r1 [global]" in report
    assert "TOOLBOX BROKEN amodulelevel" not in report
    assert "TOOLBOX BROKEN zsolo: NameError" in report
    assert "4" in python_kernel.kernel(code="VALUE", context=ctx)


@needs_kernel
def test_save_accepts_a_definition_that_calls_an_already_saved_tool(ctx):
    """Load execs every tool file into one namespace, so a sibling resolves."""
    python_kernel.kernel(code="def leaf(n):\n    return n * 2\n", context=ctx)
    tbmod.toolbox(action="save", name="leaf", note="doubles", context=ctx)
    python_kernel.kernel(code="def composite(n):\n    return leaf(n) + 1\n", context=ctx)

    report = tbmod.toolbox(action="save", name="composite", note="uses leaf", context=ctx)

    assert not report.startswith("ERROR")
    assert "composite" in report
    python_kernel.kernel(restart=True, context=ctx)
    tbmod.toolbox(action="load", context=ctx)
    assert "9" in python_kernel.kernel(code="print(composite(4))", context=ctx)


@needs_kernel
def test_save_refuses_a_definition_whose_free_name_lives_in_the_session(ctx):
    python_kernel.kernel(code=(
        "PREFIX = '>> '\n"
        "def shout(text):\n"
        "    return PREFIX + text.upper()\n"
    ), context=ctx)

    report = tbmod.toolbox(action="save", name="shout", note="loud", context=ctx)

    directory = tbmod.toolbox_dirs(Path(ctx.cwd))[0]
    assert report.startswith("ERROR: 'shout' uses PREFIX")
    assert not (directory / "shout.py").exists()
    assert not (directory / ".history").exists()


@needs_kernel
def test_a_refused_save_leaves_the_previous_revision_and_history_alone(ctx):
    cwd = Path(ctx.cwd)
    python_kernel.kernel(code=(
        "def shout(text):\n"
        "    return text\n"
        "PREFIX = '>> '\n"
    ), context=ctx)
    assert tbmod.toolbox(action="save", name="shout", note="plain", context=ctx).startswith(
        "saved shout r1")
    python_kernel.kernel(code="def shout(text):\n    return PREFIX + text\n", context=ctx)

    report = tbmod.toolbox(action="save", name="shout", note="loud", context=ctx)

    directory = tbmod.toolbox_dirs(cwd)[0]
    record = tbmod.discover(cwd)["shout"]
    assert report.startswith("ERROR: 'shout' uses PREFIX")
    assert record.revision == 1
    assert [entry["note"] for entry in record.history] == ["plain"]
    assert not (directory / ".history").exists()


def test_save_refuses_an_explicit_source_that_reads_an_undefined_name(ctx, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    report = tbmod.toolbox(action="save", name="over", source=(
        "def over(n):\n"
        "    return n > THRESHOLD\n"
    ), context=ctx)

    directory = tbmod.save_layer(Path(ctx.cwd)).directory
    assert report.startswith("ERROR: 'over' uses THRESHOLD")
    assert not (directory / "over.py").exists()


def test_an_explicit_source_with_its_own_imports_saves_cleanly(ctx, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    report = tbmod.toolbox(action="save", name="mean", source=(
        "import statistics\n"
        "\n"
        "def mean(values):\n"
        "    return statistics.fmean(values)\n"
    ), context=ctx)

    assert report.startswith("saved mean r1 [shelf]")
    assert "import statistics" in (tbmod.save_layer(Path(ctx.cwd)).directory / "mean.py").read_text()


@needs_kernel
def test_a_source_probe_never_lands_in_the_namespace_the_model_reads(ctx):
    python_kernel.kernel(code="def only():\n    return 1\n", context=ctx)
    tbmod.toolbox(action="save", name="only", context=ctx)

    report = python_kernel.kernel(code="", context=ctx)

    assert report == "NAMESPACE only"


@needs_kernel
def test_a_live_kernel_is_tracked_so_process_exit_can_shut_it_down(ctx):
    python_kernel.kernel(code="x = 1", context=ctx)
    session = ctx.kernel_session

    assert session in python_kernel._LIVE_SESSIONS
    assert session.alive()

    python_kernel._shutdown_live_kernels()

    assert session.alive() is False
    assert session not in python_kernel._LIVE_SESSIONS


@needs_kernel
def test_the_toolbox_load_probe_reports_json_the_kernel_actually_produced(ctx):
    directory = tbmod.toolbox_dirs(Path(ctx.cwd))[0]
    directory.mkdir(parents=True)
    (directory / "alpha.py").write_text("def alpha():\n    return 'a'\n", encoding="utf-8")
    session, error, _started = python_kernel.get_session(ctx)
    assert error == ""

    loaded, problems = tbmod.load_into_kernel(session, Path(ctx.cwd))

    assert loaded == ["alpha"]
    assert problems == []
    assert json.loads(json.dumps(loaded)) == ["alpha"]


@needs_kernel
def test_the_shipped_example_loads_and_calls_cleanly_in_a_fresh_kernel(ctx):
    report = tbmod.toolbox(action="load", context=ctx)

    result = python_kernel.kernel(code="print(word_frequencies('a b a c a', limit=2))", context=ctx)

    assert "word_frequencies r1 [global] js" in report
    assert "[('a', 3), ('b', 1)]" in result


def test_saving_to_global_archives_the_global_body_even_when_a_project_copy_shadows_it(ctx):
    """discover() lets project shadow global; save must not trust that view."""
    cwd = Path(ctx.cwd)
    global_dir, project_dir = tbmod.toolbox_dirs(cwd)[0], project_toolbox(cwd)
    tbmod.write_revision(cwd, "summarise", "def summarise():\n    return 'g1'\n",
                         model="qwen", scope="global")
    tbmod.write_revision(cwd, "summarise", "def summarise():\n    return 'p1'\n",
                         model="fable", scope="local")

    report = tbmod.write_revision(cwd, "summarise", "def summarise():\n    return 'g2'\n",
                                  model="opus", scope="global")

    archived = global_dir / ".history" / "summarise.r1.py"
    assert report.startswith("refined summarise r2 [global]")
    assert "authors: qwen -> opus" in report
    assert archived.is_file()
    assert "return 'g1'" in archived.read_text(encoding="utf-8")
    assert "return 'g2'" in (global_dir / "summarise.py").read_text(encoding="utf-8")
    assert "return 'p1'" in (project_dir / "summarise.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# footer contract, offline (a stub kernel session, no kernel process)
# ---------------------------------------------------------------------------


class _OfflineKernel:
    """KernelSession stand-in that serves a canned namespace probe."""

    def __init__(self) -> None:
        self.cwd = Path("/tmp")
        self.executions = 0
        self.handles: dict[str, python_kernel.CellHandle] = {}
        self.current = ""
        self.last = ""
        self.namespace: dict[str, str] = {}
        self.visible_names: set[str] = set()

    def alive(self) -> bool:
        return True

    def restart(self) -> None:
        self.executions = 0
        self.namespace = {}
        self.visible_names = set()


def _offline_kernel(monkeypatch, output=None, names=(), callables=None, alive=True):
    session = _OfflineKernel()
    probe = {"callables": dict(callables or {}), "names": list(names)}

    def fake_run_cell(sess, code, timeout=120, *, store_history=True, marker=""):
        if "__js_probe" in code:
            return python_kernel.CellOutput(marker=json.dumps(probe))
        return output if output is not None else python_kernel.CellOutput()

    def fake_submit_cell(sess, code, wait, *, label=""):
        result = output if output is not None else python_kernel.CellOutput()
        handle = python_kernel.CellHandle(id=label or "1", msg_id="offline", code=code,
                                 started=time.monotonic())
        handle.died = result.died
        handle.finished = not result.died
        handle.elapsed = result.elapsed
        return handle, result

    monkeypatch.setattr(python_kernel, "get_session", lambda context: (session, "", False))
    monkeypatch.setattr(python_kernel, "run_cell", fake_run_cell)
    monkeypatch.setattr(python_kernel, "submit_cell", fake_submit_cell)
    if not alive:
        session.alive = lambda: False  # type: ignore[method-assign]
    return session


def test_kernel_footer_survives_a_died_kernel(tmp_path, monkeypatch):
    session = _offline_kernel(
        monkeypatch, python_kernel.CellOutput(died=True), names=["x"], alive=False
    )
    context = ToolContext(cwd=tmp_path, kernel_verbosity="quiet")

    result = python_kernel.kernel(code="import os\nos._exit(1)", context=context)

    assert result.startswith("ERROR: the kernel died during execution (cell 1)")
    assert result.splitlines()[-1] == "NAMESPACE (none)"
    assert session.namespace == {}


def test_the_kernel_panel_draws_plain_on_a_stderr_that_is_not_a_terminal(ctx, monkeypatch):
    import sys

    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    python_kernel.render_event(ctx, "normal", "toolbox saved")
    assert "toolbox saved" in stream.getvalue() and "\x1b" not in stream.getvalue()


def test_an_agent_listing_kernel_and_toolbox_gets_them_with_default_settings(monkeypatch):
    from types import SimpleNamespace

    from js import run_assembly
    from js.toolkit import policy

    seen = []
    monkeypatch.setattr(policy.output, "notice", lambda message, **_fields: seen.append(message))
    surface = run_assembly.registry_for(SimpleNamespace(prompt_roots=())).select(["kernel:eager", "toolbox:eager"])

    assert {"kernel", "toolbox"} <= set(surface.by_name)
    assert seen == []


@needs_kernel
def test_toolbox_load_and_save_refuse_while_a_cell_runs_and_leave_it_running(ctx):
    ctx.kernel_wait_seconds = 0.2
    python_kernel.kernel(code="def keep():\n    return 1\n", context=ctx)
    python_kernel.kernel(code="import time\ntime.sleep(3)\nprint('cell done')", context=ctx)

    started = time.monotonic()
    load = tbmod.toolbox(action="load", context=ctx)
    save = tbmod.toolbox(action="save", name="keep", note="n", context=ctx)

    assert time.monotonic() - started < 2
    assert load.startswith("ERROR") and load.splitlines()[-1].endswith("RUNNING")
    assert save.startswith("ERROR") and save.splitlines()[-1].endswith("RUNNING")
    finished = python_kernel.kernel(action="wait", timeout=30, context=ctx)
    assert "cell done" in finished
    assert "KeyboardInterrupt" not in finished


@needs_kernel
def test_output_over_the_cap_keeps_the_traceback_and_the_namespace_line(ctx):
    ctx.max_tool_result_bytes = 4000
    result = python_kernel.kernel(
        code="for i in range(3000): print('row', i)\nraise ValueError('the real error')",
        context=ctx,
    )

    model = result.model
    assert len(model.encode()) <= 4000
    assert "row 0" in model and "row 2999" in model
    assert "ValueError: the real error" in model
    assert model.splitlines()[-1].startswith("NAMESPACE ")


@needs_kernel
def test_a_call_after_the_kernel_died_starts_a_new_one_that_records_why(ctx):
    python_kernel.kernel(code="x = 1", context=ctx)
    ctx.kernel_session.manager.provisioner.process.kill()
    ctx.kernel_session.manager.provisioner.process.wait()

    result = python_kernel.kernel(code="print('fresh')", context=ctx)

    assert ctx.kernel_session.replaced == "died"
    assert "fresh" in result


@needs_kernel
def test_save_accepts_match_captures_and_calls_through_tools(ctx):
    python_kernel.kernel(code=(
        "def head(value):\n"
        "    match value:\n"
        "        case [first, *rest]:\n"
        "            return first, rest\n"
        "        case {'key': found, **others}:\n"
        "            return found, others\n"
        "    return tools.names()\n"
    ), context=ctx)

    report = tbmod.toolbox(action="save", name="head", note="n", context=ctx)

    assert not report.startswith("ERROR")


def test_type_parameters_bind_their_names():
    assert tbmod.unbound_names("def first[T](items: list[T]) -> T:\n    return items[0]\n") == []


def test_prune_keeps_a_finished_handle_whose_output_was_never_read():
    session = python_kernel.KernelSession(cwd=Path("."), artifacts=Path("."))
    for index in range(python_kernel.KEEP_FINISHED_HANDLES + 3):
        handle = python_kernel.CellHandle(id=str(index), msg_id=str(index), code="",
                                          started=float(index), finished=True)
        handle.messages = [{}]
        handle.delivered = 0 if index == 0 else 1
        session.handles[handle.id] = handle

    session.prune()

    assert "0" in session.handles
