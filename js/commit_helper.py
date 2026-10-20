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


# Every caller here parses git output, so operator config that reshapes it is
# overridden on the command line, which outranks config files and
# GIT_CONFIG_PARAMETERS. color.diff/color.status are more specific than
# color.ui and win over it, so all three are pinned. diff.noprefix,
# diff.mnemonicPrefix and diff.relative change the `a/` `b/` headers that
# `git apply` needs; suppressBlankEmpty drops the space on blank context lines.
# --literal-pathspecs: a path operand is a file name, never a glob or `:(magic)`,
# so `stage '*.txt' all` cannot stage other files.
_GIT_OVERRIDES = (
    "-c", "color.ui=false",
    "-c", "color.diff=false",
    "-c", "color.status=false",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.relative=false",
    "-c", "diff.suppressBlankEmpty=false",
    "--literal-pathspecs",
)

# Per-call diff flags for the diffs this helper parses and re-applies. External
# diff drivers and textconv filters print text `git apply` cannot take back.
_DIFF_FLAGS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-relative",
    "--unified=3",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def _git(
    *args: str,
    check: bool = True,
    stdin: str | None = None,
    repo: str | Path | None = None,
) -> subprocess.CompletedProcess:
    workdir = _repo_path(repo)
    try:
        # Bytes in, bytes out, decoded here: text mode would translate
        # newlines (CRLF content and CR in paths lost). surrogateescape both
        # ways, so a latin-1 file's hunk goes back to `git apply` as the same
        # bytes.
        raw = subprocess.run(
            ["git", *_GIT_OVERRIDES, "-C", str(workdir), *args],
            capture_output=True,
            input=None if stdin is None else stdin.encode("utf-8", "surrogateescape"),
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitCommandError(tuple(args), workdir, 127, "", "git executable not found", "git-failed") from exc
    proc = subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode("utf-8", "surrogateescape"),
        raw.stderr.decode("utf-8", "surrogateescape"),
    )
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
    detail = (exc.stderr or exc.stdout).strip() or msgs.GIT_EXIT.said(code=exc.returncode)
    if exc.kind == "not-a-repo":
        return msgs.GIT_NOT_A_REPO.said(repo=exc.repo)
    return msgs.GIT_FAILED.said(repo=exc.repo, argv=" ".join(exc.argv), detail=detail)


def _printable(text: str) -> str:
    """Text safe for a strict UTF-8 stdout: undecodable bytes become U+FFFD."""
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def _print(text: str) -> None:
    print(_printable(text))


def _out(message: msgs.Message, **fields) -> None:
    _print(message.text(**fields))


def _err(message: msgs.Message, **fields) -> None:
    print(message.text(**fields), file=sys.stderr)


@dataclass(frozen=True, slots=True)
class StatusRow:
    """One ``git status`` entry. ``orig`` is the source path of a rename or copy."""

    xy: str
    path: str
    orig: str | None = None

    def shown(self) -> str:
        return f"{self.orig} -> {self.path}" if self.orig else self.path


def _toplevel(repo: str | Path | None = None) -> Path:
    """The work tree root. Status paths are root-relative, so every git call
    that takes them back runs from here, whatever directory ``-C`` named."""
    # Exactly one trailing LF is git's delimiter; anything else is the name.
    top = _git("rev-parse", "--show-toplevel", repo=repo).stdout.removesuffix("\n")
    return Path(top) if top else _repo_path(repo)


def _porcelain(repo: str | Path | None = None) -> list[StatusRow]:
    """Rows from ``git status --porcelain=v1 -z -uall``.

    ``-z`` gives raw, unquoted, root-relative paths and puts a rename's source
    in its own field; ``-uall`` lists the files inside untracked directories
    instead of ``newdir/``.
    """
    out = _git("status", "--porcelain=v1", "-z", "-uall", repo=repo).stdout
    fields = out.split("\0")
    rows: list[StatusRow] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        orig = None
        if "R" in xy or "C" in xy:
            orig = fields[i] if i < len(fields) else None
            i += 1
        rows.append(StatusRow(xy, path, orig))
    return rows


