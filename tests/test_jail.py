"""`js -C DIR` keeps the agent in DIR: command tools run under bubblewrap with
the home hidden, file tools refuse paths outside DIR and the bound paths."""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from js import cli, jail, paths, persona, runtime, settings
from js.toolkit import ToolContext, call_tool, kernel as kmod, process_net, terminal
from js.toolkit.registry import build_default_registry

from test_subagent_isolation import _fake_stream_result, make_cfg, prompt_dir

needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
needs_kernel = pytest.mark.skipif(
    not all(importlib.util.find_spec(name) for name in ("jupyter_client", "ipykernel")),
    reason="jupyter_client/ipykernel not installed",
)

SECRET = "operator-secret-7731"


@pytest.fixture
def operator_home(tmp_path):
    """The test HOME (conftest points HOME at tmp_path) with operator files."""
    home = Path(os.environ["HOME"])
    (home / ".zshrc").write_text(f"export TOKEN={SECRET}\n")
    (home / "notes.txt").write_text(SECRET)
    return home


@pytest.fixture
def jailed(operator_home, tmp_path, monkeypatch):
    """A jail at tmp_path/foo holding one file, and a ToolContext working there."""
    root = tmp_path / "foo"
    root.mkdir()
    (root / "inside.txt").write_text("inside\n")
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = ()
    return context


def run_shell(command: str, context: ToolContext) -> tuple[int, str]:
    result = process_net.shell(command, context=context)
    lines = result.splitlines()
    code = int(next(line for line in lines if line.startswith("exit=")).split("=", 1)[1])
    return code, result


def tool(name: str, context: ToolContext, **args) -> str:
    registry = build_default_registry()
    return call_tool(registry.resolve(name), args, context)


@needs_bwrap
def test_ls_home_shows_no_operator_files(jailed):
    code, result = run_shell("ls -A ~", jailed)

    assert code == 0
    assert ".zshrc" not in result
    assert "notes.txt" not in result


@needs_bwrap
def test_find_finds_no_zshrc_under_home(jailed):
    code, result = run_shell('find /home "$HOME" -name .zshrc 2>/dev/null; echo END', jailed)

    assert code == 0
    output = result.split("--- stdout ---\n", 1)[1]
    assert output.strip() == "END"


def test_other_views_of_a_hidden_home_are_hidden():
    # The home is a btrfs subvolume; the whole filesystem is mounted too.
    mounts = [
        jail._Mount("0:1", Path("/"), Path("/"), "ext4"),
        jail._Mount("0:32", Path("/"), Path("/mnt/pool"), "btrfs"),
        jail._Mount("0:32", Path("/backup/home/op"), Path("/home/op"), "btrfs"),
    ]

    assert jail._other_views(Path("/home/op"), mounts) == [Path("/mnt/pool/backup/home/op")]


@needs_bwrap
def test_read_of_zshrc_is_refused_with_one_line(jailed):
    result = tool("read", jailed, path="~/.zshrc")

    assert result.startswith("ERROR:")
    assert len(result.splitlines()) == 1
    assert SECRET not in result
    assert "inside" in tool("read", jailed, path="inside.txt")


@needs_bwrap
def test_file_tools_refuse_writes_outside_the_jail(jailed, operator_home):
    result = tool("write", jailed, path=str(operator_home / "planted.txt"), content="x")

    assert result.startswith("ERROR:")
    assert not (operator_home / "planted.txt").exists()
    assert not tool("write", jailed, path="made.txt", content="ok\n").startswith("ERROR")
    assert (jailed.cwd / "made.txt").read_text() == "ok\n"


@needs_bwrap
def test_fs_search_refuses_the_home(jailed):
    result = tool("fs_search", jailed, pattern=SECRET, path="~")

    assert result.startswith("ERROR:")
    assert SECRET not in result


@needs_bwrap
def test_parallel_read_only_calls_stay_in_the_jail(jailed):
    """One batch of read-only calls runs at once; each is still confined."""
    calls = [
        runtime._PendingToolCall("in", "read", ['{"file_path": "inside.txt"}']),
        runtime._PendingToolCall("home", "read", ['{"file_path": "~/.zshrc"}']),
        runtime._PendingToolCall("grep", "fs_search", ['{"pattern": "inside", "output_mode": "content"}']),
        runtime._PendingToolCall("grep_home", "fs_search", [f'{{"pattern": "{SECRET}", "path": "~"}}']),
    ]
    jailed.max_parallel_tools = 8

    records = runtime._dispatch_tool_calls(
        calls, runtime.Telemetry(None), 65536, False, runtime.ToolErrorTracker(),
        build_default_registry(), jailed,
    )

    results = {pc.id: result for pc, _args, result in records}
    assert "inside" in results["in"] and not results["in"].startswith("ERROR")
    assert "inside" in results["grep"] and not results["grep"].startswith("ERROR")
    for refused in ("home", "grep_home"):
        assert results[refused].startswith("ERROR:")
        assert SECRET not in results[refused]


