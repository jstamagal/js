"""`js -C DIR` keeps the agent in DIR: command tools run under bubblewrap with
the home hidden, file tools refuse paths outside DIR and the bound paths."""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from js import cli, jail, paths, persona, runtime, settings
from js.toolkit import ToolContext, call_tool, fs, kernel as kmod, process_net, terminal
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
    context = ToolContext(cwd=root, kernel_enabled=True)
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


def test_removing_a_parent_preserves_a_read_only_bind(tmp_path, monkeypatch):
    root = tmp_path / "project"
    protected = root / "parent" / "protected"
    protected.mkdir(parents=True)
    key = protected / "data"
    key.write_text("keep")
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(jail, "ACTIVE", jail.Jail(root=root, bwrap="bwrap", private=private))
    context = ToolContext(cwd=root)
    context.jail_bind = (str(protected),)

    result = tool("remove", context, path=str(protected.parent), permanent=True)

    assert key.exists()
    assert key.read_text() == "keep"
    assert isinstance(result, jail.Refusal)


def test_removing_a_parent_obeys_a_writable_bind_override(tmp_path, monkeypatch):
    root = tmp_path / "project"
    bound = root / "parent" / "bound"
    bound.mkdir(parents=True)
    (bound / "data").write_text("remove")
    private = tmp_path / "private"
    private.mkdir()
    active = jail.Jail(root=root, bwrap="bwrap", private=private)
    active.add(jail.Bind(bound, True))
    monkeypatch.setattr(jail, "ACTIVE", active)
    context = ToolContext(cwd=root)
    context.jail_bind = (str(bound),)

    result = tool("remove", context, path=str(bound.parent), permanent=True)

    assert not isinstance(result, jail.Refusal)
    assert not bound.parent.exists()


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
        # Only a tool that runs outside the jail under this HOME: a rustup
        # shim finds no toolchain in the test HOME even outside the jail.
        if shutil.which(name) and subprocess.run([name, "--version"], capture_output=True).returncode == 0:
            code, result = run_shell(f"{name} --version", jailed)
            assert code == 0, result


def test_the_host_tmp_on_path_does_not_replace_the_private_tmp(jailed):
    argv = jail.active().argv(["true"], env_path=f"/tmp{os.pathsep}/usr/bin")

    binds = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg.endswith("bind") or arg.endswith("bind-try")]
    assert [src for src, dst in binds if dst == "/tmp"] == [str(jail.active().tmp)]


def test_a_path_directory_inside_the_jail_dir_stays_writable(jailed, monkeypatch):
    """DIR sits in a hidden tree (the test HOME); its own .venv/bin on PATH is
    not bound back read-only by argv (covered by DIR's rw bind), so the file
    tools must not call it read-only either (ALL-CLAIMS jail.py MED)."""
    active = jail.active()
    bin_dir = active.root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tool").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin")
    if not any(jail._under(bin_dir.resolve(), h) for h in jail.hidden_roots()):
        pytest.skip("the jail dir is not inside a hidden tree here")

    argv = active.argv(["true"], env_path=os.environ["PATH"])
    ro = [argv[i + 1] for i, arg in enumerate(argv) if arg in {"--ro-bind", "--ro-bind-try"}]
    assert str(bin_dir.resolve()) not in ro
    assert active.confine(bin_dir / "tool", write=True) == (bin_dir / "tool").resolve()


def test_a_path_directory_under_the_host_tmp_is_bound_back_read_only(jailed, tmp_path_factory, monkeypatch):
    # tmp_path is the test HOME, a hidden tree; the venv sits outside it.
    venv = tmp_path_factory.mktemp("venv").resolve()
    if Path("/tmp") not in venv.parents or any(jail._under(venv, h) for h in jail.hidden_roots()):
        pytest.skip("pytest's temporary tree is not plainly under /tmp")
    bin_dir = venv / "bin"
    bin_dir.mkdir()
    (bin_dir / "tool").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin")
    active = jail.active()

    argv = active.argv(["true"], env_path=os.environ["PATH"])
    ro = [argv[i + 1:i + 3] for i, arg in enumerate(argv) if arg in {"--ro-bind", "--ro-bind-try"}]
    assert [str(bin_dir), str(bin_dir)] in ro
    assert active.confine(bin_dir / "tool") == bin_dir / "tool"
    with pytest.raises(jail.JailError):
        active.confine(bin_dir / "tool", write=True)


@needs_bwrap
def test_path_directories_under_host_tmp_run(jailed, monkeypatch):
    bin_dir = Path(tempfile.mkdtemp(prefix="js-jail-bin-", dir="/tmp"))
    try:
        hello = bin_dir / "hello-from-tmp"
        hello.write_text("#!/bin/sh\necho hello-ran\n")
        hello.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        code, result = run_shell("hello-from-tmp", jailed)
    finally:
        shutil.rmtree(bin_dir)

    assert code == 0, result
    assert "hello-ran" in result


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
def test_a_host_tmp_path_the_jail_does_not_show_is_refused_without_the_jail_dir(jailed):
    """A /tmp path absent from the jail's private /tmp is outside the jail; the
    refusal names the path the model gave and carries no retry count."""
    host_dir = Path(tempfile.mkdtemp(prefix="js-jail-other-", dir="/tmp"))
    try:
        (host_dir / "x.txt").write_text("host-only\n")
        calls = [runtime._PendingToolCall("r", "read", [f'{{"file_path": "{host_dir}/x.txt"}}'])]

        records = runtime._dispatch_tool_calls(
            calls, runtime.Telemetry(None), 65536, False, runtime.ToolErrorTracker(),
            build_default_registry(), jailed,
        )

        result = records[0][2]
        assert result.startswith("ERROR:")
        assert "outside the jail" in result
        assert str(jail.active().private) not in result
        assert "<retry>" not in result
        assert "host-only" not in result
    finally:
        shutil.rmtree(host_dir, ignore_errors=True)



