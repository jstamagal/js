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

Built on git plumbing, diff and apply, which has been stable for about 20
years. Binary diffs, pure renames and mode-only changes have no text hunks; the
helper says so and the whole file is staged instead.

Every line it prints is an entry in `js/messages.py`, printed with `.text()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

from . import messages as msgs


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
        return _git_failure_message(self)


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
            # -c color.ui=false: every caller here parses plumbing output, and
            # an operator with color.ui=always (or GIT_CONFIG_PARAMETERS
            # carrying it) gets ANSI-wrapped lines that no longer start with
            # "@@" or a status code. An explicit -c outranks both config and
            # GIT_CONFIG_PARAMETERS, so parsing sees plain text regardless.
            ["git", "-c", "color.ui=false", "-C", str(workdir), *args],
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
    detail = (exc.stderr or exc.stdout).strip() or msgs.GIT_EXIT.text(code=exc.returncode)
    if exc.kind == "not-a-repo":
        return msgs.GIT_NOT_A_REPO.text(repo=exc.repo)
    return msgs.GIT_FAILED.text(repo=exc.repo, argv=" ".join(exc.argv), detail=detail)


def _out(message: msgs.Message, **fields) -> None:
    print(message.text(**fields))


def _err(message: msgs.Message, **fields) -> None:
    print(message.text(**fields), file=sys.stderr)


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
        return msgs.SURVEY_DETACHED.text(sha=short.stdout.strip())
    symbolic = _git("symbolic-ref", "--short", "HEAD", check=False, repo=repo)
    return symbolic.stdout.strip() or msgs.SURVEY_NO_COMMITS.text()


def _tracked_paths(rows: list[tuple[str, str]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for xy, path in rows:
        if xy == "??" or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _print_diff_section(repo: Path, paths: list[str], *, cached: bool) -> None:
    _out(msgs.SURVEY_STAGED if cached else msgs.SURVEY_UNSTAGED)
    any_diff = False
    for path in paths:
        args = ("diff", "--cached", "--", path) if cached else ("diff", "--", path)
        diff = _git(*args, repo=repo).stdout
        if not diff.strip():
            continue
        any_diff = True
        _, hunks = _split_hunks(diff)
        _out(msgs.SURVEY_FILE, path=path, hunks=msgs.plural(len(hunks), "hunk"))
        if not hunks:
            _out(msgs.SURVEY_NO_TEXT_HUNKS)
        for i, h in enumerate(hunks, 1):
            _out(msgs.SURVEY_HUNK, index=i, header=h.splitlines()[0])
            print(h.rstrip("\n"))
    if not any_diff:
        _out(msgs.SURVEY_NONE)


def cmd_survey(repo: str | Path | None = None) -> int:
    repo_path = _repo_path(repo)
    try:
        branch = _branch_name(repo_path)
        rows = _porcelain(repo_path)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 2 if exc.kind == "not-a-repo" else 1

    _out(msgs.SURVEY_HEADING, repo=repo_path)
    _out(msgs.SURVEY_BRANCH, branch=branch)

    _out(msgs.SURVEY_STATUS)
    if rows:
        for xy, path in rows:
            _out(msgs.SURVEY_STATUS_ROW, xy=xy, path=path)
    else:
        _out(msgs.SURVEY_CLEAN)

    tracked = _tracked_paths(rows)
    untracked = [p for xy, p in rows if xy == "??"]

    try:
        _print_diff_section(repo_path, tracked, cached=True)
        _print_diff_section(repo_path, tracked, cached=False)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 1

    _out(msgs.SURVEY_UNTRACKED)
    for p in untracked:
        _out(msgs.SURVEY_UNTRACKED_ROW, path=p)
    if not untracked:
        _out(msgs.SURVEY_NONE)

    _out(msgs.SURVEY_LOG)
    log = _git("log", "--oneline", "-8", check=False, repo=repo_path)
    if log.returncode == 0 and log.stdout.strip():
        print(log.stdout.rstrip("\n"))
    else:
        _out(msgs.SURVEY_NO_HISTORY)
    return 0


def _stage_whole(path: str, repo: Path) -> int:
    try:
        _git("add", "--", path, repo=repo)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 1
    _out(msgs.STAGED_WHOLE, path=path)
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
        _err(msgs.STAGE_NO_CHANGES, path=path)
        return 2

    if xy == "??":
        if spec != "all":
            _err(msgs.STAGE_UNTRACKED, path=path)
            return 2
        return _stage_whole(path, repo_path)

    if spec == "all":
        return _stage_whole(path, repo_path)

    want = _wanted_hunks(spec)
    if want is None:
        _err(msgs.STAGE_BAD_SPEC, spec=spec)
        return 2

    try:
        diff = _git("diff", "--", path, repo=repo_path).stdout
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 1
    header, hunks = _split_hunks(diff)
    if not hunks:
        _err(msgs.STAGE_NO_HUNKS, path=path)
        return 2
    bad = [n for n in want if n < 1 or n > len(hunks)]
    if bad:
        _err(msgs.STAGE_OUT_OF_RANGE, bad=", ".join(map(str, bad)), path=path, count=len(hunks))
        return 2

    patch = header + "".join(hunks[n - 1] for n in want)
    try:
        _git("apply", "--cached", "--recount", stdin=patch, repo=repo_path)
    except GitCommandError as exc:
        detail = (exc.stderr or exc.stdout).strip() or msgs.GIT_APPLY_FAILED.text()
        _err(msgs.STAGE_APPLY_FAILED, detail=detail, path=path)
        return 1
    _out(msgs.STAGED_HUNKS, path=path, hunks=",".join(map(str, want)), total=len(hunks))
    return 0


def cmd_commit(message_file: str, repo: str | Path | None = None, *, amend: bool = False) -> int:
    """Commit staged changes with the message read VERBATIM from a file.

    The message never transits a shell. Composed as `git commit -m "..."`
    through `bash -c`, a message body containing backticks is command
    substitution — one that described a `yes` bug executed yes(1) and got the
    box OOM-killed three times. Prose goes in a file; only the path rides argv.
    """
    repo_path = _repo_path(repo)
    msg_path = Path(message_file).expanduser()
    if not msg_path.is_absolute():
        msg_path = repo_path / msg_path
    try:
        message = msg_path.read_text(encoding="utf-8")
    except OSError as exc:
        _err(msgs.MESSAGE_FILE_UNREADABLE, error=exc)
        return 2
    if not message.strip():
        _err(msgs.MESSAGE_FILE_EMPTY, path=msg_path)
        return 2
    # cleanup=whitespace keeps `#`-prefixed lines (markdown headers in bodies).
    args = ["commit", "--file", str(msg_path), "--cleanup=whitespace"]
    if amend:
        args.append("--amend")
    try:
        proc = _git(*args, repo=repo_path)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 1
    if proc.stdout.strip():
        print(proc.stdout.strip())
    else:
        _out(msgs.COMMITTED)
    return 0


