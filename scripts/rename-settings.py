#!/usr/bin/env python3
"""Move files that drive js from the old setting names to the new ones.

    scripts/rename-settings.py FILE...          show what would change
    scripts/rename-settings.py --write FILE...  change the jsrc files in place

A jsrc file (`jsrc`, `.jsrc*`, `*.irc`) is rewritten: the old dotted names
(`model.id`, `limits.max_read_lines`) become the UPPER_SNAKE names (`MODEL`,
`READ_LINES_MAX`) after `set`, at the start of a line or as the whole body
of an `alias` / `on` line. Values are not touched; history.cwd_first's
on/off becomes HISTORY_ORDER's cwd/last. A line it cannot change safely is
left as it is and printed as `FILE:LINE: not migrated, edit by hand`.

Any other file, a shell script or a Python program, is never rewritten. Each
old setting name, old env var name and deleted name in it is printed as
`FILE:LINE: old name OLD -> NEW, edit by hand` (or `(setting deleted)`), and
those files are edited by hand.

The exit status is 1 when anything was printed for a hand edit. The rules
are `js.rename`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from js.rename import kind_of, rename_old_names, report_old_names  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        epilog=__doc__.split("\n\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--write", action="store_true", help="change the jsrc files in place")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    reported = 0
    for path in args.files:
        old = path.read_text(encoding="utf-8")
        if kind_of(str(path)) != "jsrc":
            for number, name, new in report_old_names(old):
                reported += 1
                target = f"-> {new}" if new else "(setting deleted)"
                print(f"{path}:{number}: old name {name} {target}, edit by hand")
            continue
        new, left = rename_old_names(old)
        reported += len(left)
        for number, note in left:
            print(f"{path}:{number}: not migrated, edit by hand{note}")
        if new == old:
            continue
        changed = [(o, n) for o, n in zip(old.splitlines(), new.splitlines()) if o != n]
        print(f"{path}: {len(changed)} line(s)")
        for o, n in changed:
            print(f"  - {o}\n  + {n}")
        if args.write:
            path.write_text(new, encoding="utf-8")
    return 1 if reported else 0


if __name__ == "__main__":
    sys.exit(main())
