"""`js rotate [AGE]` and `js rotate --restore [SESSION]`, each with or without
`--dry-run`: the command line of `js.rotate`."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import archive, config, messages as msgs, paths, rotate, session_store, settings


def parser() -> argparse.ArgumentParser:
    p = msgs.ArgumentParser(prog="js rotate", allow_abbrev=False,
                            description=msgs.ROTATE_DESCRIPTION.words())
    p.add_argument("age", nargs="?", metavar="AGE", help=msgs.ROTATE_OPT_AGE.words())
    p.add_argument("--restore", nargs="?", const="", metavar="SESSION", help=msgs.ROTATE_OPT_RESTORE.words())
    p.add_argument("--dry-run", action="store_true", help=msgs.ROTATE_OPT_DRY_RUN.words())
    p.add_argument("--extra", dest="extras", action="append", default=[], metavar="KEY=VALUE",
                   help=msgs.OPT_EXTRA.words())
    return p


def main(argv: list[str]) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.restore is not None and args.age is not None:
        p.error(msgs.ROTATE_RESTORE_WITH_AGE.text(age=args.age))
    # The ROTATE_* settings this run reads take its --extra values.
    config.set_invocation(config.InvocationSources(extras=tuple(args.extras)))
    return run_restore(args) if args.restore is not None else run_rotate(args)


def _format(value: object) -> str:
    fmt = str(value or "").strip()
    if fmt not in archive.FORMATS:
        raise ValueError(msgs.ROTATE_BAD_FORMAT.said(format=fmt, formats=", ".join(archive.FORMATS)))
    return fmt


def _floor(value: object) -> float:
    text = str(value or "").strip()
    if text in ("", "0"):
        return 0.0
    return rotate.parse_age(text).seconds


def _size(count: float) -> str:
    if count < 1024:
        return msgs.ROTATE_SIZE_BYTES.words(count=int(count))
    for unit in ("KB", "MB", "GB"):
        count /= 1024
        if count < 1024 or unit == "GB":
            return msgs.ROTATE_SIZE.words(value=count, unit=unit)
    raise AssertionError("unreachable")


def _report_skipped(skipped: list[tuple[Path, str]]) -> None:
    for path, why in skipped:
        msgs.warn(msgs.ROTATE_SKIPPED, path=path, why=why)


def run_rotate(args: argparse.Namespace) -> int:
    """`js rotate [AGE] [--dry-run]`."""
    try:
        age = rotate.parse_age(args.age or str(settings.live_value("ROTATE_AGE") or ""))
        fmt = _format(settings.live_value("ROTATE_FORMAT"))
        floor = _floor(settings.live_value("ROTATE_AGE_MIN"))
    except ValueError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    if args.dry_run:
        selected = rotate.plan(age, floor=floor)
        sized = rotate.sizes(selected.files())
        for path, size in sized:
            msgs.say(msgs.ROTATE_WOULD, size=_size(size), path=path)
        for path in selected.in_use:
            msgs.say(msgs.ROTATE_WOULD_SKIP, path=path)
        before = sum(size for _, size in sized)
        after = rotate.estimate([path for path, _ in sized], fmt)
        msgs.say(msgs.ROTATE_DRY_SUMMARY, files=len(sized), before=_size(before), after=_size(after),
                 freed=_size(before - after), format=fmt)
        return 0
    tally = rotate.rotate(age, fmt, floor=floor)
    _report_skipped(tally.skipped)
    msgs.say(msgs.ROTATE_SUMMARY, files=tally.files, before=_size(tally.before), after=_size(tally.after),
             skipped=len(tally.skipped), archive=paths.archive_root())
    return 1 if tally.failed else 0


def run_restore(args: argparse.Namespace) -> int:
    """`js rotate --restore [SESSION] [--dry-run]`: every archived file, or one
    session's, back to its live path."""
    if args.restore:
        from .config import locate_session_file

        try:
            session = locate_session_file(session_store.project_folder(Path.cwd()), args.restore)
        except ValueError as exc:
            msgs.warn(msgs.FAILED, error=exc)
            return 2
        files = rotate.session_archived_files(session)
    else:
        files = rotate.archived_files()
    if args.dry_run:
        total = 0
        for live in files:
            found = archive.located(live)
            size = found.stat().st_size if found is not None else 0
            total += size
            msgs.say(msgs.UNROTATE_WOULD, size=_size(size), path=live)
        msgs.say(msgs.UNROTATE_DRY_SUMMARY, files=len(files), size=_size(total))
        return 0
    restored, size, failed = rotate.restore_all(files)
    _report_skipped(failed)
    msgs.say(msgs.UNROTATE_SUMMARY, files=restored, size=_size(size))
    return 1 if failed else 0
