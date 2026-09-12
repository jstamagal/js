"""Pinned, integrity-checked binaries owned by this js checkout.

They live in ``tools/bin`` beside the source, the one directory .gitignore has
to cover, and ``shell`` puts it on PATH so a command can reach fd, bat and fzf
by name. Runtime callers prefer ``tools/bin/<name>`` and consult PATH only when
the installer has not populated that file. Keeping that fallback here makes the
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
import stat
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


TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools" / "bin"
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
    # Sidecar files the executable will not run without, as
    # (archive member, installed name, sha256). obscura spawns obscura-worker
    # from its own directory, so installing the one binary alone produces a
    # tools/bin/obscura that resolve_binary() happily returns and that then
    # fails at render time.
    companions: tuple[tuple[str, str, str], ...] = ()


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
    # The `stealth` asset, not the plain one: that build carries TLS
    # impersonation on top of the browser fingerprint, which is what browse
    # relies on to get one bot-detection verdict. The plain build would render
    # the same pages and lose the handshake.
    DownloadTool(
        name="obscura",
        executable="obscura",
        version="0.2.2",
        asset="obscura-x86_64-linux-stealth.tar.gz",
        url=(
            "https://github.com/h4ckf0r0day/obscura/releases/download/v0.2.2/"
            "obscura-x86_64-linux-stealth.tar.gz"
        ),
        asset_sha256="faf46c28948c10c6d44d6f46faad577adba43d63bb19b83cdb92a5e22bdd5da1",
        archive_member="obscura",
        executable_sha256="0e30b1ee35e3f3f291fed3cc55f7284964c8c741dcc24de1b171fcefa3444cdf",
        companions=(
            (
                "obscura-worker",
                "obscura-worker",
                "34edfd3c79c45f04e86a8bae6994956b06d611cb5951d9b497ef91f449541321",
            ),
        ),
    ),
)

ARIA2_VERSION = "1.37.0"
ARIA2_EXECUTABLE = "aria2c"

# Executables js provisions itself, keyed by name, with the pinned version the
# installer verifies. aria2c is the transfer engine every other download rides
# on, so it is installed first and never resolved through an unrelated PATH copy.
SYSTEM_TOOLS: dict[str, str] = {ARIA2_EXECUTABLE: ARIA2_VERSION}


def aria2_release(machine: str | None = None, system: str | None = None) -> DownloadTool:
    """Pinned static ELF releases, verified without executing foreign code.

    Both files have no PT_INTERP or dynamic section (readelf -l -d), so they
    require neither a musl loader nor a minimum glibc version.
    """
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    system = system or platform.system()
    hashes = {
        "x86_64": (
            "e0a09b12ef67f35f8a8e4fdddbec851d235b7c31da549d0578bff459032b499a",
            "80e577dc58348b96da46dd12d326bc99794b5021be395a3e890f2d67c8790c22",
        ),
        "aarch64": (
            "0c681a89a40e0f82d1f5137608e86257eb0af201459c002941ea098f2b8c26b6",
            "99a057bd383a28f1d5fab6e8cc5f6f3ed4172f5e65c59548242e965454d654c1",
        ),
    }
    if system != "Linux" or machine not in hashes:
        raise InstallError(f"aria2: no verified release asset for {system} {machine}")
    asset = f"aria2-{machine}-linux-musl_static.zip"
    archive_hash, executable_hash = hashes[machine]
    return DownloadTool(
        name="aria2", executable=ARIA2_EXECUTABLE, version=ARIA2_VERSION,
        asset=asset,
        url=f"https://github.com/abcfy2/aria2-static-build/releases/download/1.37.0/{asset}",
        asset_sha256=archive_hash, archive_member="aria2c",
        executable_sha256=executable_hash,
    )


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


def _extract_member(
    spec: DownloadTool, archive: Path, destination: Path, member_name: str
) -> None:
    if spec.asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            try:
                info = bundle.getinfo(member_name)
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_ISLNK(mode):
                    raise InstallError(f"{member_name} in {spec.asset} is not a regular file")
                source = bundle.open(member_name)
            except KeyError as exc:
                raise InstallError(
                    f"{spec.asset} did not contain pinned member {member_name}"
                ) from exc
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
        return
    if spec.asset.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as bundle:
            try:
                member = bundle.getmember(member_name)
            except KeyError as exc:
                raise InstallError(
                    f"{spec.asset} did not contain pinned member {member_name}"
                ) from exc
            if not member.isfile():
                raise InstallError(f"{member_name} in {spec.asset} is not a regular file")
            source = bundle.extractfile(member)
            if source is None:
                raise InstallError(f"{member_name} in {spec.asset} is not a regular file")
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
    downloader: Callable[[str, Path], None] | None = None,
) -> str:
    """Install one archive and return ``present`` or ``installed``."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    if downloader is None:
        downloader = _download
    target = tools_dir / spec.executable
    if _is_current(target, spec.executable_sha256) and all(
        _is_current(tools_dir / name, checksum)
        for _member, name, checksum in spec.companions
    ):
        return "present"

    with tempfile.TemporaryDirectory(prefix=f".{spec.executable}-", dir=tools_dir) as raw_temp:
        temp = Path(raw_temp)
        cache = tools_dir / ".archives"
        cache.mkdir(exist_ok=True)
        archive = cache / f"{spec.asset_sha256}-{spec.asset}"
        extracted = temp / spec.executable
        if not archive.is_file() or _sha256(archive) != spec.asset_sha256:
            staged_archive = temp / spec.asset
            downloader(spec.url, staged_archive)
            _verify(staged_archive, spec.asset_sha256, spec.asset)
            os.replace(staged_archive, archive)
        _verify(archive, spec.asset_sha256, spec.asset)
        _extract_member(spec, archive, extracted, spec.archive_member)
        _verify(extracted, spec.executable_sha256, f"{spec.name} executable")
        extracted.chmod(0o755)
        for member, installed_name, checksum in spec.companions:
            companion = temp / installed_name
            _extract_member(spec, archive, companion, member)
            _verify(companion, checksum, f"{spec.name} companion {installed_name}")
            companion.chmod(0o755)
        # Validate the entire release before publishing any of its files.
        for _member, installed_name, _checksum in spec.companions:
            os.replace(temp / installed_name, tools_dir / installed_name)
        os.replace(extracted, target)
    return "installed"