@needs_bwrap
def test_path_directories_under_home_run(jailed, operator_home, monkeypatch):
    bin_dir = operator_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    hello = bin_dir / "hello-from-home"
    hello.write_text("#!/bin/sh\necho hello-ran\n")
    hello.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    code, result = run_shell("hello-from-home", jailed)

    assert code == 0
    assert "hello-ran" in result
    for name in ("rg", "uv", "cargo"):
        if shutil.which(name):
            code, result = run_shell(f"{name} --version", jailed)
            assert code == 0, result


@needs_bwrap
def test_path_directories_under_host_tmp_run(jailed, monkeypatch):
    # The jail's /tmp is private; a PATH directory (or the kernel's
    # interpreter) under the host /tmp is bound back read-only.
    bin_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        hello = bin_dir / "hello-from-tmp"
        hello.write_text("#!/bin/sh\necho tmp-ran\n")
        hello.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        code, result = run_shell("hello-from-tmp", jailed)
    finally:
        shutil.rmtree(bin_dir)

    assert code == 0
    assert "tmp-ran" in result


@needs_bwrap
def test_tmp_on_path_keeps_the_private_tmp(jailed, monkeypatch):
    marker = Path(tempfile.mkstemp(dir="/tmp")[1])
    try:
        monkeypatch.setenv("PATH", f"/tmp{os.pathsep}{os.environ['PATH']}")

        _, result = run_shell(f"cat {marker}; echo x > /tmp/private && echo WROTE", jailed)
    finally:
        marker.unlink()

    assert "No such file" in result
    assert "WROTE" in result


@needs_bwrap
def test_network_stays_on(jailed, tmp_path):
    client = shutil.which("curl")
    python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else None
    if client is None and python is None:
        pytest.skip("neither curl nor /usr/bin/python3 to make a request with")
    served = tmp_path / "served"
    served.mkdir()
    (served / "page.txt").write_text("served-over-http")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(served), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/page.txt"
    try:
        if client is not None:
            command = f"curl -s {url}"
        else:
            command = f"{python} -c \"import urllib.request; print(urllib.request.urlopen('{url}').read().decode())\""
        code, result = run_shell(command, jailed)
    finally:
        server.shutdown()

    assert code == 0
    assert "served-over-http" in result


@needs_bwrap
def test_provider_keys_are_not_in_the_command_environment(jailed, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")

    code, result = run_shell("env", jailed)

    assert code == 0
    assert "sk-should-not-leak" not in result


@needs_bwrap
def test_tmp_is_private_and_shared_with_the_file_tools(jailed):
    marker = Path("/tmp") / f"js-jail-test-{os.getpid()}-{time.monotonic_ns()}"

    code, _ = run_shell(f"echo from-shell > {marker}", jailed)

    assert code == 0
    assert not marker.exists()
    assert "from-shell" in tool("read", jailed, path=str(marker))
    assert not tool("write", jailed, path="/tmp/from-tool.txt", content="from-tool\n").startswith("ERROR")
    code, result = run_shell("cat /tmp/from-tool.txt", jailed)
    assert "from-tool" in result


@needs_bwrap
def test_jail_bind_shows_paths_read_only_unless_rw(jailed, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "data.txt").write_text("bound-data")

    code, result = run_shell(f"cat {extra}/data.txt", jailed)
    assert code != 0
    assert tool("read", jailed, path=str(extra / "data.txt")).startswith("ERROR:")

    jailed.jail_bind = (str(extra),)
    code, result = run_shell(f"cat {extra}/data.txt; touch {extra}/new", jailed)
    assert "bound-data" in result
    assert code != 0
    assert "bound-data" in tool("read", jailed, path=str(extra / "data.txt"))
    assert tool("write", jailed, path=str(extra / "w.txt"), content="x").startswith("ERROR:")

    jailed.jail_bind = (f"{extra}:rw",)
    code, _ = run_shell(f"touch {extra}/new", jailed)
    assert code == 0
    assert (extra / "new").exists()
    assert not tool("write", jailed, path=str(extra / "w.txt"), content="x").startswith("ERROR")


@needs_bwrap
def test_spilled_tool_results_stay_readable(jailed):
    spill_dir = paths.tool_results_dir()
    spill_dir.mkdir(parents=True, exist_ok=True)
    spill = spill_dir / "result-1.txt"
    spill.write_text("spilled-result\n")

    code, result = run_shell(f"wc -c {spill}", jailed)

    assert code == 0
    assert "spilled-result" in tool("read", jailed, path=str(spill))
    assert tool("write", jailed, path=str(spill), content="x", overwrite=True).startswith("ERROR:")


def test_jail_bind_setting_refuses_a_relative_path():
    spec = settings.spec_for("jail.bind")

    _value, error = settings.coerce_value(spec, '["relative/dir"]')
    assert error is not None
    value, error = settings.coerce_value(spec, '["~/.cache/uv:rw", "/opt/data"]')
    assert error is None
    assert value == ["~/.cache/uv:rw", "/opt/data"]


@needs_bwrap
def test_a_subagent_is_jailed(jailed, monkeypatch, tmp_path):
    from js.toolkit.meta import _run_one_task_async

    prompts = prompt_dir(tmp_path, "worker")
    cfg = make_cfg(tmp_path, "parent", prompts.parent / "parent")
    jailed.jail_bind = ("/opt/some-bind",)
    seen: dict[str, object] = {}

    async def turn(cfg, system, messages, telemetry, *, tool_context, **kwargs):
        seen["bind"] = tool_context.jail_bind
        seen["ls"] = run_shell("ls -A ~", tool_context)[1]
        seen["read"] = tool("read", tool_context, path="~/.zshrc")
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    asyncio.run(_run_one_task_async(1, 1, "work", jailed, cfg, build_default_registry(), "worker", None))

    assert seen["bind"] == ("/opt/some-bind",)
    assert ".zshrc" not in seen["ls"]
    assert str(seen["read"]).startswith("ERROR:")


@needs_bwrap
def test_terminal_runs_in_the_jail(jailed, monkeypatch):
    pytest.importorskip("pexpect")
    pytest.importorskip("pyte")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    try:
        terminal.terminal_session(action="start", session="t",
                                  command="ls -A ~; echo KEY=${OPENAI_API_KEY:-none}; echo DONE; sleep 5",
                                  context=jailed)
        deadline = time.monotonic() + 10
        screen = ""
        while time.monotonic() < deadline and "DONE" not in screen:
            screen = terminal.terminal_session(action="look", session="t", wait_ms=200, context=jailed)
    finally:
        terminal.close_terminal_sessions(jailed)

    assert "DONE" in screen
    assert "KEY=none" in screen
    assert ".zshrc" not in screen


@needs_bwrap
@needs_kernel
def test_kernel_runs_in_the_jail_and_survives_an_interrupt(jailed):
    jailed.kernel_verbosity = "quiet"
    jailed.kernel_wait_seconds = 0.5
    try:
        listing = kmod.kernel(code="import os\nprint(sorted(os.listdir(os.path.expanduser('~'))))",
                              context=jailed)
        kmod.kernel(code="import time\ntime.sleep(60)", context=jailed)
        stopped = kmod.kernel(action="interrupt", context=jailed)
        after = kmod.kernel(code="print('still-here')", context=jailed)
    finally:
        session = jailed.kernel_session
        if session is not None:
            session.shutdown()

    assert ".zshrc" not in listing
    assert "KeyboardInterrupt" in stopped
    assert "still-here" in after


def test_no_bwrap_refuses_dash_C(monkeypatch, tmp_path, capsys):
    target = tmp_path / "foo"
    target.mkdir()
    monkeypatch.setattr(jail.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "_run_prompt", lambda *a, **k: pytest.fail("ran the prompt unjailed"))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["-C", str(target), "-p", "hi"]) == 2

    assert jail.active() is None
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1
    assert "bwrap" in err


