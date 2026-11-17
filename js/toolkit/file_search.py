"""The search tools: fs_search over ripgrep and ast_search over ast-grep,
the child processes they run, and stopping them when a turn is cancelled."""

from __future__ import annotations

import ast
import json
import os
import re
import signal
import stat
import subprocess
import textwrap
import threading
from functools import lru_cache
from pathlib import Path

from .. import jail
from ..tool_binaries import resolve_binary
from .core import ToolContext
from .fs import format_numbered_lines, hash_bytes, read_regular_bytes, unified_diff
from .path_hints import missing_path_hint
from .results import truncate
from .sanitize import int_or_default


def _glob_literal(text: str) -> str:
    """``text`` as a ripgrep glob that matches only itself."""
    return re.sub(r"([\\\[\]*?{}])", r"\\\1", text)


def _real_dir(path: Path) -> Path:
    """``path`` with symlinks resolved, or as given when it cannot be."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


_RG_MISSING = 'ERROR: rg (ripgrep) not found in tools/bin or PATH; run `just install` to provision it.'
_RG_TIMEOUT_S = 120
# ripgrep's line for a path its walk could not open or read, e.g.
# "rg: /a/b: IO error for operation on /a/b: Permission denied (os error 13)".
# rg exits 2 after one of these but still prints every match it reached.
_RG_WALK_ERROR = re.compile(r"^rg: (?P<path>.+?): (?:.*: )?(?P<reason>[^:]+?) \(os error \d+\)$")
_RG_WALK_ERRORS_SHOWN = 5
_AST_GREP_MISSING = (
    'ERROR: ast-grep not found in tools/bin or PATH; run `just install` to provision it.'
)
_AST_GREP_TIMEOUT_S = 120
AST_GREP_LANGUAGES = (
    "Bash", "C", "Cpp", "CSharp", "Css", "Dart", "Elixir", "Go",
    "Haskell", "Hcl", "Html", "Java", "JavaScript", "Json", "Kotlin",
    "Lua", "Markdown", "Nix", "Php", "Python", "Ruby", "Rust", "Scala",
    "Solidity", "Swift", "Tsx", "TypeScript", "Yaml",
)


def _rg_binary() -> str | None:
    return resolve_binary("rg")


def _ast_grep_binary() -> str | None:
    return resolve_binary("ast-grep")


def _rg_env() -> dict[str, str]:
    # Inherit the real environment (PATH, locale) but drop the box-local ripgrep
    # config so the documented contract holds everywhere: .gitignore honoured
    # inside a git tree, .ignore/.rgignore anywhere, hidden + binary + non-regular
    # files skipped. A stray RIPGREP_CONFIG_PATH must not silently change results.
    env = dict(os.environ)
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


@lru_cache(maxsize=1)
def _rg_types(rg: str) -> frozenset[str]:
    """Names rg accepts for `--type` (`rust`, `py`, `js`, ...), from `rg --type-list`."""
    try:
        proc = subprocess.run(
            [rg, "--type-list"], capture_output=True, text=True, timeout=10, env=_rg_env()
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(
        line.split(":", 1)[0].strip() for line in proc.stdout.splitlines() if ":" in line
    )


_SEARCH_PROCESSES: dict[int, set[subprocess.Popen]] = {}
_SEARCH_PROCESSES_LOCK = threading.Lock()


def _kill_search_process(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def interrupt_inflight(context: ToolContext) -> int:
    """Stop searches owned by a cancelled turn; their workers reap the children."""
    with _SEARCH_PROCESSES_LOCK:
        processes = tuple(_SEARCH_PROCESSES.get(id(context), ()))
        for proc in processes:
            _kill_search_process(proc)
    return len(processes)


def _rg_stream(
    argv: list[str],
    want: int,
    timeout: int,
    input_text: str | None = None,
    cwd: str | None = None,
    context: ToolContext | None = None,
) -> tuple[list[str], int | None, str, bool]:
    """Run rg and collect at most *want* output lines, then stop it.

    Returns (lines, returncode, stderr, timed_out). returncode is None when rg
    was stopped early because enough lines were already gathered — the caller
    treats that as a successful match. Streaming with an early stop keeps memory
    bounded (a pattern matching millions of lines never buffers them all).

    The deadline is enforced by a watchdog that kills rg, NOT by a check inside
    the read loop: `for line in proc.stdout` blocks until rg emits something, so a
    loop-side check can only fire while output is already flowing — exactly the
    case that does not need a timeout. A long scan that matches nothing emits no
    lines at all, and used to run unbounded.

    The child gets its own session so the watchdog can signal the whole group.
    Killing only the direct child leaves any grandchild holding the write end of
    the pipe, and the read loop then blocks past the deadline anyway — the exact
    hang the watchdog exists to prevent."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_rg_env(),
            start_new_session=True,
            cwd=cwd,
        )
    except OSError as exc:
        return [], 2, str(exc), False
    with _SEARCH_PROCESSES_LOCK:
        _SEARCH_PROCESSES.setdefault(id(context), set()).add(proc)
    lines: list[str] = []
    stopped_early = False
    expired = threading.Event()

    def _kill_group() -> None:
        _kill_search_process(proc)

    def _expire() -> None:
        expired.set()
        _kill_group()

    watchdog = threading.Timer(timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    stderr_chunks: list[bytes] = []
    assert proc.stderr is not None

    def read_stderr() -> None:
        stderr_chunks.append(proc.stderr.read())

    reader = threading.Thread(target=read_stderr, daemon=True, name="search-stderr")
    reader.start()
    writer: threading.Thread | None = None
    if input_text is not None:
        assert proc.stdin is not None

        def write_stdin() -> None:
            try:
                proc.stdin.write(input_text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=write_stdin, daemon=True, name="search-stdin")
        writer.start()
    assert proc.stdout is not None
    try:
        # Split the raw bytes on b"\n" only. Text-mode universal newlines would
        # turn a bare CR inside a matched line (a file with CR line endings) into
        # a break, splitting one match into several lines; decoding with
        # backslashreplace renders bytes that are not valid UTF-8 as \xNN escapes
        # instead of dropping them to U+FFFD.
        for raw in proc.stdout:
            lines.append(raw.rstrip(b"\n").decode("utf-8", errors="backslashreplace"))
            if len(lines) >= want:
                stopped_early = True
                break
        if stopped_early:
            _kill_group()
        proc.wait()
        reader.join()
    finally:
        _kill_group()
        proc.wait()
        reader.join()
        if writer is not None:
            writer.join()
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                stream.close()
        watchdog.cancel()
        with _SEARCH_PROCESSES_LOCK:
            processes = _SEARCH_PROCESSES.get(id(context), set())
            processes.discard(proc)
            if not processes:
                _SEARCH_PROCESSES.pop(id(context), None)
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    timed_out = expired.is_set()
    rc = None if stopped_early else proc.returncode
    return lines, rc, stderr, timed_out


def _rg_walk_errors(stderr: str) -> list[str] | None:
    """The paths rg's walk could not read, each as "path: reason", or None
    when stderr is empty or says anything else (a bad regex or glob)."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    found = [_RG_WALK_ERROR.match(line) for line in lines]
    if not found or not all(found):
        return None
    return [f"{match['path']}: {match['reason']}" for match in found]


# Excludes every dot-prefixed basename, and prunes dot-prefixed directories with
# it, restoring ripgrep's default hidden behaviour after a whitelist glob has
# overridden it.
_HIDDEN_GUARD_GLOB = "!.*"


def _glob_asks_for_hidden(pattern: str | None) -> bool:
    """True when a caller's glob names a dot-prefixed path component itself.

    `.env`, `**/.github/*` and `.git/**` are explicit requests for hidden paths.
    `.` and `..` are path syntax, not hidden names, so they do not count.
    A negated glob (``!.env``) filters results; it never broadens the search to
    hidden paths, so it is not an explicit request.
    """
    if not pattern:
        return False
    text = str(pattern).strip()
    if text.startswith("!"):
        return False
    return any(
        part.startswith(".") and part not in (".", "..")
        for part in text.split("/")
        if part
    )


def _stat_stamp(path: Path) -> tuple[int, int, int] | None:
    """Identity of the file a search described, so the dedup cache stops
    describing a tree that moved underneath it: any edit, external write, or
    undo restore changes at least mtime or size."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def fs_search(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    output_mode: str | None = None,
    before_context: int | None = None,
    after_context: int | None = None,
    context_lines: int | None = None,
    show_line_numbers: bool | None = True,
    case_insensitive: bool | None = False,
    file_type: str | None = None,
    head_limit: int | None = None,
    offset: int | None = None,
    multiline: bool | None = False,
    context: ToolContext | None = None,
    **rg_flags,
) -> str:
    assert context is not None
    before_context = rg_flags.get("-B", before_context)
    after_context = rg_flags.get("-A", after_context)
    context_lines = rg_flags.get("-C", context_lines)
    show_line_numbers = rg_flags.get("-n", show_line_numbers)
    case_insensitive = rg_flags.get("-i", case_insensitive)
    file_type = rg_flags.get("type", file_type)
    root = context.resolve_path(path or ".")
    if not root.exists():
        return f"ERROR: Path does not exist: {root}{missing_path_hint(context, root)}"
    try:
        root_stat = root.stat()
    except OSError as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    if not stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISREG(root_stat.st_mode):
        return f"ERROR: not a regular file or directory: {root}"
    mode = output_mode or "files_with_matches"
    if file_type and (
        (glob and not str(glob).strip().startswith("!"))
        or (mode == "files" and pattern and not str(pattern).strip().startswith("!"))
    ):
        # ripgrep resolves a positive --glob ahead of --type, so a whitelist glob
        # makes file_type a no-op. The two cannot be intersected; name the
        # conflict instead of silently applying one.
        return (
            "ERROR: file_type cannot be combined with a positive glob; ripgrep "
            "would apply the glob and drop file_type. Pass one filter."
        )
    # A directory's own stat does not move when a nested file changes, so the
    # stamp cannot vouch for a directory-root result: skip the memo for
    # directory roots. A regular-file root's stamp does establish what the hit
    # described (st_ino catches a rename-over that preserves size and mtime).
    memoized = context.search_deduplicate and not stat.S_ISDIR(root_stat.st_mode)
    stamp = _stat_stamp(root) if memoized else None
    cache_key = repr((pattern, str(root), glob, mode, before_context, after_context, context_lines, show_line_numbers, case_insensitive, file_type, head_limit, offset, multiline, stamp))
    cached = context.search_cache.get(cache_key) if memoized else None
    if cached is not None:
        return cached + "\n[deduplicated repeated search]"

    rg = _rg_binary()
    if rg is None:
        return _RG_MISSING

    if head_limit is not None and not isinstance(head_limit, bool):
        try:
            if int(head_limit) < 1:
                return "ERROR: head_limit must be at least 1"
        except (TypeError, ValueError):
            pass
    skip = int_or_default(offset, 0, minimum=0)
    limit = int_or_default(head_limit, 10_000, minimum=1)
    before = int_or_default(context_lines if context_lines is not None else before_context, 0, minimum=0)
    after = int_or_default(context_lines if context_lines is not None else after_context, 0, minimum=0)

    # `--sort path` makes ripgrep walk and print in a stable single-threaded
    # order. Offset paging slices the captured stream, and ripgrep's parallel
    # output order is not stable, so without this consecutive pages overlap and
    # some entries are never returned.
    # `--one-file-system` keeps the walk on the filesystem the root is on: a
    # mount point under the root (an NFS share, an automount) is not entered.
    argv = [rg, "--color=never", "--sort", "path"]
    if context.search_one_file_system:
        argv.append("--one-file-system")
    if mode == "files":
        argv.append("--files")
    elif mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif mode == "count":
        # --with-filename so a single explicit file still prints path:count;
        # ripgrep omits the path for a lone file otherwise.
        argv += ["--count", "--with-filename"]
    elif mode == "content":
        argv += ["--no-heading", "--with-filename"]
        argv.append("--line-number" if show_line_numbers else "--no-line-number")
        if before:
            argv += ["--before-context", str(before)]
        if after:
            argv += ["--after-context", str(after)]
    else:
        return "ERROR: output_mode must be one of files, files_with_matches, content, count"
    if case_insensitive and mode != "files":
        argv.append("--ignore-case")
    if multiline:
        argv += ["--multiline", "--multiline-dotall"]
    glob_flag = "--iglob" if case_insensitive and mode == "files" else "--glob"
    if glob:
        argv += [glob_flag, str(glob)]
    if file_type:
        # Prefer rg's own type table so `type=rust` matches *.rs (and `py` also
        # matches .pyi/.pyw), as ripgrep's `--type` does. A bare extension
        # rg has no type for ("gdshader") still works via an extension glob —
        # previously EVERY value became `*.<value>`, so `type=rust` silently matched
        # nothing at all.
        name = str(file_type).strip().lstrip(".")
        if name in _rg_types(rg):
            argv += ["--type", name]
        else:
            argv += ["--glob", f"*.{name}"]
    if mode == "files":
        argv += [glob_flag, pattern]
    else:
        argv += ["--regexp", pattern]
    # A whitelist --glob outranks ripgrep's hidden-file filter, so every mode that
    # passes one (files mode always does: `pattern` IS the glob) was silently
    # behaving like `rg --files --hidden` and dumping .git internals. The guard
    # goes LAST because overrides are last-match-wins. Skipped when the caller
    # names a dot-prefixed component themselves — that is an explicit ask, and it
    # needs --hidden too or the hidden directory is pruned before the glob is
    # consulted. An explicit `path` to a hidden file still works either way:
    # ripgrep never applies the hidden filter to a root it was handed.
    if _glob_asks_for_hidden(glob) or (mode == "files" and _glob_asks_for_hidden(pattern)):
        argv.append("--hidden")
    else:
        argv += ["--glob", _HIDDEN_GUARD_GLOB]
    # JAIL_HIDE paths inside the root stay out whatever the globs ask for.
    for glob in _hidden_globs(root if root.is_dir() else root.parent, context):
        argv += ["--glob", glob]
    argv += ["--", str(root)]

    # Run rg from the search root: a glob containing a slash is matched against
    # the path relative to rg's working directory, so `src/*.py` can never match
    # under an absolute root unless rg's cwd is that root. Absolute roots still
    # print absolute paths, so the output shape is unchanged.
    rg_cwd = str(root) if root.is_dir() else str(root.parent)
    # Ask for one past the page so an exact-fit result is not reported as
    # truncated; only a genuine extra line proves more matches exist.
    lines, rc, stderr, timed_out = _rg_stream(
        argv, skip + limit + 1, _RG_TIMEOUT_S, cwd=rg_cwd, context=context
    )
    if timed_out:
        return f"ERROR: search timed out after {_RG_TIMEOUT_S}s"
    # rc None = rg stopped early with a full page of matches; 0 = matches; 1 = no
    # matches (clean empty result); 2 with only walk errors on stderr = the
    # matches rg could reach; anything else = real rg error (bad regex/glob).
    unread = _rg_walk_errors(stderr or "")
    if rc is not None and rc not in (0, 1) and unread is None:
        detail = (stderr or "").strip()
        if len(detail) > 2000:
            detail = detail[:2000] + " …[diagnostic truncated]"
        return f"ERROR: {detail or f'rg exit {rc}'}"

    truncated = len(lines) > skip + limit
    sliced = lines[skip:skip + limit]
    out = "\n".join(sliced) if sliced else "(no matches)"
    if truncated:
        out += f"\n[more matches than head_limit={limit}; continue with offset={skip + limit}]"
    if unread:
        more = len(unread) - _RG_WALK_ERRORS_SHOWN
        named = "; ".join(unread[:_RG_WALK_ERRORS_SHOWN]) + (f"; and {more} more" if more > 0 else "")
        if rc is None:
            # The walk stopped with the page full; paths past that point were
            # never reached, so the count is a floor.
            out += (
                f"\n[not searched, rg could not read at least {len(unread)} path(s) "
                f"before the walk stopped at head_limit: {named}]"
            )
        else:
            out += f"\n[not searched, rg could not read {len(unread)} path(s): {named}]"
    if memoized:
        context.search_cache[cache_key] = out
    return out


