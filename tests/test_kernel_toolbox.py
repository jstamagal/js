"""The two-tool mode: a persistent IPython kernel and the toolbox on top of it.

The kernel tests here start REAL kernels and run REAL code in them. They are
guarded by a skip only so a box without jupyter_client still reports a clean
suite; on a synced env they run for real, including killing a kernel mid-cell.
"""

from __future__ import annotations

import importlib.util
import io
import json
import time
from pathlib import Path

import pytest

from js import config as jsconfig
from js import settings as jssettings
from js.toolkit import ToolContext
from js.toolkit import kernel as kmod
from js.toolkit import toolbox as tbmod
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
    work.mkdir()
    monkeypatch.setattr(tbmod._paths, "config_dir", lambda: tmp_path / "config")
    context = ToolContext(cwd=work)
    context.model = "test-model"
    context.kernel_verbosity = "quiet"
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

    monkeypatch.setattr(kmod, "_console", fake_console)
    result = fn()
    return result, buffer.getvalue()


# ---------------------------------------------------------------------------
# toolbox on disk — no kernel needed
# ---------------------------------------------------------------------------


def test_save_writes_a_parseable_provenance_header_with_model_note_and_revision(ctx):
    tbmod.write_revision(Path(ctx.cwd), "adder", "def adder(a, b):\n    return a + b\n",
                         model="qwen", note="first cut", stamp=time.time())

    path = tbmod.toolbox_dirs(Path(ctx.cwd))[0] / "adder.py"
    header = tbmod._parse_header(path.read_text(encoding="utf-8"))

    assert header["name"] == "adder"
    assert header["revision"] == 1
    assert header["history"] == [
        {"revision": 1, "model": "qwen", "date": time.strftime("%Y-%m-%d"),
         "note": "first cut"}
    ]
    assert "def adder(a, b):" in path.read_text(encoding="utf-8")