@needs_bwrap
def test_file_tool_results_name_the_jail_tmp_not_the_host_dir(jailed):
    """Under -C every fs tool result names /tmp and ~/.js/tmp paths as the
    model gave them, in success and in error."""
    private = str(jail.active().private)
    name = f"js-jail-names-{os.getpid()}-{time.monotonic_ns()}"
    tmp_file = f"/tmp/{name}/a.txt"
    js_tmp_file = f"~/.js/tmp/{name}.txt"
    results = [
        tool("write", jailed, path=tmp_file, content="alpha\nbeta\n"),
        tool("write", jailed, path=tmp_file, content="again\n"),
        tool("read", jailed, path=tmp_file),
        tool("patch", jailed, path=tmp_file, old_string="beta", new_string="gamma"),
        tool("patch", jailed, path=tmp_file, old_string="absent", new_string="x"),
        tool("fs_search", jailed, pattern="gamma", path=f"/tmp/{name}"),
        tool("fs_search", jailed, pattern="gamma", path="/tmp", output_mode="content"),
        tool("write", jailed, path=f"/tmp/{name}/b.py", content="f(1)\n"),
        tool("ast_search", jailed, pattern="f($A)", path=f"/tmp/{name}", lang="python"),
        tool("write", jailed, path=js_tmp_file, content="delta\n"),
        tool("read", jailed, path=js_tmp_file),
        tool("remove", jailed, path=tmp_file, permanent=True),
        tool("undo", jailed, path=tmp_file),
        tool("remove", jailed, path=js_tmp_file, permanent=True),
        tool("remove", jailed, path=js_tmp_file, permanent=True),
    ]

    for result in results:
        assert private not in result
    assert f"/tmp/{name}/a.txt" in results[0]
    assert "gamma" in results[5] + results[6] and f"/tmp/{name}/a.txt" in results[6]
    if fs._ast_grep_binary() is not None:
        assert f"/tmp/{name}/b.py" in results[8]
    assert str(Path.home() / ".js" / "tmp" / f"{name}.txt") in results[9]
    for error in (results[1], results[4], results[14]):
        assert error.startswith("ERROR")


@needs_bwrap
def test_an_image_read_under_the_jail_tmp_keeps_the_host_path_for_the_model_client(jailed):
    """The image marker's path field is what the model client reads the image
    from, so it stays the host path; the stub the model sees does not."""
    private = str(jail.active().private)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    image = jail.active().tmp / f"js-jail-img-{os.getpid()}.png"
    image.write_bytes(png)
    jailed.vision_enabled = True

    result = tool("read", jailed, path=f"/tmp/{image.name}")

    _prefix, host, _mime, stub = result.split("\t", 3)
    assert Path(host).read_bytes() == png
    assert private not in stub
    assert f"/tmp/{image.name}" in stub


def test_a_jail_refusal_gets_no_retry_count_and_other_errors_do():
    tracker = runtime.ToolErrorTracker()

    refused = tracker.record("read", jail.Refusal("ERROR: /x is outside the jail"))
    failed = tracker.record("read", "ERROR: no such file: /y")

    assert "<retry>" not in refused
    assert "<retry>" in failed


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


@needs_bwrap
def test_jail_hide_blanks_key_stores_in_a_jail_rooted_at_home(operator_home, monkeypatch):
    ssh = operator_home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text(SECRET)
    (operator_home / ".netrc").write_text(SECRET)
    (paths.home() / "jsrc").parent.mkdir(parents=True, exist_ok=True)
    (paths.home() / "jsrc").write_text(f"set API_KEY {SECRET}\n")
    spill_dir = paths.tool_results_dir()
    spill_dir.mkdir(parents=True, exist_ok=True)
    (spill_dir / "result-1.txt").write_text("spilled-result\n")
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()

    code, result = run_shell(
        "cat ~/.ssh/id_ed25519 ~/.netrc ~/.js/jsrc; "
        f"cat {spill_dir}/result-1.txt; echo kept > ~/.js/tmp/t && cat ~/.js/tmp/t",
        context,
    )

    assert SECRET not in result
    assert "spilled-result" in result
    assert "kept" in result
    assert tool("read", context, path=str(ssh / "id_ed25519")).startswith("ERROR:")
    assert tool("read", context, path=str(paths.home() / "jsrc")).startswith("ERROR:")
    assert "spilled-result" in tool("read", context, path=str(spill_dir / "result-1.txt"))


@needs_bwrap
def test_searches_in_a_home_rooted_jail_leave_jail_hide_paths_out(operator_home, monkeypatch):
    ssh = operator_home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("KEYMATERIAL-4471\n")
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()

    files = tool("fs_search", context, pattern="**", glob=".ssh/**", output_mode="files_with_matches")
    found = tool("fs_search", context, pattern="KEYMATERIAL", glob=".ssh/**")
    listing = fs.list_dir(str(operator_home), recursive=True, context=context)

    assert "id_ed25519" not in files + found + listing
    assert "KEYMATERIAL" not in found


@needs_bwrap
def test_a_jail_dir_or_bind_inside_a_jail_hide_path_stays_hidden(operator_home, tmp_path, monkeypatch):
    nested = operator_home / ".ssh" / "nested"
    nested.mkdir(parents=True)
    (nested / "key").write_text(SECRET)
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = (str(nested),)

    code, result = run_shell(f"cat {nested}/key", context)

    assert SECRET not in result
    assert tool("read", context, path=str(nested / "key")).startswith("ERROR:")


@needs_bwrap
def test_file_tools_and_commands_share_the_private_js_tmp_in_a_home_rooted_jail(operator_home, monkeypatch):
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()
    target = paths.user_home() / ".js" / "tmp" / "shared.txt"

    assert not tool("write", context, path=str(target), content="from-the-file-tool").startswith("ERROR")
    code, result = run_shell(f"cat {target}", context)

    assert "from-the-file-tool" in result
    assert not target.exists()  # it lives in the jail's private directory


