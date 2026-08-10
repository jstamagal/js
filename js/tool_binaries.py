"""Pinned, integrity-checked binaries owned by this js checkout.

Runtime callers prefer ``js/tools/<name>`` and consult PATH only when the
installer has not populated that file. Keeping that fallback here makes the
degraded behavior explicit while ensuring an installed tool is never resolved
through PATH (notably, ``sg`` on this box is not ast-grep).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import warnings
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

ARIA2_VERSION = "1.37.0"
ARIA2_EXECUTABLE = "aria2c"
SYSTEM_TOOLS = {ARIA2_EXECUTABLE: ARIA2_VERSION}


class DownloadError(RuntimeError):
    """A byte transfer failed without publishing an incomplete destination."""


def resolve_binary(executable: str) -> str | None:
    """Return js's managed executable, falling back to PATH before install."""
    managed = TOOLS_DIR / executable
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    return shutil.which(executable)


def warn_urllib_fallback(purpose: str) -> None:
    """Make loss of aria2c's transfer guarantees visible without changing results."""
    warnings.warn(
        f"aria2c is unavailable; {purpose} is falling back to urllib without "
        "segmented transfer or cross-attempt resume",
        RuntimeWarning,
        stacklevel=2,
    )


def aria2_argv(
    binary: str,
    url: str,
    partial: Path,
    *,
    timeout_s: float,
    headers: dict[str, str] | None = None,
) -> list[str]:
    """Build the single-file aria2c invocation used by runtime and installer."""
    socket_timeout = max(1, min(30, math.ceil(timeout_s)))
    connect_timeout = max(1, min(10, math.ceil(timeout_s)))
    argv = [
        binary,
        "--continue=true",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--split=8",
        "--max-connection-per-server=8",
        "--min-split-size=1M",
        "--max-tries=5",
        "--retry-wait=1",
        f"--connect-timeout={connect_timeout}",
        f"--timeout={socket_timeout}",
        "--file-allocation=none",
        "--follow-torrent=false",
        "--follow-metalink=false",
        "--enable-color=false",
        "--console-log-level=warn",
        "--summary-interval=0",
        "--download-result=hide",
        f"--dir={partial.parent}",
        f"--out={partial.name}",
    ]
    argv.extend(f"--header={name}: {value}" for name, value in (headers or {}).items())
    argv.append(url)
    return argv


def _partial_download_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.aria2-part")


def _captured_tail(stream, limit: int = 500) -> str:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - limit))
    return stream.read().decode("utf-8", errors="replace").strip()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def download_with_aria2(
    binary: str,
    url: str,
    destination: Path,
    *,
    timeout_s: float,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    before_publish: Callable[[], None] | None = None,
) -> None:
    """Download to a resumable hidden partial and atomically publish on success."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_download_path(destination)
    control = Path(f"{partial}.aria2")
    metadata = Path(f"{partial}.js-meta")
    identity_payload = json.dumps(
        [url, sorted((headers or {}).items())], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    identity = hashlib.sha256(identity_payload).hexdigest()
    try:
        existing_identity = metadata.read_text(encoding="ascii").strip()
    except OSError:
        existing_identity = ""
    if existing_identity != identity:
        partial.unlink(missing_ok=True)
        control.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="ascii",
        prefix=f".{metadata.name}-",
        dir=destination.parent,
        delete=False,
    ) as stream:
        temporary_metadata = Path(stream.name)
        stream.write(identity)
    temporary_metadata.chmod(0o600)
    os.replace(temporary_metadata, metadata)
    argv = aria2_argv(binary, url, partial, timeout_s=timeout_s, headers=headers)
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(argv, stdout=stdout, stderr=stderr)
        except OSError as exc:
            raise DownloadError(f"could not start aria2c for {url}: {exc}") from exc
        while process.poll() is None:
            if max_bytes is not None and partial.exists() and partial.stat().st_size > max_bytes:
                _stop_process(process)
                partial.unlink(missing_ok=True)
                control.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                raise DownloadError(f"response exceeds {max_bytes} byte download limit")
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                _stop_process(process)
                raise DownloadError(f"aria2c timed out after {timeout_s:g}s downloading {url}")
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass

        if process.returncode != 0:
            captured = _captured_tail(stderr) or _captured_tail(stdout)
            detail = " ".join(captured.split()) if captured else "no diagnostic output"
            raise DownloadError(
                f"aria2c exited {process.returncode} downloading {url}: {detail}"
            )
    if not partial.is_file():
        raise DownloadError(f"aria2c exited 0 but wrote no file downloading {url}")
    size = partial.stat().st_size
    if max_bytes is not None and size > max_bytes:
        partial.unlink(missing_ok=True)
        control.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        raise DownloadError(f"response exceeds {max_bytes} byte download limit")
    if before_publish is not None:
        before_publish()
    os.replace(partial, destination)
    control.unlink(missing_ok=True)
    metadata.unlink(missing_ok=True)


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
    aria2c = resolve_binary(ARIA2_EXECUTABLE)
    if aria2c is not None:
        download_with_aria2(
            aria2c,
            url,
            destination,
            timeout_s=120,
            headers={"User-Agent": "js-tool-installer/0.1"},
        )
        return
    warn_urllib_fallback("the tool-binary installer")
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
    for executable, version in SYSTEM_TOOLS.items():
        binary = resolve_binary(executable)
        if binary is None:
            print(
                f"system: {executable} {version} is unavailable; downloads will visibly "
                "fall back to urllib"
            )
        else:
            print(f"system: {executable} {version} at {binary}")


def main() -> int:
    try:
        install_all()
    except (DownloadError, InstallError, OSError, urllib.error.URLError) as exc:
        print(f"!! tool install failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