def _ast_language(raw: str | None) -> str | None:
    if raw is None:
        return None
    folded = str(raw).strip().casefold()
    return next((language for language in AST_GREP_LANGUAGES if language.casefold() == folded), "")


def _ast_argv(
    binary: str,
    pattern: str,
    root: Path,
    lang: str | None,
    rewrite: str | None = None,
    stdin: bool = False,
    excludes: tuple[str, ...] = (),
) -> list[str]:
    argv = [
        binary,
        "run",
        # Attached form: ast-grep's clap parser reads a hyphen-leading token after
        # a separated flag as another option, so `- $A` (a YAML block sequence or
        # Markdown bullet) never reaches the matcher.
        f"--pattern={pattern}",
        "--json=stream",
        "--color",
        "never",
    ]
    if lang:
        argv += ["--lang", lang]
    if rewrite is not None:
        argv.append(f"--rewrite={rewrite}")
    for glob in excludes:
        argv += ["--globs", glob]
    if stdin:
        argv.append("--stdin")
    else:
        argv += ["--", str(root)]
    return argv


def _hidden_globs(base: Path, context: ToolContext) -> tuple[str, ...]:
    """ast-grep/ripgrep globs, relative to working directory ``base``, that
    leave out every `JAIL_HIDE` path inside it."""
    out: list[str] = []
    for hidden in jail.hidden_under(base, context):
        rel = _glob_literal(hidden.relative_to(_real_dir(base)).as_posix())
        out += [f"!/{rel}", f"!/{rel}/**"]
    return tuple(out)