@needs_bwrap
@pytest.mark.skipif(not Path("/etc/hostname").is_file(), reason="no /etc/hostname")
def test_jail_hide_blanks_a_path_the_read_only_root_shows(jailed):
    jailed.jail_hide = ("/etc/hostname",)

    code, result = run_shell("wc -c < /etc/hostname", jailed)

    assert code == 0
    assert "\n0\n" in f"\n{result}\n"


@needs_bwrap
def test_a_path_directory_inside_a_jail_hide_path_stays_hidden(operator_home, monkeypatch):
    tools = operator_home / ".ssh" / "bin"
    tools.mkdir(parents=True)
    (tools / "key").write_text(SECRET)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    root = operator_home / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = ()

    code, result = run_shell(f"cat {tools}/key", context)

    assert SECRET not in result


@needs_bwrap
def test_a_jail_hide_path_with_glob_characters_stays_out_of_searches(operator_home, monkeypatch):
    keys = operator_home / "[keys]"
    keys.mkdir()
    (keys / "token.txt").write_text("KEYMATERIAL-9182\n")
    (operator_home / "notes").mkdir()
    (operator_home / "notes" / "plain.txt").write_text("KEYMATERIAL-9183\n")
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()
    context.jail_hide = (str(keys),)

    found = tool("fs_search", context, pattern="KEYMATERIAL")

    assert "token.txt" not in found
    assert "plain.txt" in found  # the search itself reaches the rest of DIR


@needs_bwrap
def test_wrap_never_probes_a_network_mount(jailed, monkeypatch):
    dead = Path("/srv/js-test-dead-nfs")
    mounts = jail._mounts()
    monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, jail._Mount("0:999", Path("/"), dead, "nfs4")])
    probed: list[Path] = []
    real_is_dir = getattr(jail, "_is_dir", Path.is_dir)

    def prober(path: Path) -> bool:
        if path == dead or dead in path.parents:
            probed.append(path)
            raise AssertionError(f"probed the dead mount {path}")
        return real_is_dir(path)

    monkeypatch.setattr(jail, "_is_dir", prober, raising=False)

    argv = jail.wrap(["true"], jailed, cwd=jailed.cwd)

    assert probed == []
    assert "--ro-bind" in argv  # the jail was still built


@needs_bwrap
def test_removing_an_ancestor_of_a_hidden_path_is_refused(operator_home, monkeypatch):
    gh = operator_home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "hosts.yml").write_text(SECRET)
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()

    result = tool("remove", context, path=str(operator_home / ".config"), permanent=True)

    assert result.startswith("ERROR")
    assert (gh / "hosts.yml").read_text() == SECRET


@needs_bwrap
def test_removing_a_hidden_path_that_is_a_symlink_is_refused(operator_home, tmp_path, monkeypatch):
    store = tmp_path / "dotfiles" / "netrc"
    store.parent.mkdir()
    store.write_text(SECRET)
    (operator_home / ".netrc").symlink_to(store)
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()

    result = tool("remove", context, path=str(operator_home / ".netrc"), permanent=True)

    assert result.startswith("ERROR")
    assert (operator_home / ".netrc").is_symlink()


@needs_bwrap
def test_a_symlink_into_a_hidden_path_stays_out_of_a_recursive_listing(operator_home, tmp_path, monkeypatch):
    ssh = operator_home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text(SECRET)
    work = operator_home / "work"
    work.mkdir()
    (work / "alias").symlink_to(ssh / "id_ed25519")
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()

    listing = fs.list_dir(str(work), recursive=True, context=context)

    assert "alias" not in listing


@needs_bwrap
def test_a_recursive_listing_reads_the_hidden_paths_once_not_per_file(operator_home, monkeypatch):
    small, large = operator_home / "small", operator_home / "large"
    small.mkdir()
    large.mkdir()
    (small / "a.txt").write_text("a")
    for index in range(30):
        (large / f"{index}.txt").write_text("a")
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()
    reads: list[int] = []
    real_hides = jail.Jail.hides
    monkeypatch.setattr(jail.Jail, "hides", lambda self, hide=None: reads.append(1) or real_hides(self, hide))

    def reads_for(directory: Path) -> int:
        reads.clear()
        fs.list_dir(str(directory), recursive=True, context=context)
        return len(reads)

    assert reads_for(large) == reads_for(small)


@needs_bwrap
def test_hidden_ast_matches_do_not_use_up_the_result_limit(operator_home, monkeypatch):
    if fs._ast_grep_binary() is None:
        pytest.skip("ast-grep is not installed")
    keys = operator_home / "aaa-keys"
    keys.mkdir()
    for n in range(20):
        (keys / f"k{n}.js").write_text("let secret = 1;\n")
    work = operator_home / "work"
    work.mkdir()
    for n in range(2):
        (work / f"v{n}.js").write_text("let secret = 2;\n")
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()
    context.jail_hide = (str(keys),)

    result = fs.ast_search("let secret = $A", path=str(operator_home), lang="javascript",
                           max_results=1, context=context)

    assert "aaa-keys" not in result
    assert "v0.js" in result or "v1.js" in result
    assert "additional matches omitted" in result


def _mount_targets(argv: list[str]) -> list[str]:
    """Every path a bwrap argument mounts something on."""
    targets: list[str] = []
    for index, arg in enumerate(argv):
        if arg in {"--tmpfs", "--dev", "--proc"}:
            targets.append(argv[index + 1])
        elif arg in {"--bind", "--ro-bind", "--bind-try", "--ro-bind-try"}:
            targets.append(argv[index + 2])
    return targets


@needs_bwrap
def test_a_network_mount_is_never_mounted_over(jailed, monkeypatch):
    fake = Path("/etc/js-test-nfs")
    mounts = jail._mounts()
    monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, jail._Mount("0:998", Path("/"), fake, "nfs4")])

    argv = jail.wrap(["cat", "/etc/hostname"], jailed, cwd=jailed.cwd)

    assert not any(Path(target) == fake or fake in Path(target).parents for target in _mount_targets(argv))
    assert "/etc" in _mount_targets(argv)  # its parent is covered instead
    if Path("/etc/hostname").is_file():
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0
        assert proc.stdout == Path("/etc/hostname").read_text()