def test_second_save_bumps_the_revision_and_archives_the_previous_body(ctx):
    cwd = Path(ctx.cwd)
    tbmod.write_revision(cwd, "adder", "def adder(a, b):\n    return a + b\n", model="qwen")
    report = tbmod.write_revision(cwd, "adder", "def adder(a, b):\n    return int(a) + int(b)\n",
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


def test_restore_writes_the_old_body_as_a_new_revision_so_history_only_grows(ctx):
    cwd = Path(ctx.cwd)
    tbmod.write_revision(cwd, "adder", "def adder(a, b):\n    return a + b\n", model="qwen")
    tbmod.write_revision(cwd, "adder", "def adder(a, b):\n    return 0\n",
                         model="fable", note="broke it")

    report = tbmod.restore(cwd, "adder", 1, model="fable")

    record = tbmod.discover(cwd)["adder"]
    assert report.startswith("refined adder r3 [global]")
    assert record.revision == 3
    assert "return a + b" in record.path.read_text(encoding="utf-8")
    assert [entry["note"] for entry in record.history] == [
        "", "broke it", "rolled back to r1"]
    assert tbmod._archived_revisions(record) == [1, 2]


def test_history_reports_every_revision_and_what_can_be_restored(ctx):
    cwd = Path(ctx.cwd)
    tbmod.write_revision(cwd, "adder", "def adder(a, b):\n    return a + b\n",
                         model="qwen", note="first cut")
    tbmod.write_revision(cwd, "adder", "def adder(a, b):\n    return b + a\n",
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
    global_dir, project_dir = tbmod.toolbox_dirs(cwd)
    for directory, body in ((global_dir, "return 'global'"), (project_dir, "return 'project'")):
        directory.mkdir(parents=True)
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


# ---------------------------------------------------------------------------
# degradation, capping, verbosity — no kernel needed
# ---------------------------------------------------------------------------


def test_a_missing_jupyter_package_becomes_one_error_naming_it(ctx, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: None if name == "jupyter_client" else object())

    result = kmod.kernel(code="1 + 1", context=ctx)

    assert result.startswith("ERROR: the kernel tool needs the jupyter_client package")
    assert "pyproject.toml" in result and "just sync" in result
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

    assert result.startswith("saved adder r1 [global]")
    assert tbmod.discover(Path(ctx.cwd))["adder"].history[0]["note"] == "by hand"


def test_model_facing_cap_uses_the_js_limit_knob_and_names_it_in_the_marker(ctx):
    ctx.max_tool_result_bytes = 200

    capped = kmod.cap_for_model("x" * 5000, ctx)

    assert len(capped.encode("utf-8")) <= 200
    assert "[truncated: limits.max_tool_result_bytes (200) reached]" in capped


def test_per_call_verbosity_beats_the_config_knob_which_beats_normal(ctx):
    ctx.kernel_verbosity = "quiet"

    assert kmod.resolve_verbosity(ctx, "verbose") == "verbose"
    assert kmod.resolve_verbosity(ctx, "") == "quiet"
    assert kmod.resolve_verbosity(ctx, "nonsense") == "quiet"
    ctx.kernel_verbosity = "bogus"
    assert kmod.resolve_verbosity(ctx, "") == "normal"


def test_the_render_clips_long_output_and_says_how_many_lines_it_hid(ctx, monkeypatch):
    ctx.kernel_render_max_lines = 5

    _result, screen = stderr_of(monkeypatch, lambda: kmod.render_execution(
        ctx, level="normal", code="print(1)",
        stdout="\n".join(f"line {i}" for i in range(40)),
        stderr="", display="", error="", elapsed=0.1, cell=1,
        added=[], removed=[], namespace=["f"], images=[], interrupted=False))

    assert "line 4" in screen
    assert "line 5" not in screen
    assert "... 35 more lines (full text went to the model)" in screen


def test_verbose_splits_the_streams_that_normal_merges(ctx, monkeypatch):
    def render(level):
        _r, screen = stderr_of(monkeypatch, lambda: kmod.render_execution(
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
        _r, screen = stderr_of(monkeypatch, lambda: kmod.render_execution(
            ctx, level="quiet", code="x=1", stdout="hello\n", stderr="",
            display="", error=error, elapsed=0.1, cell=3, added=[], removed=[],
            namespace=["f"], images=[], interrupted=False))
        return screen

    assert render("") == ""
    assert "kernel[3] ERROR" in render("ZeroDivisionError: division by zero")
    assert "ZeroDivisionError" in render("ZeroDivisionError: division by zero")


def test_a_multi_line_event_renders_one_line_per_line(ctx, monkeypatch):
    ctx.kernel_verbosity = "normal"

    _r, screen = stderr_of(monkeypatch, lambda: kmod.render_event(
        ctx, "normal", "TOOLBOX\nalpha r1 [global] qwen\nbeta r2 [project] fable"))

    assert screen.count("· ") == 3
    assert "· beta r2 [project] fable" in screen


def test_verbose_shows_kernel_lifecycle_that_normal_keeps_off_the_screen(ctx, monkeypatch):
    def render(level):
        _r, screen = stderr_of(monkeypatch, lambda: kmod.render_event(
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
    assert "persistent IPython kernel" in registry.resolve("kernel").description
    assert "outlive the session" in registry.resolve("toolbox").description


def test_the_kernel_description_changes_when_toolbox_shares_the_surface():
    text = build_default_registry().resolve("kernel").description

    with_toolbox = render_tool_name_sections(text, {"kernel", "toolbox"}, tool="kernel")
    alone = render_tool_name_sections(text, {"kernel"}, tool="kernel")

    assert "toolbox action=load" in with_toolbox
    assert "Nothing here survives the session." in alone
    assert "Nothing here survives the session." not in with_toolbox
    assert "toolbox action=load" not in alone


def test_the_twotool_agent_ships_with_exactly_kernel_toolbox_and_shell():
    import yaml

    root = Path(__file__).resolve().parents[1]
    spec = yaml.safe_load((root / "prompts" / "twotool" / "00-tools.yaml").read_text())

    assert spec["tools"] == ["kernel", "toolbox", "shell"]
    assert (root / "prompts" / "twotool" / "01-prompt.md").read_text().strip()


def test_kernel_verbosity_is_a_real_config_knob_with_a_default(tmp_path, monkeypatch):
    keys = {spec.key for spec in jssettings.REGISTRY}
    assert {"kernel.verbosity", "kernel.render_max_lines"} <= keys

    monkeypatch.setenv("JS_KERNEL_VERBOSITY", "verbose")
    cfg = jsconfig.from_env(save_session=False, cwd=tmp_path)
    assert cfg.kernel_verbosity == "verbose"
    assert cfg.kernel_render_max_lines == jssettings.DEFAULT_KERNEL_RENDER_MAX_LINES

    # a value outside the three levels falls back rather than reaching the render
    monkeypatch.setenv("JS_KERNEL_VERBOSITY", "shouting")
    assert jsconfig.from_env(save_session=False, cwd=tmp_path).kernel_verbosity == "normal"


# ---------------------------------------------------------------------------
# real kernels
# ---------------------------------------------------------------------------


@needs_kernel
def test_state_survives_between_calls_and_the_namespace_line_lists_it(ctx):
    first = kmod.kernel(code="def triple(n):\n    return n * 3\n", context=ctx)
    second = kmod.kernel(code="triple(14)", context=ctx)

    assert "DEFINED triple(n)" in first
    assert "NAMESPACE triple" in first
    assert "42" in second
    assert "NAMESPACE triple" in second


@needs_kernel
def test_the_namespace_listing_is_rederived_from_the_live_kernel_each_call(ctx):
    kmod.kernel(code="def gone():\n    pass\ndef stays():\n    pass\n", context=ctx)

    after = kmod.kernel(code="del gone", context=ctx)

    assert "GONE gone" in after
    assert "NAMESPACE stays" in after
    assert ctx.kernel_session.namespace == {"stays": "stays()"}


@needs_kernel
def test_a_timeout_interrupts_the_cell_and_leaves_the_namespace_intact(ctx):
    kmod.kernel(code="def survivor():\n    return 'alive'\n", context=ctx)

    started = time.monotonic()
    result = kmod.kernel(code="import time\ntime.sleep(60)", timeout=3, context=ctx)
    elapsed = time.monotonic() - started

    assert result.startswith("INTERRUPTED after 3s.")
    assert "KeyboardInterrupt" in result
    assert elapsed < 30
    assert "'alive'" in kmod.kernel(code="survivor()", context=ctx)


@needs_kernel
def test_a_kernel_that_dies_mid_cell_is_reported_not_waited_on(ctx):
    kmod.kernel(code="x = 1", context=ctx)

    started = time.monotonic()
    result = kmod.kernel(code="import os\nos._exit(1)", timeout=120, context=ctx)
    elapsed = time.monotonic() - started

    assert result.startswith("ERROR: the kernel died during execution (cell 2)")
    assert "restart=true" in result
    assert elapsed < 60


@needs_kernel
def test_restart_clears_the_namespace_and_says_so(ctx):
    kmod.kernel(code="def doomed():\n    pass\n", context=ctx)

    report = kmod.kernel(restart=True, context=ctx)

    assert report == "kernel restarted; the namespace is empty"
    assert kmod.kernel(code="", context=ctx) == "NAMESPACE (none)"


@needs_kernel
def test_image_output_lands_in_an_artifact_file_named_in_the_result(ctx):
    result = kmod.kernel(code=(
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
    assert saved.parent == Path(ctx.cwd) / ".js" / "kernel"


@needs_kernel
def test_the_kernels_own_stderr_goes_to_a_log_file_not_the_operators_screen(ctx, monkeypatch):
    _result, screen = stderr_of(monkeypatch, lambda: kmod.kernel(
        code="1 + 1", verbosity="verbose", context=ctx))

    log = (Path(ctx.cwd) / ".js" / "kernel" / "kernel.log").read_text(encoding="utf-8")
    assert "IPKernelApp" in log
    assert "IPKernelApp" not in screen


@needs_kernel
def test_a_tool_saved_from_the_kernel_loads_into_a_fresh_one_with_its_imports(ctx):
    cwd = Path(ctx.cwd)
    kmod.kernel(code=(
        "import statistics\n"
        "def spread(values):\n"
        "    return statistics.fmean(values)\n"
    ), context=ctx)

    saved = tbmod.toolbox(action="save", name="spread", note="mean of a list", context=ctx)
    body = (tbmod.toolbox_dirs(cwd)[0] / "spread.py").read_text(encoding="utf-8")

    ctx.kernel_session.shutdown()
    ctx.kernel_session = None
    assert "NameError" in kmod.kernel(code="spread([1, 2, 3])", context=ctx)

    loaded = tbmod.toolbox(action="load", context=ctx)
    used = kmod.kernel(code="spread([2, 4, 6])", context=ctx)

    assert "hoisted into the file: import statistics" in saved
    assert "import statistics" in body
    assert "TOOLBOX loaded spread r1 [global] test-model" in loaded
    assert "4.0" in used


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
    assert "'ok'" in kmod.kernel(code="healthy()", context=ctx)


@needs_kernel
def test_save_warns_about_session_names_it_could_not_put_in_the_file(ctx):
    kmod.kernel(code=(
        "PREFIX = '>> '\n"
        "def shout(text):\n"
        "    return PREFIX + text.upper()\n"
    ), context=ctx)

    report = tbmod.toolbox(action="save", name="shout", note="loud", context=ctx)

    assert "saved shout r1 [global]" in report
    assert "WARNING this definition also uses PREFIX" in report
    assert "will NameError when a later session loads it" in report


@needs_kernel
def test_a_source_probe_never_lands_in_the_namespace_the_model_reads(ctx):
    kmod.kernel(code="def only():\n    return 1\n", context=ctx)
    tbmod.toolbox(action="save", name="only", context=ctx)

    report = kmod.kernel(code="", context=ctx)

    assert report == "NAMESPACE only"


@needs_kernel
def test_a_live_kernel_is_tracked_so_process_exit_can_shut_it_down(ctx):
    kmod.kernel(code="x = 1", context=ctx)
    session = ctx.kernel_session

    assert session in kmod._LIVE_SESSIONS
    assert session.alive()

    kmod._shutdown_live_kernels()

    assert session.alive() is False
    assert session not in kmod._LIVE_SESSIONS


@needs_kernel
def test_the_toolbox_load_probe_reports_json_the_kernel_actually_produced(ctx):
    directory = tbmod.toolbox_dirs(Path(ctx.cwd))[0]
    directory.mkdir(parents=True)
    (directory / "alpha.py").write_text("def alpha():\n    return 'a'\n", encoding="utf-8")
    session, error, _started = kmod.get_session(ctx)
    assert error == ""

    loaded, problems = tbmod.load_into_kernel(session, Path(ctx.cwd))

    assert loaded == ["alpha"]
    assert problems == []
    assert json.loads(json.dumps(loaded)) == ["alpha"]
