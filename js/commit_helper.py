"""Deterministic mechanics for the commit agent — survey + robust partial staging.

The commit agent's flaky parts are all deterministic: surveying repo state (branch,
porcelain status, staged/unstaged diffs, untracked files, recent log) and
splitting one file across commits. Both belong in code, not in the model.

Run against the target repo explicitly; the helper never depends on the caller's
process cwd when ``-C``/``--repo`` is supplied:

    python3 -m js.commit_helper -C /path/to/repo survey
        One compact snapshot the agent reads once instead of probing: branch,
        porcelain status, staged and unstaged text diffs with every hunk numbered
        per file, untracked files, and recent log.

    python3 -m js.commit_helper -C /path/to/repo stage <file> <hunks>
        Stage exactly the named unstaged hunks of one tracked file — ``1,3`` or
        ``all`` — via ``git apply --cached --recount``. For an untracked file,
        only ``stage <file> all`` is valid and does ``git add <file>``.

Built on git plumbing (diff/apply), stable for ~20 years; ~90%+ of real cases.
Known gaps it deliberately punts (it tells you, never guesses): binary diffs,
pure renames, mode-only changes — for those, stage the whole file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys


@dataclass(slots=True)
class GitCommandError(RuntimeError):
    """A checked git command failed."""

    argv: tuple[str, ...]
    repo: Path
    returncode: int
    stdout: str
    stderr: str
    kind: str

    def __str__(self) -> str:
        detail = (self.stderr or self.stdout).strip() or f"exit {self.returncode}"
        return f"{self.kind}: git {' '.join(self.argv)} failed in {self.repo}: {detail}"


def _repo_path(repo: str | Path | None = None) -> Path:
    if repo is None:
        return Path.cwd().resolve(strict=False)
    return Path(repo).expanduser().resolve(strict=False)


def _classify_failure(proc: subprocess.CompletedProcess) -> str:
    text = f"{proc.stderr}\n{proc.stdout}".lower()
    if "not a git repository" in text or "not in a git directory" in text:
        return "not-a-repo"
    return "git-failed"


def _git(
    *args: str,
    check: bool = True,
    stdin: str | None = None,
    repo: str | Path | None = None,
) -> subprocess.CompletedProcess:
    workdir = _repo_path(repo)
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            input=stdin,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitCommandError(tuple(args), workdir, 127, "", "git executable not found", "git-failed") from exc
    if check and proc.returncode != 0:
        raise GitCommandError(
            tuple(args),
            workdir,
            proc.returncode,
            proc.stdout,
            proc.stderr,
            _classify_failure(proc),
        )
    return proc


def _git_failure_message(exc: GitCommandError) -> str:
    detail = (exc.stderr or exc.stdout).strip() or f"exit {exc.returncode}"
    if exc.kind == "not-a-repo":
        return f"error: not a git repository: {exc.repo}"
    return f"error: git failed in {exc.repo}: git {' '.join(exc.argv)}: {detail}"


def _porcelain(repo: str | Path | None = None) -> list[tuple[str, str]]:
    """[(XY, path), ...] from ``git status --porcelain``."""
    out = _git("status", "--porcelain", repo=repo).stdout
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


def _branch_name(repo: str | Path | None = None) -> str:
    current = _git("branch", "--show-current", repo=repo).stdout.strip()
    if current:
        return current
    short = _git("rev-parse", "--short", "HEAD", check=False, repo=repo)
    if short.returncode == 0 and short.stdout.strip():
        return f"(detached HEAD at {short.stdout.strip()})"
    symbolic = _git("symbolic-ref", "--short", "HEAD", check=False, repo=repo)
    return symbolic.stdout.strip() or "(no commits yet)"


def _tracked_paths(rows: list[tuple[str, str]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for xy, path in rows:
        if xy == "??" or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _print_diff_section(repo: Path, title: str, paths: list[str], *, cached: bool) -> None:
    print(f"\n-- {title} (hunks numbered per file; reference as `stage <file> <n[,n]|all>`) --")
    any_diff = False
    for path in paths:
        args = ("diff", "--cached", "--", path) if cached else ("diff", "--", path)
        diff = _git(*args, repo=repo).stdout
        if not diff.strip():
            continue
        any_diff = True
        _, hunks = _split_hunks(diff)
        print(f"\n### {path}  ({len(hunks)} hunk{'s' if len(hunks) != 1 else ''})")
        if not hunks:
            print("(no text hunks — binary/rename/mode? stage the whole file)")
        for i, h in enumerate(hunks, 1):
            first = h.splitlines()[0]
            print(f"  --- hunk {i}: {first}")
            print(h.rstrip("\n"))
    if not any_diff:
        print("(none)")


def cmd_survey(repo: str | Path | None = None) -> int:
    repo_path = _repo_path(repo)
    try:
        branch = _branch_name(repo_path)
        rows = _porcelain(repo_path)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 2 if exc.kind == "not-a-repo" else 1

    print(f"=== SURVEY ({repo_path}) ===\nbranch: {branch}")

    print("-- status --")
    if rows:
        for xy, path in rows:
            print(f"{xy} {path}")
    else:
        print("(clean tree, nothing to commit)")

    tracked = _tracked_paths(rows)
    untracked = [p for xy, p in rows if xy == "??"]

    try:
        _print_diff_section(repo_path, "staged diff", tracked, cached=True)
        _print_diff_section(repo_path, "unstaged diff", tracked, cached=False)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 1

    if untracked:
        print("\n-- untracked --")
        for p in untracked:
            print(f"?? {p}  (new file — `stage {p} all` to add whole)")
    else:
        print("\n-- untracked --\n(none)")

    print("\n-- recent log --")
    log = _git("log", "--oneline", "-8", check=False, repo=repo_path)
    if log.returncode == 0 and log.stdout.strip():
        print(log.stdout.rstrip("\n"))
    else:
        print("(no history)")
    return 0


def _stage_whole(path: str, repo: Path) -> int:
    try:
        _git("add", "--", path, repo=repo)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 1
    print(f"staged whole file: {path}")
    return 0


def _wanted_hunks(spec: str) -> list[int] | None:
    try:
        want = sorted({int(n) for n in spec.split(",") if n.strip()})
    except ValueError:
        return None
    return want or None


def cmd_stage(path: str, spec: str, repo: str | Path | None = None) -> int:
    repo_path = _repo_path(repo)
    try:
        rows = dict((p, xy) for xy, p in _porcelain(repo_path))
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 2 if exc.kind == "not-a-repo" else 1

    xy = rows.get(path)
    if xy is None:
        print(f"error: '{path}' has no pending changes", file=sys.stderr)
        return 2

    if xy == "??":
        if spec != "all":
            print(
                f"error: '{path}' is untracked; use `stage {path} all` (hunk specs only work for tracked text diffs)",
                file=sys.stderr,
            )
            return 2
        return _stage_whole(path, repo_path)

    if spec == "all":
        return _stage_whole(path, repo_path)

    want = _wanted_hunks(spec)
    if want is None:
        print(f"error: hunks must be comma-separated numbers or 'all', got '{spec}'", file=sys.stderr)
        return 2

    try:
        diff = _git("diff", "--", path, repo=repo_path).stdout
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 1
    header, hunks = _split_hunks(diff)
    if not hunks:
        print(f"error: no unstaged text hunks in '{path}' (binary/rename/mode/already staged?) — use `stage {path} all`", file=sys.stderr)
        return 2
    bad = [n for n in want if n < 1 or n > len(hunks)]
    if bad:
        print(f"error: hunk(s) {bad} out of range; '{path}' has {len(hunks)}", file=sys.stderr)
        return 2

    patch = header + "".join(hunks[n - 1] for n in want)
    try:
        _git("apply", "--cached", "--recount", stdin=patch, repo=repo_path)
    except GitCommandError as exc:
        detail = (exc.stderr or exc.stdout).strip() or "git apply failed"
        print(f"{detail}\n(fallback: `stage {path} all`)", file=sys.stderr)
        return 1
    print(f"staged {path} hunk(s) {','.join(map(str, want))} of {len(hunks)}")
    return 0


def _extract_repo(argv: list[str]) -> tuple[Path | None, list[str], str | None]:
    repo: Path | None = None
    rest = list(argv)
    while rest:
        arg = rest[0]
        if arg in ("-C", "--repo"):
            if len(rest) < 2:
                return repo, rest, f"usage: python3 -m js.commit_helper {arg} <dir> <survey|stage ...>"
            repo = _repo_path(rest[1])
            rest = rest[2:]
            continue
        if arg.startswith("--repo="):
            repo = _repo_path(arg.split("=", 1)[1])
            rest = rest[1:]
            continue
        break
    return repo, rest, None


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    repo, argv, error = _extract_repo(raw)
    if error:
        print(error, file=sys.stderr)
        return 2
    if not argv or argv[0] not in ("survey", "stage"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "survey":
        return cmd_survey(repo)
    if len(argv) != 3:
        print("usage: python3 -m js.commit_helper [-C DIR|--repo DIR] stage <file> <hunks|all>", file=sys.stderr)
        return 2
    return cmd_stage(argv[1], argv[2], repo)


if __name__ == "__main__":
    raise SystemExit(main())