@needs_bwrap
def test_a_network_mount_under_tmp_does_not_bring_the_host_tmp_back(jailed, monkeypatch, tmp_path):
    host_file = Path(tempfile.gettempdir()) / f"js-host-only-{os.getpid()}.txt"
    host_file.write_text("host tmp")
    try:
        fake = Path(tempfile.gettempdir()) / "js-test-nas"
        mounts = jail._mounts()
        monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, jail._Mount("0:997", Path("/"), fake, "nfs4")])

        argv = jail.wrap(["ls", "/tmp"], jailed, cwd=jailed.cwd)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)

        assert host_file.name not in proc.stdout
        assert "/tmp" not in [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    finally:
        host_file.unlink()


@needs_bwrap
def test_a_network_mount_directly_in_root_is_left_out_without_being_listed(jailed, monkeypatch):
    fake = Path("/js-test-nfs-root")
    mounts = jail._mounts()
    monkeypatch.setattr(jail, "_mounts", lambda: [
        *mounts, jail._Mount("0:996", Path("/"), fake, "nfs4"),
        jail._Mount("0:995", Path("/"), fake / "child", "nfs4")])
    real_scandir = os.scandir

    def scandir(path="."):
        if not isinstance(path, int) and (Path(path) == fake or fake in Path(path).parents):
            raise AssertionError(f"listed the network mount {path}")
        return real_scandir(path)

    monkeypatch.setattr(jail.os, "scandir", scandir)

    argv = jail.wrap(["cat", "/etc/hostname"], jailed, cwd=jailed.cwd)

    assert "/" not in _mount_targets(argv)  # / is built entry by entry, without the mount
    assert not any(Path(t) == fake or fake in Path(t).parents for t in _mount_targets(argv))
    if Path("/etc/hostname").is_file():
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == Path("/etc/hostname").read_text()


@needs_bwrap
@pytest.mark.parametrize("cover", ["network", "hidden"])
def test_a_tmpfs_under_a_read_only_view_takes_no_new_file(operator_home, tmp_path, monkeypatch, cover):
    # The parent of a network mount, and the parent of a hidden path under a
    # read-only bind, get a tmpfs with their entries bound back; the view
    # stays read-only all the same.
    root = tmp_path / "work"
    root.mkdir()
    if cover == "network":
        mounts = jail._mounts()
        fake = jail._Mount("0:993", Path("/"), Path("/etc/js-test-nfs"), "nfs4")
        monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, fake])
        target, bind = Path("/etc/js-made-in-jail"), ()
    else:
        target, bind = operator_home / "made-in-jail", ("~",)
    monkeypatch.chdir(root)
    try:
        jail.enter(root, setting=bind)
        context = ToolContext(cwd=root)
        context.jail_bind = bind

        assert _shell_ok(f"touch {root}/made-inside", context)
        assert not _shell_ok(f"touch {target}", context)
    finally:
        jail.leave()


@needs_bwrap
def test_a_home_on_a_network_mount_is_a_writable_tmpfs(monkeypatch):
    base = Path(tempfile.mkdtemp(dir="/var/tmp" if os.access("/var/tmp", os.W_OK) else None))
    if jail._under(base, Path("/tmp")):
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip("needs a writable directory outside /tmp")
    home, root = base / "home", base / "work"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    mounts = jail._mounts()
    monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, jail._Mount("0:992", Path("/"), home, "nfs4")])
    monkeypatch.chdir(root)
    try:
        jail.enter(root)
        context = ToolContext(cwd=root)
        context.jail_bind = ()

        assert _shell_ok(f"touch {home}/made-in-jail", context)
        assert not (home / "made-in-jail").exists()
    finally:
        jail.leave()
        shutil.rmtree(base, ignore_errors=True)


def test_jail_bind_setting_refuses_a_relative_path():
    spec = settings.spec_for("JAIL_BIND")

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
def test_a_background_subagent_is_jailed(jailed, monkeypatch, tmp_path):
    from js.toolkit import meta

    prompts = prompt_dir(tmp_path, "worker")
    jailed.config = make_cfg(tmp_path, "parent", prompts.parent / "parent")
    seen: dict[str, object] = {}

    async def turn(cfg, system, messages, telemetry, *, tool_context, **kwargs):
        seen["ls"] = run_shell("ls -A ~", tool_context)[1]
        seen["read"] = tool("read", tool_context, path="~/.zshrc")
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    started = meta.task(tasks=["work"], agent_id="worker", background=True, context=jailed)
    handle = started.rsplit("HANDLE ", 1)[1].split()[0]
    finished = meta.task(action="wait", handle=handle, timeout=60, context=jailed)

    assert "done" in finished
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


@needs_bwrap
@needs_kernel
def test_a_kernel_cell_calls_tools_under_the_jail(jailed):
    jailed.kernel_verbosity = "quiet"
    jailed.kernel_wait_seconds = 30
    code = ("print('INSIDE', tools.read('inside.txt', show_line_numbers=False).strip())\n"
            "try:\n"
            "    print(tools.read('~/.zshrc'))\n"
            "except tools.ToolError as exc:\n"
            "    print('REFUSED')\n")
    try:
        _, result = runtime._dispatch("kernel", json.dumps({"code": code}), runtime.Telemetry(None),
                                      cap_bytes=65536, registry=build_default_registry(),
                                      tool_context=jailed)
    finally:
        session = jailed.kernel_session
        if session is not None:
            session.shutdown()

    assert "INSIDE inside" in result
    assert "REFUSED" in result
    assert SECRET not in result


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
    cfg = type("Cfg", (), {"settings": {"SHELL_PROGRAM": "sh"}, "allow_inline_code": False})()

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


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler for envctx")
def test_envctx_hist_zero_leaves_shell_history_out(tmp_path):
    envctx = Path(__file__).resolve().parent.parent / "tools" / "envctx.c"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_history").write_text("echo secret-cmd\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "SHELL": "/bin/bash", "CC": shutil.which("cc")}
    for name in ("HISTFILE", "ENVCTX_HIST", "ENVCTX_SKIP"):
        env.pop(name, None)

    def run(**extra):
        return subprocess.run(["sh", str(envctx)], env={**env, **extra},
                              capture_output=True, text=True, timeout=60).stdout

    assert "secret-cmd" in run()
    out = run(ENVCTX_HIST="0")
    assert "recent_shell" not in out and "secret-cmd" not in out


