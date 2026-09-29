"""Convert agent manifests to agent.yaml and drop tools entries that match no tool.

    uv run python scripts/migrate_agents.py [--apply] [--show] [ROOT ...]

With no ROOT it walks the global agents dir. Without --apply it only reports
what it would do. An agent dir with 00-tools.yaml (or 00*.md frontmatter) gets
agent.yaml, then loses the old file (or the frontmatter); an agent dir that
already has agent.yaml is rewritten when one of its tools entries matches no
tool. Each written agent.yaml is parsed with the real manifest loader, and one
that does not load is put back.

The conversion is `js.agent_migration`, the same one the ~/.js home migration
runs. An entry is kept when it is a `tag:` entry, a glob that matches a tool, or
a name that is a tool or an agent in the repo prompts, the global agents dir or
a ROOT.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from js import paths
from js.agent_migration import (  # noqa: F401 - the tests reach these through this script
    Result,
    default_roots,
    migrate_agent_dir,
    migrate_root,
    plan,
    render_manifest,
    tool_matcher,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_agents", description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="*", type=Path, help="agent roots (default: the global agents dir)")
    parser.add_argument("--apply", action="store_true", help="write the changes; default is a dry run")
    parser.add_argument("--show", action="store_true", help="print each agent.yaml that would be written")
    args = parser.parse_args(argv)
    roots = args.roots or [paths.global_agents_dir()]
    failed = False
    counts: dict[str, int] = {}
    keep = tool_matcher(default_roots(*(root for root in roots if root.is_dir())))
    for root in roots:
        if not root.is_dir():
            print(f"{root}: not a directory", file=sys.stderr)
            failed = True
            continue
        for result in migrate_root(root, apply=args.apply, keep=keep):
            counts[result.action] = counts.get(result.action, 0) + 1
            if result.action == "none":
                continue
            verb = {
                "migrate": "migrated" if args.apply else "would migrate",
                "prune": "pruned" if args.apply else "would prune",
            }.get(result.action, result.action)
            line = f"{verb}: {result.agent_dir}: {result.message}"
            if result.dropped:
                line += f" (dropped, matching no tool: {', '.join(result.dropped)})"
            print(line, file=sys.stderr if result.action == "error" else sys.stdout)
            failed = failed or result.action == "error"
            if args.show and result.manifest_text:
                print("    " + result.manifest_text.rstrip("\n").replace("\n", "\n    "))
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    print(f"{'applied' if args.apply else 'dry run'}: {summary or 'nothing found'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
