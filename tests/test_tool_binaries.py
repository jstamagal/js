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

    with pytest.raises(tool_binaries.InstallError, match="checksum mismatch.*refusing to install"):
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


def test_platform_error_names_required_and_detected_platform(monkeypatch) -> None:
    monkeypatch.setattr(tool_binaries.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tool_binaries.platform, "machine", lambda: "arm64")

    with pytest.raises(
        tool_binaries.InstallError,
        match="js tool binaries need Linux x86_64; found Darwin arm64",
    ):
        tool_binaries._require_supported_platform()


def test_manual_obscura_copy_is_verified_and_executable(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "owner-obscura"
    source.write_bytes(b"pinned manual obscura")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(tool_binaries, "OBSCURA_SHA256", checksum)

    state = tool_binaries.install_obscura(tools_dir=tools_dir, source=source)
    installed = tools_dir / "obscura"

    assert state == "copied"
    assert installed.read_bytes() == b"pinned manual obscura"
    assert installed.stat().st_mode & stat.S_IXUSR == stat.S_IXUSR