@needs_bwrap
def test_a_kernel_started_before_a_jail_hide_change_is_restarted(monkeypatch, tmp_path):
    started: list = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.stopped = False

        def start(self):
            started.append(self)

        def alive(self):
            return not self.stopped

        def shutdown(self):
            self.stopped = True

    monkeypatch.setattr(kmod, "missing_dependencies", lambda: "")
    monkeypatch.setattr(kmod, "KernelSession", FakeSession)
    monkeypatch.setattr(kmod, "_attach_tools", lambda session, context: None)
    root = tmp_path / "root"
    root.mkdir()
    jail.enter(root)
    context = ToolContext(cwd=root, kernel_enabled=True)
    context.jail_hide = ()

    first, _err, _new = kmod.get_session(context)
    again, _err, started_again = kmod.get_session(context)
    context.jail_hide = ("~/.ssh",)
    changed, _err, restarted = kmod.get_session(context)

    assert again is first and not started_again
    assert restarted and changed is not first and first.stopped
    assert changed.jail_hide == ("~/.ssh",)


@needs_bwrap
def test_a_terminal_started_before_a_jail_hide_change_runs_nothing_more(operator_home, tmp_path, monkeypatch):
    from js.toolkit import terminal

    pytest.importorskip("pexpect")
    pytest.importorskip("pyte")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_hide = ()
    try:
        started = terminal.terminal_session("start", "main", command="cat", context=context)
        assert not started.startswith("ERROR"), started
        context.jail_hide = (str(operator_home / "notes.txt"),)

        sent = terminal.terminal_session("send", "main", keys="hi,Enter", context=context)

        assert sent.startswith("ERROR")
        assert "main" not in context.terminal_sessions
    finally:
        terminal.close_terminal_sessions(context)


@needs_bwrap
def test_the_python_header_retry_keeps_hidden_paths_out(operator_home, monkeypatch):
    if fs._ast_grep_binary() is None:
        pytest.skip("ast-grep is not installed")
    keys = operator_home / "aaa-keys"
    keys.mkdir()
    (keys / "k.py").write_text("def probe(x):\n    return 1\n")
    work = operator_home / "work"
    work.mkdir()
    (work / "v.py").write_text("def probe(x):\n    return 2\n")
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()
    context.jail_hide = (str(keys),)

    result = fs.ast_search("def probe($X):", path=str(operator_home), lang="python", context=context)

    assert "v.py" in result
    assert "aaa-keys" not in result
    assert not result.startswith("ERROR")


def test_an_unjailed_kernel_survives_a_jail_setting_change(monkeypatch, tmp_path):
    class FakeSession:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.stopped = False

        def start(self):
            pass

        def alive(self):
            return not self.stopped

        def shutdown(self):
            self.stopped = True

    jail.leave()
    monkeypatch.setattr(kmod, "missing_dependencies", lambda: "")
    monkeypatch.setattr(kmod, "KernelSession", FakeSession)
    monkeypatch.setattr(kmod, "_attach_tools", lambda session, context: None)
    context = ToolContext(cwd=tmp_path, kernel_enabled=True)
    context.jail_hide = ()

    first, _err, _new = kmod.get_session(context)
    context.jail_hide = ("~/.ssh",)
    again, _err, started = kmod.get_session(context)

    assert again is first and not started and not first.stopped