@needs_bwrap
def test_dash_C_runs_the_model_tools_in_the_jail(monkeypatch, tmp_path, operator_home):
    target = tmp_path / "foo"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    seen: dict[str, str] = {}

    def completion_stub(**kwargs):
        seen["ls"] = run_shell("ls -A ~", runtime.T.STOCK_CONTEXT)[1]
        return _fake_stream_result("ok")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)
    monkeypatch.setattr(runtime.model_metadata, "accepts_image_input", lambda *a, **k: False)

    assert cli.main(["-C", str(target), "-p", "hi", "-n", "-q"]) == 0

    assert jail.active() is not None and jail.active().root == target.resolve()
    assert os.environ["JS_JAIL"] == str(target.resolve())
    assert ".zshrc" not in seen["ls"]


def test_commit_helper_dash_C_is_not_the_jail(tmp_path):
    from js import commit_helper

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    commit_helper.main(["-C", str(tmp_path), "survey"])

    assert jail.active() is None


def test_system_prompt_commands_see_the_shell_program(tmp_path):
    spec = persona.PromptSpec(system="shell={{JS_SHELL}}", tool_selectors=())
    cfg = type("Cfg", (), {"settings": {"shell": {"program": "sh"}}, "allow_inline_code": False})()

    expanded = persona._expand_spec(spec, cfg)

    assert expanded.system == f"shell={shutil.which('sh')}"


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler for envctx")
def test_envctx_reports_the_tool_shell_and_the_jail(tmp_path):
    envctx = Path(__file__).resolve().parent.parent / "tools" / "envctx.c"
    env = {**os.environ, "JS_SHELL": "/usr/bin/tool-shell", "SHELL": "/usr/bin/login-shell",
           "JS_JAIL": str(tmp_path / "foo"), "JS_MODE": "repl"}

    out = subprocess.run(["sh", str(envctx)], env=env, capture_output=True, text=True, timeout=60).stdout

    user_line = next(line for line in out.splitlines() if line.startswith("user="))
    assert " shell=/usr/bin/tool-shell " in user_line
    # envctx writes a path under $HOME with ~.
    assert " confined=~/foo" in user_line
    assert any(line.startswith("rule:") and "~/foo" in line for line in out.splitlines())