def release_plan() -> tuple[list[DownloadTool], list[str]]:
    """Select verified native assets; never substitute a compatibility layer."""
    system = platform.system()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    libc, version = platform.libc_ver()
    specs: list[DownloadTool] = []
    missing: list[str] = []
    try:
        specs.append(aria2_release(machine, system))
    except InstallError as exc:
        missing.append(str(exc))
    rows = json.loads(Path(__file__).with_name("tool_releases.json").read_text())
    for name in ("rg", "fd", "bat", "fzf", "ast-grep"):
        row = next((r for r in rows if r["executable"] == name
                    and r["system"] == system and r["machine"] == machine), None)
        if row is None:
            missing.append(f"{name}: no verified release asset for {system} {machine}")
            continue
        minimum = row["minimum_glibc"]
        if minimum and (libc != "glibc" or not version or
                        tuple(map(int, version.split("."))) < tuple(map(int, minimum.split(".")))):
            missing.append(f"{name}: {row['asset']} requires glibc >= {minimum}; "
                           f"found {libc or 'unknown libc'} {version}; no verified musl asset")
            continue
        specs.append(DownloadTool(**{k: v for k, v in row.items()
                                     if k not in ("system", "machine", "minimum_glibc")}))
    if system == "Linux" and machine == "x86_64" and libc == "glibc" and version and (
        tuple(map(int, version.split("."))) >= (2, 35)
    ):
        specs.append(next(s for s in DOWNLOAD_TOOLS if s.name == "obscura"))
    else:
        missing.append(f"obscura: no verified compatible release for {system} {machine} "
                       f"{libc} {version}; pinned x86_64 Linux asset requires glibc >= 2.35; "
                       "ARM64 Linux/macOS archives await content verification")
    return specs, missing


def _provision_aria2_via_brew(tools_dir: Path) -> str | None:
    """macOS: no verified static release exists, so aria2 comes from Homebrew.

    Returns the installed executable path, or None when brew cannot provide it.
    """
    brew = shutil.which("brew")
    if brew is None:
        return None
    candidate = shutil.which(ARIA2_EXECUTABLE)
    if candidate is None:
        try:
            subprocess.run([brew, "install", "aria2"], check=True, timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        candidate = shutil.which(ARIA2_EXECUTABLE)
    if candidate is None:
        prefix = subprocess.run([brew, "--prefix"], capture_output=True, text=True).stdout.strip()
        brewed = Path(prefix) / "bin" / ARIA2_EXECUTABLE
        candidate = str(brewed) if brewed.is_file() else None
    if candidate is None:
        return None
    tools_dir.mkdir(parents=True, exist_ok=True)
    target = tools_dir / ARIA2_EXECUTABLE
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(candidate)
    return str(target)


def install_all(*, tools_dir: Path = TOOLS_DIR) -> None:
    specs, missing = release_plan()

    if platform.system() == "Darwin" and not any(s.executable == ARIA2_EXECUTABLE for s in specs):
        brewed = _provision_aria2_via_brew(tools_dir)
        if brewed is not None:
            print(f"present: aria2 (Homebrew) at {brewed}")
            missing = [m for m in missing if not m.startswith("aria2:")]
        else:
            missing = [m for m in missing if not m.startswith("aria2:")] + [
                "aria2: Homebrew unavailable or `brew install aria2` failed; "
                "install Homebrew (https://brew.sh) and rerun `just install`"
            ]

    def download(url: str, destination: Path) -> None:
        managed = tools_dir / ARIA2_EXECUTABLE
        aria_spec = next((s for s in specs if s.executable == ARIA2_EXECUTABLE), None)
        if aria_spec is not None and _is_current(managed, aria_spec.executable_sha256):
            download_with_aria2(str(managed), url, destination, timeout_s=120,
                                headers={"User-Agent": "js-tool-installer/0.1"})
        else:
            # Bootstrap is independent of PATH (including unrelated system aria2).
            request = urllib.request.Request(url, headers={"User-Agent": "js-tool-installer/0.1"})
            with urllib.request.urlopen(request, timeout=120) as response:
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output)

    print(f"js tool directory: {tools_dir}")
    for spec in specs:
        target = tools_dir / spec.executable
        if _is_current(target, spec.executable_sha256) and all(
            _is_current(tools_dir / name, checksum)
            for _member, name, checksum in spec.companions
        ):
            print(
                f"present: {spec.name} {spec.version} at {target} "
                f"(sha256 {spec.executable_sha256})"
            )
            continue
        print(f"download: {spec.name} {spec.version} ({spec.asset})")
        print(f"  {spec.url}")
        state = install_download(spec, tools_dir=tools_dir, downloader=download)
        print(
            f"{state}: {target} (asset sha256 {spec.asset_sha256}; "
            f"executable sha256 {spec.executable_sha256})"
        )
    if missing:
        raise InstallError("toolkit incomplete:\n" + "\n".join(missing))

def main() -> int:
    try:
        install_all()
    except (DownloadError, InstallError, OSError, urllib.error.URLError) as exc:
        print(f"!! tool install failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