def _ast_records(
    binary: str,
    pattern: str,
    root: Path,
    lang: str | None,
    rewrite: str | None,
    want: int,
    context: ToolContext,
) -> tuple[list[dict], int | None, str, bool, str | None, bool]:
    # Hidden paths stay out of the search itself, so they never count
    # against ``want``; the globs match relative to the search root.
    base = root if root.is_dir() else root.parent
    excludes = _hidden_globs(base, context)
    lines, rc, stderr, timed_out = _rg_stream(
        _ast_argv(binary, pattern, root, lang, rewrite, excludes=excludes), want, _AST_GREP_TIMEOUT_S,
        cwd=str(base), context=context,
    )

    used_stdin = False
    if rc == 1 and not lines and lang and root.is_file():
        try:
            source = read_regular_bytes(root).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [], rc, stderr, timed_out, str(exc), used_stdin
        lines, rc, stderr, timed_out = _rg_stream(
            _ast_argv(binary, pattern, root, lang, rewrite, stdin=True),
            want,
            _AST_GREP_TIMEOUT_S,
            input_text=source, context=context,
        )
        used_stdin = True

    records: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            return [], rc, stderr, timed_out, f"invalid ast-grep JSON output: {exc.msg}", used_stdin
        if not isinstance(record, dict):
            return [], rc, stderr, timed_out, "invalid ast-grep JSON output: expected an object", used_stdin
        if used_stdin and record.get("file") == "STDIN":
            record["file"] = str(root)
        records.append(record)
    return records, rc, stderr, timed_out, None, used_stdin


