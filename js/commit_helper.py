"""Deterministic mechanics for the commit agent — survey + robust partial staging.

The commit agent's flaky parts are all DETERMINISTIC: surveying repo state (a
dozen `git status`/`diff`/`log`/`ls` round-trips) and splitting one file across
commits (blind, fragile `git add -p`). Both belong in code, not in the model.

Run from inside the target repo (cwd = the repo):

    python3 -m js.commit_helper survey
        One compact snapshot the agent reads once instead of probing: branch,
        porcelain status, the working diff with every hunk NUMBERED per file,
        untracked files, and recent log. Replaces the whole opening survey.

    python3 -m js.commit_helper stage <file> <hunks>
        Stage exactly the named hunks of one tracked file — `1,3` or `all` —
        via `git apply --cached --recount`. The durable way to split a mixed
        file across commits: no interactive `git add -p`, no positional blind
        input, and it NEVER runs `git checkout` (the only command that loses
        work). For an untracked file, `stage <file> all` does `git add <file>`.

Built on git plumbing (diff/apply), stable for ~20 years; ~90%+ of real cases.
Known gaps it deliberately punts (it tells you, never guesses): binary diffs,
pure renames, mode-only changes — for those, `git add <file>` the whole file.
"""

from __future__ import annotations

import subprocess
import sys


def _git(*args: str, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, input=stdin, check=False,
    )


def _porcelain() -> list[tuple[str, str]]:
    """[(XY, path), ...] from `git status --porcelain`."""
    out = _git("status", "--porcelain").stdout
    rows = []
    for line in out.splitlines():
        if not line:
            continue
        rows.append((line[:2], line[3:]))
    return rows


def _split_hunks(diff_text: str) -> tuple[str, list[str]]:
    """Return (file header, [hunk, ...]) for a single-file diff. Hunk = one @@ block."""
    lines = diff_text.splitlines(keepends=True)
    header, hunks, cur = [], [], None
    for ln in lines:
        if ln.startswith("@@"):
            if cur is not None:
                hunks.append("".join(cur))
            cur = [ln]
        elif cur is None:
            header.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        hunks.append("".join(cur))
    return "".join(header), hunks


def cmd_survey() -> int:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "(no commits yet)"
    print(f"=== SURVEY (cwd repo) ===\nbranch: {branch}")

    rows = _porcelain()
    if not rows:
        print("-- status --\n(clean tree, nothing to commit)")
        return 0

    print("-- status --")
    for xy, path in rows:
        print(f"{xy} {path}")

    tracked = [p for xy, p in rows if xy != "??"]
    untracked = [p for xy, p in rows if xy == "??"]

    print("\n-- working diff (hunks numbered per file; reference as `file N`) --")
    for path in tracked:
        diff = _git("diff", "--", path).stdout
        if not diff.strip():
            diff = _git("diff", "--cached", "--", path).stdout  # already-staged
        _, hunks = _split_hunks(diff)
        print(f"\n### {path}  ({len(hunks)} hunk{'s' if len(hunks) != 1 else ''})")
        if not hunks:
            print("(no text hunks — binary/rename/mode? stage the whole file)")
        for i, h in enumerate(hunks, 1):
            first = h.splitlines()[0]
            print(f"  --- hunk {i}: {first}")
            print(h.rstrip("\n"))

    if untracked:
        print("\n-- untracked --")
        for p in untracked:
            print(f"?? {p}  (new file — `stage {p} all` to add whole)")

    print("\n-- recent log --")
    print(_git("log", "--oneline", "-8").stdout.rstrip("\n") or "(no history)")
    return 0


def cmd_stage(path: str, spec: str) -> int:
    rows = dict((p, xy) for xy, p in _porcelain())
    xy = rows.get(path)
    if xy is None:
        print(f"error: '{path}' has no pending changes", file=sys.stderr)
        return 2

    if xy == "??" or spec == "all":
        r = _git("add", "--", path)
        if r.returncode != 0:
            print(r.stderr.strip() or "git add failed", file=sys.stderr)
            return 1
        print(f"staged whole file: {path}")
        return 0

    try:
        want = sorted({int(n) for n in spec.split(",") if n.strip()})
    except ValueError:
        print(f"error: hunks must be comma-separated numbers or 'all', got '{spec}'", file=sys.stderr)
        return 2

    diff = _git("diff", "--", path).stdout
    header, hunks = _split_hunks(diff)
    if not hunks:
        print(f"error: no text hunks in '{path}' (binary/rename/mode?) — use `stage {path} all`", file=sys.stderr)
        return 2
    bad = [n for n in want if n < 1 or n > len(hunks)]
    if bad:
        print(f"error: hunk(s) {bad} out of range; '{path}' has {len(hunks)}", file=sys.stderr)
        return 2

    patch = header + "".join(hunks[n - 1] for n in want)
    r = _git("apply", "--cached", "--recount", stdin=patch)
    if r.returncode != 0:
        print((r.stderr.strip() or "git apply failed") + f"\n(fallback: `stage {path} all`)", file=sys.stderr)
        return 1
    print(f"staged {path} hunk(s) {','.join(map(str, want))} of {len(hunks)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("survey", "stage"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "survey":
        return cmd_survey()
    if len(argv) != 3:
        print("usage: python3 -m js.commit_helper stage <file> <hunks|all>", file=sys.stderr)
        return 2
    return cmd_stage(argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(main())
