from __future__ import annotations

import hashlib
import io
import stat
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from js import tool_binaries
from js.toolkit.core import ToolContext


def _tar_gz(member: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _spec(archive: bytes, executable: bytes) -> tool_binaries.DownloadTool:
    return tool_binaries.DownloadTool(
        name="fixture-tool",
        executable="fixture",
        version="1.2.3",
        asset="fixture.tar.gz",
        url="https://example.test/fixture.tar.gz",
        asset_sha256=hashlib.sha256(archive).hexdigest(),
        archive_member="fixture-1.2.3/fixture",
        executable_sha256=hashlib.sha256(executable).hexdigest(),
    )


def _zip(member: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as bundle:
        bundle.writestr(member, content)
    return output.getvalue()


def test_install_download_verifies_and_installs_pinned_archive(tmp_path: Path) -> None:
    executable = b"#!/bin/sh\necho fixture 1.2.3\n"
    archive = _tar_gz("fixture-1.2.3/fixture", executable)
    spec = _spec(archive, executable)

    def download(url: str, destination: Path) -> None:
        assert url == spec.url
        destination.write_bytes(archive)

    state = tool_binaries.install_download(spec, tools_dir=tmp_path, downloader=download)
    installed = tmp_path / "fixture"

    assert state == "installed"
    assert installed.read_bytes() == executable
    assert installed.stat().st_mode & stat.S_IXUSR == stat.S_IXUSR


def test_install_download_rejects_corrupted_checksum(tmp_path: Path) -> None:
    executable = b"valid executable"
    archive = _tar_gz("fixture-1.2.3/fixture", executable)
    spec = replace(_spec(archive, executable), asset_sha256="0" * 64)

    def download(_url: str, destination: Path) -> None:
        destination.write_bytes(archive)

    with pytest.raises(tool_binaries.InstallError):
        tool_binaries.install_download(spec, tools_dir=tmp_path, downloader=download)


def test_install_download_extracts_pinned_zip_member(tmp_path: Path) -> None:
    executable = b"ast-grep fixture"
    archive = _zip("ast-grep", executable)
    spec = replace(
        _spec(archive, executable),
        executable="ast-grep",
        asset="app-x86_64-unknown-linux-gnu.zip",
        archive_member="ast-grep",
    )

    def download(_url: str, destination: Path) -> None:
        destination.write_bytes(archive)

    state = tool_binaries.install_download(spec, tools_dir=tmp_path, downloader=download)

    assert state == "installed"
    assert (tmp_path / "ast-grep").read_bytes() == executable


def test_install_download_is_idempotent_without_second_download(tmp_path: Path) -> None:
    executable = b"same executable every time"
    archive = _tar_gz("fixture-1.2.3/fixture", executable)
    spec = _spec(archive, executable)
    calls = 0

    def download(_url: str, destination: Path) -> None:
        nonlocal calls
        calls += 1
        destination.write_bytes(archive)

    first = tool_binaries.install_download(spec, tools_dir=tmp_path, downloader=download)
    second = tool_binaries.install_download(spec, tools_dir=tmp_path, downloader=download)

    assert (first, second, calls) == ("installed", "present", 1)


def test_resolve_binary_prefers_js_tools_before_path(tmp_path: Path, monkeypatch) -> None:
    managed = tmp_path / "rg"
    managed.write_text("managed ripgrep")
    managed.chmod(0o755)
    monkeypatch.setattr(tool_binaries, "TOOLS_DIR", tmp_path)
    monkeypatch.setattr(tool_binaries.shutil, "which", lambda _name: "/usr/bin/rg")

    assert tool_binaries.resolve_binary("rg") == str(managed)


def test_resolve_binary_falls_back_to_path_before_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tool_binaries, "TOOLS_DIR", tmp_path)
    monkeypatch.setattr(tool_binaries.shutil, "which", lambda name: f"/path/bin/{name}")

    assert tool_binaries.resolve_binary("rg") == "/path/bin/rg"


@pytest.mark.parametrize("environment", [
    {"TERMUX_VERSION": "0.118.3"},
    {"PREFIX": "/data/data/com.termux/files/usr"},
    {"PREFIX": "/data/user/0/com.termux/files/usr"},
])
def test_termux_plan_never_selects_desktop_artifacts(monkeypatch, environment):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(tool_binaries.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tool_binaries.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(tool_binaries.platform, "libc_ver", lambda: ("glibc", "2.40"))

    specs, missing = tool_binaries.release_plan()

    assert specs == []
    assert any("ast-grep" in item for item in missing)
    assert any("obscura" in item and "obscura-worker" in item for item in missing)


@pytest.mark.parametrize("failure", [None, "pkg", "timeout", "unavailable", "missing"])
def test_termux_install_uses_pkg_and_preserves_native_builds(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    prefix_bin = tmp_path / "usr" / "bin"
    prefix_bin.mkdir(parents=True)
    pkg = prefix_bin / "pkg"
    pkg.touch()
    tools = tmp_path / "managed"
    tools.mkdir()
    for name in ("aria2c", "bat", "fd", "fzf", "rg"):
        binary = prefix_bin / name
        binary.write_text("native package")
        binary.chmod(0o755)
        (tools / name).write_text("stale desktop binary")
    for name in ("ast-grep", "obscura", "obscura-worker"):
        (tools / name).write_text("owner's native build")
    monkeypatch.setattr(tool_binaries.shutil, "which", lambda name: str(pkg) if name == "pkg" and failure != "unavailable" else None)
    monkeypatch.setattr(tool_binaries.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: pytest.fail("desktop download"))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is True
        assert kwargs["timeout"] > 0
        if failure == "pkg":
            raise tool_binaries.subprocess.CalledProcessError(1, argv)
        if failure == "timeout":
            raise tool_binaries.subprocess.TimeoutExpired(argv, 600)
        if failure == "missing":
            (prefix_bin / "rg").unlink(missing_ok=True)

    monkeypatch.setattr(tool_binaries.subprocess, "run", run)
    for _ in range(2):
        with pytest.raises(tool_binaries.InstallError):
            tool_binaries.install_all(tools_dir=tools)
    expected = [str(pkg), "install", "-y", "aria2", "bat", "fd", "fzf", "ripgrep"]
    assert calls == ([] if failure == "unavailable" else [expected] * 2)
    if failure is None:
        for name in ("aria2c", "bat", "fd", "fzf", "rg"):
            assert (tools / name).is_symlink()
            assert (tools / name).resolve() == prefix_bin / name
    else:
        for name in ("aria2c", "bat", "fd", "fzf", "rg"):
            assert (tools / name).read_text() == "stale desktop binary"
    for name in ("ast-grep", "obscura", "obscura-worker"):
        assert (tools / name).read_text() == "owner's native build"


def test_platform_error_names_missing_assets(monkeypatch) -> None:
    monkeypatch.setattr(tool_binaries.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tool_binaries.platform, "machine", lambda: "arm64")
    specs, missing = tool_binaries.release_plan()
    assert {s.executable for s in specs} == {"rg", "fd", "bat", "fzf", "ast-grep"}
    assert any("aria2" in m and "Darwin aarch64" in m for m in missing)
    assert any("obscura" in m and "verification" in m for m in missing)


def test_obscura_is_pinned_to_the_stealth_release_asset() -> None:
    """The stealth build is the one that does TLS impersonation; browse depends
    on it, so the plain asset is not an acceptable substitute."""
    spec = next(tool for tool in tool_binaries.DOWNLOAD_TOOLS if tool.name == "obscura")

    assert spec.asset == "obscura-x86_64-linux-stealth.tar.gz"
    assert spec.url == (
        "https://github.com/h4ckf0r0day/obscura/releases/download/"
        f"v{spec.version}/{spec.asset}"
    )
    # Which version is pinned is a bump away; that both checksums are present
    # is what keeps the download verified.
    assert len(spec.asset_sha256) == 64
    assert len(spec.executable_sha256) == 64


def test_obscura_installs_the_worker_it_cannot_run_without(tmp_path: Path) -> None:
    """obscura spawns obscura-worker from its own directory. Installing the one
    binary leaves a tools/bin/obscura that resolve_binary returns and that then
    fails at render time."""
    import io
    import tarfile

    spec = next(tool for tool in tool_binaries.DOWNLOAD_TOOLS if tool.name == "obscura")
    main, worker = b"obscura-main", b"obscura-worker-body"

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in (("obscura", main), ("obscura-worker", worker)):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    archive_bytes = buffer.getvalue()

    def fake_download(_url: str, destination: Path) -> None:
        destination.write_bytes(archive_bytes)

    tools_dir = tmp_path / "tools"
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    spec = replace(
        spec,
        asset_sha256=archive_sha,
        executable_sha256=hashlib.sha256(main).hexdigest(),
        companions=(
            ("obscura-worker", "obscura-worker", hashlib.sha256(worker).hexdigest()),
        ),
    )

    state = tool_binaries.install_download(spec, tools_dir=tools_dir, downloader=fake_download)

    assert state == "installed"
    assert (tools_dir / "obscura").read_bytes() == main
    assert (tools_dir / "obscura-worker").read_bytes() == worker
    assert (tools_dir / "obscura-worker").stat().st_mode & stat.S_IXUSR == stat.S_IXUSR

    # An intact launcher must not mask a missing or damaged worker.
    for damage in (None, b"corrupt"):
        worker_path = tools_dir / "obscura-worker"
        if damage is None:
            worker_path.unlink()
        else:
            worker_path.write_bytes(damage)
        assert tool_binaries.install_download(
            spec, tools_dir=tools_dir, downloader=fake_download
        ) == "installed"
        assert worker_path.read_bytes() == worker
    assert tool_binaries.install_download(
        spec, tools_dir=tools_dir, downloader=lambda *_: pytest.fail("redundant download")
    ) == "present"


@pytest.mark.parametrize("machine", ["amd64", "arm64"])
def test_musl_plan_never_selects_glibc_binaries(monkeypatch, machine):
    monkeypatch.setattr(tool_binaries.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tool_binaries.platform, "machine", lambda: machine)
    monkeypatch.setattr(tool_binaries.platform, "libc_ver", lambda: ("musl", "1.2.5"))
    specs, missing = tool_binaries.release_plan()
    assert {s.executable for s in specs} == {"aria2c", "rg", "fd", "bat", "fzf"}
    assert any("ast-grep" in m for m in missing)
    assert any("obscura" in m for m in missing)


def test_rejects_tar_link_without_replacing_existing_binary(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        member = tarfile.TarInfo("fixture-1.2.3/fixture")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        bundle.addfile(member)
    archive = buffer.getvalue()
    spec = _spec(archive, b"new")
    target = tmp_path / "fixture"
    target.write_bytes(b"old")
    with pytest.raises(tool_binaries.InstallError):
        tool_binaries.install_download(
            spec, tools_dir=tmp_path, downloader=lambda _, p: p.write_bytes(archive)
        )
    assert target.read_bytes() == b"old"


def test_install_all_bootstraps_without_system_tools_and_reuses_managed_aria2(
    monkeypatch, tmp_path
):
    payload = b"managed binary"
    archive = _tar_gz("fixture-1.2.3/fixture", payload)
    aria = replace(_spec(archive, payload), executable="aria2c")
    helper = replace(_spec(archive, payload), executable="fd", asset="helper.tar.gz")
    monkeypatch.setattr(tool_binaries, "release_plan", lambda: ([aria, helper], []))
    monkeypatch.setenv("PATH", "")
    requests = []

    def urlopen(request, timeout):
        requests.append(request.full_url)
        assert timeout == 120
        return io.BytesIO(archive)

    transfers = []

    def transfer(binary, url, destination, **kwargs):
        assert binary == str(tmp_path / "aria2c")
        transfers.append(url)
        destination.write_bytes(archive)

    monkeypatch.setattr(tool_binaries.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(tool_binaries, "download_with_aria2", transfer)
    tool_binaries.install_all(tools_dir=tmp_path)
    tool_binaries.install_all(tools_dir=tmp_path)
    assert requests == [aria.url]
    assert transfers == [helper.url]
    assert (tmp_path / "fd").read_bytes() == payload

def test_shell_puts_the_managed_binary_directory_first_on_path(tmp_path, monkeypatch):
    """fd, bat and fzf are downloaded for a command to call by name."""
    from js.toolkit import process_net

    monkeypatch.setattr(process_net, "TOOLS_DIR", tmp_path / "bin")
    context = ToolContext(cwd=tmp_path)

    result = process_net.shell(command="printf %s \"$PATH\"", context=context)

    assert str(tmp_path / "bin") in result
    assert result.split("--- stdout ---")[-1].strip().startswith(str(tmp_path / "bin"))