def _hidden_tree(home: Path) -> dict[str, Path]:
    """A hidden key, a hidden notebook, and every way to name them."""
    ssh = home / ".ssh"
    ssh.mkdir(exist_ok=True)
    (ssh / "key").write_text(SECRET)
    (ssh / "nb.ipynb").write_text(json.dumps({"cells": [{"cell_type": "code", "id": "c1", "source": SECRET,
                                                        "metadata": {}, "outputs": [], "execution_count": None}],
                                             "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    (home / "alias_file").symlink_to(ssh / "key")
    (home / "alias_dir").symlink_to(ssh)
    return {"direct": ssh / "key", "file alias": home / "alias_file", "dir alias": home / "alias_dir" / "key",
            "notebook": ssh / "nb.ipynb"}


def _assert_nothing_hidden_leaks(context: ToolContext, home: Path, names: dict[str, Path]) -> None:
    key = home / ".ssh" / "key"
    for how, path in names.items():
        for name, args in (("read", {"path": str(path)}),
                           ("write", {"path": str(path), "content": "x", "overwrite": True}),
                           ("patch", {"path": str(path), "old_string": SECRET, "new_string": "x"}),
                           ("remove", {"path": str(path), "permanent": True})):
            result = tool(name, context, **args)
            assert SECRET not in result, (how, name, result)
        assert SECRET not in tool("notebook_edit", context, file_path=str(names["notebook"]),
                                  cell_id="c1", new_source="x")
    for args in ({"pattern": SECRET}, {"pattern": SECRET, "glob": "**/key"},
                 {"pattern": "**", "glob": "**/key", "output_mode": "files_with_matches"}):
        result = tool("fs_search", context, path=str(home), **args)
        assert SECRET not in result and "/.ssh/" not in result, (args, result)
    assert ".ssh/key" not in fs.list_dir(str(home), recursive=True, context=context)
    code, shown = run_shell(
        f"cat {key} {home}/alias_file {home}/alias_dir/key {home}/*/key {home}/.ssh/* 2>/dev/null; true", context)
    assert SECRET not in shown
    assert key.read_text() == SECRET  # nothing changed it


@needs_bwrap
def test_no_tool_reaches_a_hidden_path_in_a_home_rooted_jail(operator_home, monkeypatch):
    names = _hidden_tree(operator_home)
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home)
    context.jail_bind = ()

    _assert_nothing_hidden_leaks(context, operator_home, names)


@needs_bwrap
def test_no_tool_reaches_a_hidden_path_through_a_bound_alias(operator_home, tmp_path, monkeypatch):
    _hidden_tree(operator_home)
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = (str(operator_home / "alias_dir"), str(operator_home / "alias_file"))

    code, shown = run_shell(f"cat {operator_home}/alias_dir/key {operator_home}/alias_file 2>/dev/null; true", context)

    assert SECRET not in shown
    for path in (operator_home / "alias_dir" / "key", operator_home / "alias_file"):
        assert SECRET not in tool("read", context, path=str(path))


@needs_bwrap
def test_a_hidden_path_is_hidden_in_another_mount_view_too(operator_home, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    (mirror / ".ssh").mkdir(parents=True)
    (mirror / ".ssh" / "key").write_text(SECRET)
    home = jail._real(operator_home)
    mounts = jail._mounts()
    holder = max((m for m in mounts if jail._under(home, m.point)), key=lambda m: len(m.point.parts))
    view = jail._Mount(holder.device, holder.root / home.relative_to(holder.point), mirror, "ext4")
    monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, view])
    monkeypatch.chdir(mirror)
    jail.enter(mirror)
    context = ToolContext(cwd=mirror)
    context.jail_bind = ()

    code, shown = run_shell(f"cat {mirror}/.ssh/key 2>/dev/null; true", context)

    assert SECRET not in shown
    assert tool("read", context, path=str(mirror / ".ssh" / "key")).startswith("ERROR")


@needs_bwrap
@needs_kernel
def test_the_kernel_reaches_no_hidden_path(operator_home, monkeypatch):
    names = _hidden_tree(operator_home)
    monkeypatch.chdir(operator_home)
    jail.enter(operator_home)
    context = ToolContext(cwd=operator_home, kernel_enabled=True)
    context.jail_bind = ()
    code = (f"import contextlib\nfor p in {[str(p) for p in names.values()]!r}:\n"
            "    with contextlib.suppress(OSError):\n        print(open(p).read())\nprint('kernel-ran')\n")

    try:
        result = str(kmod.kernel(code=code, context=context))
    finally:
        if context.kernel_session is not None:
            context.kernel_session.shutdown()

    assert "kernel-ran" in result
    assert SECRET not in result


@needs_bwrap
def test_a_hidden_path_made_after_the_command_starts_stays_hidden(operator_home, tmp_path, monkeypatch):
    shared = operator_home / "shared"
    shared.mkdir()
    (shared / "visible.txt").write_text("visible")
    later = shared / "later"
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = (str(shared),)
    context.jail_hide = (str(later),)

    argv = jail.wrap(["sh", "-c", f"sleep 1; cat {later}/key 2>/dev/null; cat {shared}/visible.txt"],
                     context, cwd=root)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)
    later.mkdir()
    (later / "key").write_text(SECRET)
    out, _ = proc.communicate(timeout=60)

    assert SECRET not in out
    assert "visible" in out
    assert tool("read", context, path=str(later / "key")).startswith("ERROR")


@needs_bwrap
def test_a_bound_alias_of_a_hidden_paths_parent_shows_it_empty(operator_home, tmp_path, monkeypatch):
    gh = operator_home / ".config" / "gh"
    gh.mkdir(parents=True)
    (gh / "hosts.yml").write_text(SECRET)
    (operator_home / ".config" / "other.txt").write_text("other")
    (operator_home / "cfg").symlink_to(operator_home / ".config")
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = (str(operator_home / "cfg"),)

    code, shown = run_shell(f"cat {operator_home}/cfg/gh/hosts.yml 2>/dev/null; cat {operator_home}/cfg/other.txt", context)

    assert SECRET not in shown
    assert "other" in shown
    assert SECRET not in tool("read", context, path=str(operator_home / "cfg" / "gh" / "hosts.yml"))


def _shell_ok(command: str, context: ToolContext) -> bool:
    code, _result = run_shell(command, context)
    return code == 0


@needs_bwrap
@pytest.mark.parametrize("dir_in_home", [True, False])
@pytest.mark.parametrize("bind", [(), ("~",), ("~:rw",)])
@pytest.mark.parametrize("hide_kind", ["default", "none", "custom"])
@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("network", [True, False])
def test_the_mount_plan_keeps_dir_writable_and_binds_in_mode_and_hidden_paths_blank(
        operator_home, tmp_path, monkeypatch, dir_in_home, bind, hide_kind, present, network):
    custom = operator_home / "secretdir"
    if present:
        (operator_home / ".ssh").mkdir(exist_ok=True)
        (operator_home / ".ssh" / "key").write_text(SECRET)
        custom.mkdir(exist_ok=True)
        (custom / "key").write_text(SECRET)
    hide = {"default": None, "none": (), "custom": (str(custom),)}[hide_kind]
    hidden_files = {"default": [operator_home / ".ssh" / "key"], "none": [],
                    "custom": [custom / "key"]}[hide_kind]
    root = (operator_home / "project") if dir_in_home else Path(tempfile.mkdtemp(dir=tmp_path.parent))
    root.mkdir(exist_ok=True)
    if network:
        mounts = jail._mounts()
        fake = jail._Mount("0:994", Path("/"), Path("/etc/js-test-nfs"), "nfs4")
        monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, fake])
    monkeypatch.chdir(root)
    skip = ("made-inside", "made-in-home")
    before = _host_tree(operator_home, root, skip=skip)
    try:
        jail.enter(root, setting=bind, hide=hide)
        context = ToolContext(cwd=root)
        context.jail_bind = bind
        context.jail_hide = hide

        assert _shell_ok(f"touch {root}/made-inside", context)
        assert (root / "made-inside").exists()
        home_file = operator_home / "made-in-home"
        run_shell(f"touch {home_file} 2>/dev/null; true", context)
        # Only a writable bind of ~ reaches the host's home; a read-only one or
        # the jail's own tmpfs does not.
        assert home_file.exists() == (bind == ("~:rw",)), (bind, hide_kind)
        for path in hidden_files:
            if path.exists():
                code, shown = run_shell(f"cat {path} 2>/dev/null; true", context)
                assert SECRET not in shown, (path, bind, hide_kind)
        # The jail makes nothing on the host: no mount point, no placeholder.
        assert _host_tree(operator_home, root, skip=skip) == before
    finally:
        jail.leave()
        if not dir_in_home:
            shutil.rmtree(root, ignore_errors=True)