def _ast_match_output(records: list[dict], context: ToolContext) -> str:
    blocks: list[str] = []
    for record in records:
        raw_file = record.get("file")
        raw_range = record.get("range")
        if not isinstance(raw_file, str) or not isinstance(raw_range, dict):
            return "ERROR: invalid ast-grep JSON output: match has no file or range"
        start = raw_range.get("start")
        if not isinstance(start, dict) or not isinstance(start.get("line"), int):
            return "ERROR: invalid ast-grep JSON output: match has no start line"
        target = context.resolve_path(raw_file)
        line_number = start["line"] + 1
        source = record.get("lines", record.get("text", ""))
        if not isinstance(source, str):
            return "ERROR: invalid ast-grep JSON output: match has no source text"
        source_lines = source.splitlines() or [""]
        numbered = format_numbered_lines(source_lines, line_number)
        blocks.append(f"{target}:{line_number}\n{numbered}")
    return "\n".join(blocks) if blocks else "(no matches)"


# A metavariable, rewritten to a plain name so the pattern parses as Python.
_AST_METAVARIABLE = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)?|\$([A-Z_][A-Z0-9_]*)")
_HEADER_BODY_VAR = "JS_BODY"


def _body_var(pattern: str) -> str:
    """A metavariable name for the body the retry adds that the caller's
    pattern does not use."""
    names = {m.group(1) or m.group(2) for m in _AST_METAVARIABLE.finditer(pattern)}
    name, n = _HEADER_BODY_VAR, 1
    while name in names:
        n += 1
        name = f"{_HEADER_BODY_VAR}_{n}"
    return name


