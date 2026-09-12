"""Re-pin js's managed binaries against each project's latest release.

The pins in ``js/tool_releases.json`` and the obscura spec in
``js/tool_binaries.py`` name an exact asset and its checksums, so bumping one
by hand means downloading the asset, hashing it, opening the archive and
hashing the member. Nothing checked them, so they drifted years behind. This
does the arithmetic for every platform, including ones this machine cannot
run, and reports what moved.

    uv run python scripts/refresh_tool_releases.py --check    # report only
    uv run python scripts/refresh_tool_releases.py            # rewrite pins
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASES = ROOT / "js" / "tool_releases.json"
REPOS = {
    "ripgrep": "BurntSushi/ripgrep",
    "fd": "sharkdp/fd",
    "bat": "sharkdp/bat",
    "fzf": "junegunn/fzf",
    "ast-grep": "ast-grep/ast-grep",
}


def latest_tag(repo: str) -> str:
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def member_bytes(asset: str, blob: bytes, member: str) -> bytes:
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            return archive.read(member)
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        extracted = archive.extractfile(member)
        if extracted is None:
            raise KeyError(member)
        return extracted.read()


def retarget(text: str, old: str, new: str) -> str:
    """Swap a version inside an asset name, url or archive member path."""
    return text.replace(old, new) if old in text else text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report drift without rewriting")
    args = parser.parse_args()

    entries = json.loads(RELEASES.read_text())
    pinned = {entry["name"]: entry["version"] for entry in entries}
    latest = {name: latest_tag(repo).lstrip("v") for name, repo in REPOS.items()}

    drift = {name: (pinned[name], latest[name]) for name in pinned if pinned[name] != latest[name]}
    for name, (was, now) in sorted(drift.items()):
        print(f"stale: {name} {was} -> {now}")
    for name in sorted(set(pinned) - set(drift)):
        print(f"current: {name} {pinned[name]}")
    if args.check or not drift:
        return 1 if drift else 0

    for entry in entries:
        name = entry["name"]
        if name not in drift:
            continue
        was, now = drift[name]
        asset = retarget(entry["asset"], was, now)
        url = retarget(entry["url"], was, now)
        member = retarget(entry["archive_member"], was, now)
        print(f"download: {name} {now} ({asset})")
        blob = fetch(url)
        try:
            executable = member_bytes(asset, blob, member)
        except KeyError:
            print(f"  !! {member!r} is not in {asset}; pin it by hand", file=sys.stderr)
            return 2
        entry.update(
            version=now, asset=asset, url=url, archive_member=member,
            asset_sha256=hashlib.sha256(blob).hexdigest(),
            executable_sha256=hashlib.sha256(executable).hexdigest(),
        )
        print(f"  asset {entry['asset_sha256']}\n  exe   {entry['executable_sha256']}")

    RELEASES.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"rewrote {RELEASES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