def _host_tree(*roots: Path, skip: tuple[str, ...] = ()) -> dict[str, tuple]:
    """Every path under ``roots`` as the host has it: its kind and its bytes
    or link target. js's own state (~/.js) and the names in ``skip`` are left out."""
    out: dict[str, tuple] = {}
    for root in roots:
        for top, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not (Path(top) == Path(os.environ["HOME"]) and d == ".js")]
            for name in [*dirs, *files]:
                path = Path(top) / name
                if name in skip:
                    continue
                if path.is_symlink():
                    out[str(path)] = ("link", os.readlink(path))
                elif path.is_dir():
                    out[str(path)] = ("dir",)
                else:
                    out[str(path)] = ("file", path.read_bytes())
    return out


def _while_running(context: ToolContext, root: Path, command: str, change) -> str:
    """``command`` run in the jail, with ``change`` made on the host while it runs."""
    argv = jail.wrap(["sh", "-c", f"sleep 1; {command}"], context, cwd=root)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)
    change()
    out, _ = proc.communicate(timeout=60)
    return out


@needs_bwrap
def test_replacing_a_hidden_file_on_the_host_does_not_show_it(operator_home, tmp_path, monkeypatch):
    netrc = operator_home / ".netrc"
    netrc.write_text("old")
    root = Path(tempfile.mkdtemp(dir=tmp_path.parent))
    monkeypatch.chdir(root)
    jail.enter(root, setting=("~",))
    context = ToolContext(cwd=root)
    context.jail_bind = ("~",)

    def replace_it():
        fresh = operator_home / ".netrc.new"
        fresh.write_text(SECRET)
        os.replace(fresh, netrc)

    try:
        out = _while_running(context, root, f"cat {netrc} 2>/dev/null; true", replace_it)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    assert SECRET not in out


@needs_bwrap
def test_a_missing_hidden_path_stays_hidden_through_a_read_only_alias(operator_home, tmp_path, monkeypatch):
    config = operator_home / ".config"
    config.mkdir()
    (operator_home / "cfg").symlink_to(config)
    root = Path(tempfile.mkdtemp(dir=tmp_path.parent))
    monkeypatch.chdir(root)
    jail.enter(root, setting=(str(operator_home / "cfg"),))
    context = ToolContext(cwd=root)
    context.jail_bind = (str(operator_home / "cfg"),)

    def make_it():
        (config / "gh").mkdir()
        (config / "gh" / "key").write_text(SECRET)

    try:
        out = _while_running(context, root, f"cat {operator_home}/cfg/gh/key 2>/dev/null; true", make_it)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    assert SECRET not in out


def test_dash_C_starts_the_jail_with_the_configured_jail_hide(monkeypatch, tmp_path):
    seen = {}

    def fake_enter(directory, **kwargs):
        seen.update(kwargs)
        raise jail.JailError("stop here")

    monkeypatch.setattr(jail, "enter", fake_enter)
    monkeypatch.setenv("JS_JAIL_HIDE", "[]")
    work = tmp_path / "work"
    work.mkdir()

    assert cli.main(["-C", str(work), "-p", "hi"]) == 2
    assert seen["hide"] == []



def test_dash_C_reads_jail_hide_from_the_targets_env_file(monkeypatch, tmp_path):
    seen = {}

    def fake_enter(directory, **kwargs):
        seen.update(kwargs)
        raise jail.JailError("stop here")

    monkeypatch.setattr(jail, "enter", fake_enter)
    # Restored to unset afterwards: the .env load sets it in os.environ.
    monkeypatch.setenv("JS_JAIL_HIDE", "")
    monkeypatch.delenv("JS_JAIL_HIDE")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text(f'JS_JAIL_HIDE=["{work}"]\n', encoding="utf-8")

    assert cli.main(["-C", str(work), "-p", "hi"]) == 2
    assert seen["hide"] == [str(work)]



def test_dash_C_refuses_a_bad_setting_without_a_traceback(monkeypatch, tmp_path):
    entered = []
    monkeypatch.setattr(jail, "enter", lambda *a, **k: entered.append(a))
    work = tmp_path / "work"
    work.mkdir()

    assert cli.main(["-C", str(work), "--extra", "TOP_P=bad", "-p", "hi"]) == 2
    assert entered == []


_MOUNT_OPS = {"--bind", "--ro-bind", "--bind-try", "--ro-bind-try", "--tmpfs"}


def _mount_paths(argv: list[str]) -> list[str]:
    """Every source and target of a mount in bwrap ``argv``."""
    out: list[str] = []
    for index, arg in enumerate(argv):
        if arg == "--":
            break
        if arg == "--tmpfs":
            out.append(argv[index + 1])
        elif arg in _MOUNT_OPS:
            out += argv[index + 1:index + 3]
    return out


def _etc_dir() -> Path:
    return next(p for p in sorted(Path("/etc").iterdir()) if p.is_dir() and not p.is_symlink())