def _parses(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _bodyless_python_header(pattern: str, language: str | None, root: Path) -> str | None:
    """The pattern, dedented, when it is a `def` or `class` (decorated or not,
    over any number of lines) that stops at its colon. Such a pattern is not
    parseable Python, so ast-grep matches nothing for it."""
    if language is not None and language != "Python":
        return None
    if language is None and root.is_file() and root.suffix not in (".py", ".pyi"):
        return None
    header = textwrap.dedent(pattern).strip()
    plain = _AST_METAVARIABLE.sub(lambda m: m.group(1) or m.group(2) or "_", header)
    if _parses(plain) is not None:
        return None
    module = _parses(f"{plain}\n    pass\n")
    if module is None or len(module.body) != 1:
        return None
    if not isinstance(module.body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None
    return header


def _trim_to_header(records: list[dict], body_var: str) -> None:
    """Cut each match's source lines to those before its body, captured as
    `body_var`, starts."""
    for record in records:
        body = (record.get("metaVariables") or {}).get("multi", {}).get(body_var) or []
        start = (record.get("range") or {}).get("start", {}).get("line")
        source = record.get("lines")
        if not body or not isinstance(start, int) or not isinstance(source, str):
            continue
        body_line = body[0].get("range", {}).get("start", {}).get("line")
        if isinstance(body_line, int) and body_line > start:
            record["lines"] = "\n".join(source.splitlines()[: body_line - start])


def _ast_warning(stderr: str) -> str:
    """Promote ast-grep's first stderr line into a model-visible note.

    ast-grep prints ``Warning: Pattern contains an ERROR node …`` while exiting
    0 or 1, so without this a pattern it could not parse as the chosen language
    is indistinguishable from a genuine no-match."""
    for line in stderr.splitlines():
        detail = line.strip()
        if detail:
            return f"WARNING: {detail.removeprefix('Warning: ').strip()}"
    return ""


def _prepare_ast_rewrite(
    records: list[dict], context: ToolContext
) -> dict[Path, tuple[bytes, bytes]] | str:
    edits_by_file: dict[Path, list[tuple[int, int, bytes, bytes]]] = {}
    for record in records:
        raw_file = record.get("file")
        offsets = record.get("replacementOffsets")
        replacement = record.get("replacement")
        text = record.get("text")
        if (
            not isinstance(raw_file, str)
            or not isinstance(offsets, dict)
            or not isinstance(offsets.get("start"), int)
            or not isinstance(offsets.get("end"), int)
            or not isinstance(replacement, str)
            or not isinstance(text, str)
        ):
            return "ERROR: invalid ast-grep JSON output: rewrite metadata is incomplete"
        target = context.resolve_path(raw_file)
        edits_by_file.setdefault(target, []).append(
            (offsets["start"], offsets["end"], text.encode("utf-8"), replacement.encode("utf-8"))
        )

    prepared: dict[Path, tuple[bytes, bytes]] = {}
    for target, edits in edits_by_file.items():
        try:
            source = target.read_bytes()
        except OSError as exc:
            return f"ERROR: {exc}"
        previous_end = -1
        for start, end, expected, _replacement in sorted(edits):
            if start < previous_end:
                return f"ERROR: ast-grep returned overlapping rewrites for {target}"
            if start < 0 or end < start or source[start:end] != expected:
                return f"ERROR: {target} changed while preparing the structural rewrite"
            previous_end = end
        updated = source
        for start, end, _expected, replacement in sorted(edits, reverse=True):
            updated = updated[:start] + replacement + updated[end:]
        prepared[target] = (source, updated)
    return prepared


def _ast_rewrite_diff(prepared: dict[Path, tuple[bytes, bytes]]) -> str:
    chunks: list[str] = []
    for target, (source, updated) in prepared.items():
        chunks.append(
            unified_diff(
                source.decode("utf-8", errors="replace"),
                updated.decode("utf-8", errors="replace"),
                str(target),
                str(target),
            )
        )
    return "".join(chunks).rstrip()


def _ast_failure(rc: int | None, stderr: str, timed_out: bool, parse_error: str | None) -> str | None:
    """The error line for an ast-grep run that failed, or None. Exit 1 is a
    clean no-match."""
    if timed_out:
        return f"ERROR: search timed out after {_AST_GREP_TIMEOUT_S}s"
    if parse_error:
        return f"ERROR: {parse_error}"
    if rc is not None and rc not in (0, 1):
        detail = stderr.strip()
        first = detail.splitlines()[0] if detail else f"ast-grep exit {rc}"
        return f"ERROR: {first}"
    return None


def ast_search(
    pattern: str,
    path: str | None = None,
    lang: str | None = None,
    rewrite: str | None = None,
    apply: bool = False,
    max_results: int | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    if not isinstance(pattern, str) or not pattern.strip():
        return "ERROR: pattern is required"
    if rewrite is not None and not isinstance(rewrite, str):
        return "ERROR: rewrite must be a string"
    if apply and rewrite is None:
        return "ERROR: apply=true requires rewrite"

    language = _ast_language(lang)
    if language == "":
        return f"ERROR: unsupported ast-grep language: {lang}"
    root = context.resolve_path(path or ".")
    if not root.exists():
        return f"ERROR: Path does not exist: {root}"
    try:
        root_stat = root.stat()
    except OSError as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    if not stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISREG(root_stat.st_mode):
        return f"ERROR: not a regular file or directory: {root}"

    limit = int_or_default(max_results, 100, minimum=1)
    # Mirror fs_search: a directory's own stat does not move when a nested file
    # changes, so a directory-root result cannot be vouched for by the root's
    # stamp; only a regular-file root is memoized, keyed on its stamp.
    memoized = context.search_deduplicate and not stat.S_ISDIR(root_stat.st_mode)
    stamp = _stat_stamp(root) if memoized else None
    cache_key = repr(("ast_search", pattern, str(root), language, rewrite, limit, stamp))
    cached = context.search_cache.get(cache_key) if memoized and not apply else None
    if cached is not None:
        return cached + "\n[deduplicated repeated search]"

    binary = _ast_grep_binary()
    if binary is None:
        return _AST_GREP_MISSING

    def search(query: str, rewrite_to: str | None) -> tuple[list[dict], int | None, str, bool, str | None, bool]:
        # Every attempt, the Python header retry included, keeps the hidden
        # paths out the same way.
        return _ast_records(binary, query, root, language, rewrite_to, limit + 1, context)

    records, rc, stderr, timed_out, parse_error, _used_stdin = search(pattern, rewrite)
    failure = _ast_failure(rc, stderr, timed_out, parse_error)
    if failure is not None:
        return failure

    warning = _ast_warning(stderr)
    header = _bodyless_python_header(pattern, language, root) if not records else None
    if header is not None:
        if rewrite is not None:
            return (
                "ERROR: a Python def/class pattern without a body does not parse; "
                "add a body such as `$$$BODY` on the next line to rewrite it"
            )
        body_var = _body_var(header)
        headed = f"{header}\n    $$${body_var}"
        records, rc, stderr, timed_out, parse_error, _used_stdin = search(headed, None)
        failure = _ast_failure(rc, stderr, timed_out, parse_error)
        if failure is not None:
            return failure
        _trim_to_header(records, body_var)
        warning = _ast_warning(stderr) or (
            ""
            if records
            else "WARNING: a Python def/class pattern without a body does not parse; "
            f"searched again with a `$$${body_var}` body"
        )
    overflow = len(records) > limit
    visible = records[:limit]
    if rewrite is None:
        out = truncate(_ast_match_output(visible, context), context)
        if overflow:
            out += "\n[additional matches omitted; increase max_results to see them]"
        if warning:
            out = f"{warning}\n{out}"
        if memoized:
            context.search_cache[cache_key] = out
        return out

    prepared = _prepare_ast_rewrite(visible, context)
    if isinstance(prepared, str):
        return prepared
    diff = _ast_rewrite_diff(prepared) or "(no changes)"
    if not apply:
        suffix = "\n[additional matches omitted; increase max_results to preview them]" if overflow else ""
        out = f"DRY RUN: no files changed. Pass apply=true to apply.\n{diff}{suffix}"
        out = truncate(out, context)
        if warning:
            out = f"{warning}\n{out}"
        if memoized:
            context.search_cache[cache_key] = out
        return out
    if overflow:
        return (
            f"ERROR: rewrite matched more than max_results={limit}; "
            "narrow path or increase max_results before applying"
        )
    if not prepared or all(source == updated for source, updated in prepared.values()):
        return "(no changes)"

    for target in prepared:
        context.resolve_path(target, write=True)
    for target in prepared:
        context.snapshot(target)
    apply_stderr = ""
    rc = 0
    try:
        for target, (source, _updated) in prepared.items():
            if read_regular_bytes(target) != source:
                return f"ERROR: {target} changed while preparing the structural rewrite"
        for target, (_source, updated) in prepared.items():
            target.write_bytes(updated)
    except OSError as exc:
        rc = 1
        apply_stderr = str(exc)
    context.search_cache.clear()
    if rc != 0:
        detail = apply_stderr.strip()
        first = detail.splitlines()[0] if detail else f"ast-grep exit {rc}"
        return f"ERROR: {first}; snapshots are available through undo"

    changed = 0
    for target, (source, _predicted) in prepared.items():
        try:
            updated = target.read_bytes()
        except OSError as exc:
            return f"ERROR: {exc}; snapshots are available through undo"
        if updated != source:
            changed += 1
        updated_hash = hash_bytes(updated)
        context.reads.rehash(target, updated_hash)
        context.reads.remember_content(target, updated_hash, updated)
        prepared[target] = (source, updated)
    diff = _ast_rewrite_diff(prepared) or "(no changes)"
    summary = f"rewrote {len(visible)} match{'es' if len(visible) != 1 else ''} in {changed} file{'s' if changed != 1 else ''}"
    out = truncate(f"{summary}\n{diff}", context)
    if warning:
        out = f"{warning}\n{out}"
    return out