def _split_hunks(diff_text: str) -> tuple[str, list[str]]:
    """Return (file header, [hunk, ...]) for a single-file diff. Hunk = one @@ block."""
    # Split on LF only: splitlines() also breaks on CR, U+2028, \x0c and
    # friends inside a changed line, and a fragment starting "@@" would
    # become a fake hunk.
    lines = diff_text.split("\n")
    lines = [ln + "\n" for ln in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
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
        return msgs.SURVEY_DETACHED.said(sha=short.stdout.strip())
    symbolic = _git("symbolic-ref", "--short", "HEAD", check=False, repo=repo)
    return symbolic.stdout.strip() or msgs.SURVEY_NO_COMMITS.said()


def _tracked_rows(rows: list[StatusRow]) -> list[StatusRow]:
    tracked: list[StatusRow] = []
    seen: set[str] = set()
    for row in rows:
        if row.xy == "??" or row.path in seen:
            continue
        seen.add(row.path)
        tracked.append(row)
    return tracked


def _diff_args(path: str, *, cached: bool, orig: str | None = None) -> tuple[str, ...]:
    head = ("diff", "--cached", *_DIFF_FLAGS) if cached else ("diff", *_DIFF_FLAGS)
    # A staged rename shows as a rename only when both ends are in the pathspec.
    paths = (path, orig) if cached and orig else (path,)
    return (*head, "--", *paths)


def _print_diff_section(repo: Path, rows: list[StatusRow], *, cached: bool) -> None:
    _out(msgs.SURVEY_STAGED if cached else msgs.SURVEY_UNSTAGED)
    any_diff = False
    for row in rows:
        path = row.path
        diff = _git(*_diff_args(path, cached=cached, orig=row.orig), repo=repo).stdout
        if not diff.strip():
            continue
        any_diff = True
        _, hunks = _split_hunks(diff)
        _out(msgs.SURVEY_FILE, path=path, hunks=msgs.plural(len(hunks), "hunk"))
        if not hunks:
            _out(msgs.SURVEY_NO_TEXT_HUNKS)
        for i, h in enumerate(hunks, 1):
            _out(msgs.SURVEY_HUNK, index=i, header=h.split("\n", 1)[0])
            _print(h.rstrip("\n"))
    if not any_diff:
        _out(msgs.SURVEY_NONE)


def cmd_survey(repo: str | Path | None = None) -> int:
    try:
        repo_path = _toplevel(repo)
        branch = _branch_name(repo_path)
        rows = _porcelain(repo_path)
    except GitCommandError as exc:
        print(_git_failure_message(exc), file=sys.stderr)
        return 2 if exc.kind == "not-a-repo" else 1

    _out(msgs.SURVEY_HEADING, repo=repo_path)
    _out(msgs.SURVEY_BRANCH, branch=branch)

    _out(msgs.SURVEY_STATUS)
    if rows:
        for row in rows:
            _out(msgs.SURVEY_STATUS_ROW, xy=row.xy, path=row.shown())
    else:
        _out(msgs.SURVEY_CLEAN)

    tracked = _tracked_rows(rows)
    untracked = [row.path for row in rows if row.xy == "??"]

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
        _print(log.stdout.rstrip("\n"))
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
    try:
        repo_path = _toplevel(repo)
        rows = {row.path: row.xy for row in _porcelain(repo_path)}
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
        diff = _git(*_diff_args(path, cached=False), repo=repo_path).stdout
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
        # --whitespace=nowarn: apply.whitespace=error in config would refuse a
        # hunk the operator wrote; staging must not judge content.
        _git("apply", "--cached", "--recount", "--whitespace=nowarn", stdin=patch, repo=repo_path)
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
                return repo, rest, msgs.COMMIT_HELPER_USAGE.said(usage=f"{arg} <dir> <survey|stage|commit ...>")
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