def _extract_repo(argv: list[str]) -> tuple[Path | None, list[str], str | None]:
    repo: Path | None = None
    rest = list(argv)
    while rest:
        arg = rest[0]
        if arg in ("-C", "--repo"):
            if len(rest) < 2:
                return repo, rest, msgs.COMMIT_HELPER_USAGE.text(usage=f"{arg} <dir> <survey|stage|commit ...>")
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
    if not argv or argv[0] not in ("survey", "stage", "commit"):
        _out(msgs.COMMIT_HELPER_HELP)
        return 0 if argv else 2
    if argv[0] == "survey":
        return cmd_survey(repo)
    if argv[0] == "commit":
        rest = [a for a in argv[1:] if a != "--amend"]
        amend = "--amend" in argv[1:]
        if len(rest) != 1:
            _err(msgs.COMMIT_HELPER_USAGE, usage="[-C DIR|--repo DIR] commit <message-file> [--amend]")
            return 2
        return cmd_commit(rest[0], repo, amend=amend)
    if len(argv) != 3:
        _err(msgs.COMMIT_HELPER_USAGE, usage="[-C DIR|--repo DIR] stage <file> <hunks|all>")
        return 2
    return cmd_stage(argv[1], argv[2], repo)


if __name__ == "__main__":
    raise SystemExit(main())
