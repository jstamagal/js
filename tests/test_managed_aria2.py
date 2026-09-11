from dataclasses import replace
import hashlib
import io
import zipfile

import pytest

from js import tool_binaries as binaries


@pytest.mark.parametrize("machine,canonical", [
    ("amd64", "x86_64"), ("x86_64", "x86_64"),
    ("arm64", "aarch64"), ("aarch64", "aarch64"),
])
@pytest.mark.parametrize("libc", ["glibc", "musl"])
def test_static_aria2_selection_handles_both_linux_libcs(monkeypatch, machine, canonical, libc):
    monkeypatch.setattr(binaries.platform, "libc_ver", lambda: (libc, "1.0"))
    spec = binaries.aria2_release(machine, "Linux")
    assert spec.asset == f"aria2-{canonical}-linux-musl_static.zip"
    assert spec.archive_member == "aria2c"
    assert len(spec.executable_sha256) == 64


@pytest.mark.parametrize("system,machine", [
    ("Darwin", "arm64"), ("Windows", "amd64"), ("Linux", "riscv64"),
])
def test_aria2_reports_unverified_targets(system, machine):
    with pytest.raises(binaries.InstallError, match="aria2: no verified release asset"):
        binaries.aria2_release(machine, system)


def test_managed_aria2_install_does_not_require_path_binary(monkeypatch, tmp_path):
    payload = b"#!/bin/sh\necho managed\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("aria2c", payload)
    data = buffer.getvalue()
    spec = replace(binaries.aria2_release("amd64", "Linux"),
                   asset_sha256=hashlib.sha256(data).hexdigest(),
                   executable_sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(binaries, "TOOLS_DIR", tmp_path)
    calls = []

    def download(url, destination):
        calls.append(url)
        destination.write_bytes(data)

    assert binaries.install_download(spec, tools_dir=tmp_path, downloader=download) == "installed"
    assert binaries.resolve_binary("aria2c") == str(tmp_path / "aria2c")
    assert binaries.install_download(spec, tools_dir=tmp_path, downloader=download) == "present"
    assert calls == [spec.url]