"""Pinned, integrity-checked binaries owned by this js checkout.

Runtime callers prefer ``js/tools/<name>`` and consult PATH only when the
installer has not populated that file. Keeping that fallback here makes the
degraded behavior explicit while ensuring an installed tool is never resolved
through PATH (notably, ``sg`` on this box is not ast-grep).
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent / "tools"
MANUAL_OBSCURA_SOURCE = Path("/home/ronald_rump/.local/bin/obscura")


class InstallError(RuntimeError):
    """A tool could not be installed without compromising reproducibility."""


@dataclass(frozen=True)
class DownloadTool:
    name: str
    executable: str
    version: str
    asset: str
    url: str
    asset_sha256: str
    archive_member: str
    executable_sha256: str


# Versions and asset names were read from each project's GitHub release page
# and releases API on 2026-08-10. ripgrep's asset hash is from its published
# .sha256 file. ast-grep does not publish checksum files, so its archive and
# extracted executable hashes were computed from the pinned release asset.
DOWNLOAD_TOOLS = (
    DownloadTool(
        name="ripgrep",
        executable="rg",
        version="15.2.0",
        asset="ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz",
        url=(
            "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/"
            "ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz"
        ),
        asset_sha256="33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c",
        archive_member="ripgrep-15.2.0-x86_64-unknown-linux-musl/rg",
        executable_sha256="e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849",
    ),
    DownloadTool(
        name="ast-grep",
        executable="ast-grep",
        version="0.45.1",
        asset="app-x86_64-unknown-linux-gnu.zip",
        url=(
            "https://github.com/ast-grep/ast-grep/releases/download/0.45.1/"
            "app-x86_64-unknown-linux-gnu.zip"
        ),
        asset_sha256="76fb6555be6734fb5057dba8d2fb756430f374bb9e1af694cf1ce00e13238d63",
        archive_member="ast-grep",
        executable_sha256="6a66162e0a2447af4b7524ee04195239eb1911d07f4868f918909e7d4f453eea",
    ),
)

# obscura has no fetchable GitHub release. This is the exact owner-provided
# binary copied into js/tools after verification, rather than a network asset.
OBSCURA_VERSION = "0.2.0"
OBSCURA_SHA256 = "bde140f54b90bf064335a017780ae1d3bd33f69ccdbc7f954a63b5f43db7c723"

# The latest upstream release has Windows binaries, one Android aarch64 binary,
# and source archives. It has no Linux x86_64 aria2c asset, and js deliberately
# does not turn this installer into a from-source build system.
ARIA2_VERSION = "1.37.0"
ARIA2_RELEASE_URL = "https://github.com/aria2/aria2/releases/tag/release-1.37.0"


def resolve_binary(executable: str) -> str | None:
    """Return js's managed executable, falling back to PATH before install."""
    managed = TOOLS_DIR / executable
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    return shutil.which(executable)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, expected: str, label: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise InstallError(
            f"checksum mismatch for {label}: expected {expected}, got {actual}; "
            "refusing to install"
        )


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "js-tool-installer/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _extract_member(spec: DownloadTool, archive: Path, destination: Path) -> None:
    if spec.asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            try:
                source = bundle.open(spec.archive_member)
            except KeyError as exc:
                raise InstallError(
                    f"{spec.asset} did not contain pinned member {spec.archive_member}"
                ) from exc
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
        return
    if spec.asset.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as bundle:
            try:
                member = bundle.getmember(spec.archive_member)
            except KeyError as exc:
                raise InstallError(
                    f"{spec.asset} did not contain pinned member {spec.archive_member}"
                ) from exc
            source = bundle.extractfile(member)
            if source is None:
                raise InstallError(f"{spec.archive_member} in {spec.asset} is not a regular file")
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
        return
    raise InstallError(f"unsupported pinned archive format: {spec.asset}")


def _is_current(path: Path, expected_sha256: str) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK) and _sha256(path) == expected_sha256
    except OSError:
        return False


def install_download(
    spec: DownloadTool,
    *,
    tools_dir: Path = TOOLS_DIR,
    downloader: Callable[[str, Path], None] = _download,
) -> str:
    """Install one archive and return ``present`` or ``installed``."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    target = tools_dir / spec.executable
    if _is_current(target, spec.executable_sha256):
        return "present"

    with tempfile.TemporaryDirectory(prefix=f".{spec.executable}-", dir=tools_dir) as raw_temp:
        temp = Path(raw_temp)
        archive = temp / spec.asset
        extracted = temp / spec.executable
        downloader(spec.url, archive)
        _verify(archive, spec.asset_sha256, spec.asset)
        _extract_member(spec, archive, extracted)
        _verify(extracted, spec.executable_sha256, f"{spec.name} executable")
        extracted.chmod(0o755)
        os.replace(extracted, target)
    return "installed"


def install_obscura(
    *,
    tools_dir: Path = TOOLS_DIR,
    source: Path = MANUAL_OBSCURA_SOURCE,
) -> str:
    """Copy the pinned owner-provided obscura binary, or report it unavailable."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    target = tools_dir / "obscura"
    if _is_current(target, OBSCURA_SHA256):
        return "present"
    if not source.is_file():
        return "manual source unavailable"
    _verify(source, OBSCURA_SHA256, f"manual obscura source {source}")
    with tempfile.NamedTemporaryFile(prefix=".obscura-", dir=tools_dir, delete=False) as output:
        temporary = Path(output.name)
        with source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output)
    temporary.chmod(0o755)
    os.replace(temporary, target)
    return "copied"


def _require_supported_platform() -> None:
    system = platform.system()
    machine = platform.machine()
    if system != "Linux" or machine != "x86_64":
        raise InstallError(
            f"js tool binaries need Linux x86_64; found {system or 'unknown'} "
            f"{machine or 'unknown'}"
        )


def install_all(*, tools_dir: Path = TOOLS_DIR) -> None:
    _require_supported_platform()
    print(f"js tool directory: {tools_dir}")
    for spec in DOWNLOAD_TOOLS:
        target = tools_dir / spec.executable
        if _is_current(target, spec.executable_sha256):
            print(
                f"present: {spec.name} {spec.version} at {target} "
                f"(sha256 {spec.executable_sha256})"
            )
            continue
        print(f"download: {spec.name} {spec.version} ({spec.asset})")
        print(f"  {spec.url}")
        state = install_download(spec, tools_dir=tools_dir)
        print(
            f"{state}: {target} (asset sha256 {spec.asset_sha256}; "
            f"executable sha256 {spec.executable_sha256})"
        )

    obscura_state = install_obscura(tools_dir=tools_dir)
    if obscura_state == "manual source unavailable":
        print(
            f"manual: obscura {OBSCURA_VERSION} source is unavailable at "
            f"{MANUAL_OBSCURA_SOURCE}; js/tools/obscura was not populated"
        )
    else:
        print(
            f"{obscura_state}: obscura {OBSCURA_VERSION} at {tools_dir / 'obscura'} "
            f"from manual source {MANUAL_OBSCURA_SOURCE} (sha256 {OBSCURA_SHA256})"
        )
    print(
        f"unavailable: aria2c {ARIA2_VERSION} has no upstream Linux x86_64 binary; "
        f"not installed and not built from source ({ARIA2_RELEASE_URL})"
    )


def main() -> int:
    try:
        install_all()
    except (InstallError, OSError, urllib.error.URLError) as exc:
        print(f"!! tool install failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