@needs_bwrap
@pytest.mark.parametrize("dir_place", ["outside", "in_hidden"])
@pytest.mark.parametrize("bind", [(), ("~",), ("~:rw",)])
@pytest.mark.parametrize("hides", [("~/.ssh",), ("~/.ssh", "~/.ssh/nested"), ("~/.ssh/nested", "~/.ssh")])
@pytest.mark.parametrize("nested_present", [True, False])
@pytest.mark.parametrize("network", ["none", "home", "etc"])
@pytest.mark.parametrize("outside_home", [False, True])
def test_the_mount_plan_hides_everything_under_a_hidden_path_and_never_mounts_a_network_mount(
        operator_home, tmp_path, monkeypatch, dir_place, bind, hides, nested_present, network, outside_home):
    ssh = operator_home / ".ssh"
    ssh.mkdir()
    (ssh / "key").write_text(SECRET)
    if nested_present:
        (ssh / "nested").mkdir()
        (ssh / "nested" / "key").write_text(SECRET)
    hide = [*hides, "~/missing-key"]
    # Host paths outside ~: a root-level directory, a file, and a symlink to one.
    outside = [p for p in (Path("/opt"), Path("/etc/hostname"), Path("/etc/os-release")) if p.exists()]
    if outside_home:
        hide += [str(p) for p in outside]
    mounts = jail._mounts()
    point = None
    if network == "home":
        point = operator_home / "nfs"
        point.mkdir()
        (point / "data").write_text("nfs")
    elif network == "etc":
        point = _etc_dir()
        hide.append("/etc/js-test-missing-key")
    if point is not None:
        monkeypatch.setattr(jail, "_mounts", lambda: [*mounts, jail._Mount("0:994", Path("/"), point, "nfs4")])
    if dir_place == "in_hidden":
        root = ssh / "project"
        root.mkdir()
        monkeypatch.chdir(root)
        with pytest.raises(jail.JailError):
            jail.enter(root, setting=bind, hide=hide)
        return
    root = Path(tempfile.mkdtemp(dir=tmp_path.parent))
    monkeypatch.chdir(root)
    before = _host_tree(operator_home, root, skip=("made-inside",))
    try:
        jail.enter(root, setting=bind, hide=hide)
        context = ToolContext(cwd=root)
        context.jail_bind = bind
        context.jail_hide = tuple(hide)

        if point is not None:
            assert str(point) not in _mount_paths(jail.wrap(["true"], context, cwd=root))
        assert _shell_ok(f"touch {root}/made-inside", context)
        for key in ssh.rglob("key"):
            _code, shown = run_shell(f"cat {key} 2>/dev/null; true", context)
            assert SECRET not in shown, (key, bind, hide)
        for path in outside if outside_home else ():
            empty = f'test -z "$(ls -A {path} 2>/dev/null)"' if path.is_dir() else f"test ! -s {path}"
            assert _shell_ok(empty, context), (path, bind, hide)
        assert _host_tree(operator_home, root, skip=("made-inside",)) == before
    finally:
        jail.leave()
        shutil.rmtree(root, ignore_errors=True)


_ARGV_BUDGET = 3000


def _ro_after_writable(argv: list[str], writable: list[Path], hidden: list[Path], empty: Path) -> list[Path]:
    """The read-only bind targets in bwrap ``argv`` at or under a writable
    root, mounted after that root's writable bind: they would make part of it
    read-only. A blank of a hidden path (the empty file, or inside a hidden
    path) is meant to be read-only and is left out."""
    out: list[Path] = []
    bound: list[Path] = []
    for index, arg in enumerate(argv):
        if arg == "--":
            break
        if arg in ("--bind", "--bind-try") and Path(argv[index + 2]) in writable:
            bound.append(Path(argv[index + 2]))
        elif arg in ("--ro-bind", "--ro-bind-try") and Path(argv[index + 1]) != empty:
            target = Path(argv[index + 2])
            if (any(jail._under(target, w) for w in bound)
                    and not any(jail._under(target, h) for h in hidden)):
                out.append(target)
    return out


@needs_bwrap
@pytest.mark.parametrize("dir_place", ["config", "project", "outside"])
@pytest.mark.parametrize("bind", [(), ("~",), ("~:rw",), ("~", "~/.config:rw")])
@pytest.mark.parametrize("big", ["none", "nested", "home"])
def test_the_mount_plan_keeps_writable_trees_writable_and_its_argv_bounded(
        operator_home, tmp_path, monkeypatch, dir_place, bind, big):
    config = operator_home / ".config"
    (config / "gh").mkdir(parents=True)
    (config / "gh" / "hosts.yml").write_text(SECRET)
    (config / "app" / "deep").mkdir(parents=True)
    (config / "app" / "deep" / "settings.toml").write_text("x")
    (operator_home / "project" / "src").mkdir(parents=True)
    (operator_home / "project" / "src" / "main.py").write_text("x")
    # Directories past any rebuild cutoff: nested under ~, and ~ itself.
    large = {"none": [], "nested": [config, config / "app"],
             "home": [operator_home, config, config / "app"]}[big]
    for parent in large:
        for i in range(4100):
            (parent / f"f{i:04d}").write_text("")
    root = {"config": config, "project": operator_home / "project",
            "outside": Path(tempfile.mkdtemp(dir=tmp_path.parent))}[dir_place]
    hide = [*_default_hides_for_test(), "~/missing-key"]
    monkeypatch.chdir(root)
    try:
        jail.enter(root, setting=bind, hide=hide)
        context = ToolContext(cwd=root)
        context.jail_bind = bind
        context.jail_hide = tuple(hide)
        argv = jail.wrap(["true"], context, cwd=root)
        writable = [root, *(jail._real(jail.parse_bind(b).path) for b in bind if jail.parse_bind(b).rw)]

        assert len(argv) < _ARGV_BUDGET, len(argv)
        hidden = jail.ACTIVE.hides(hide)
        assert _ro_after_writable(argv, writable, hidden, jail.ACTIVE.private / "empty") == [], bind
        for path in [p for p in root.rglob("*") if p.is_file() and p.parent.name != "gh"][:50]:
            assert _shell_ok(f"echo y >> {path}", context), (path, bind)
        _code, shown = run_shell(f"cat {config}/gh/hosts.yml 2>/dev/null; true", context)
        assert SECRET not in shown
    finally:
        jail.leave()
        if dir_place == "outside":
            shutil.rmtree(root, ignore_errors=True)


def _default_hides_for_test() -> list[str]:
    from js import settings

    return list(settings.default_value("JAIL_HIDE") or ())
