"""Move agent manifests from 00-tools.yaml (or 00*.md frontmatter) to agent.yaml.

    uv run python scripts/migrate_agents.py [--apply] [--show] [ROOT ...]

With no ROOT it walks the global agents dir. Without --apply it only reports
what it would do. Per agent dir it writes agent.yaml, then removes the old
00-tools.yaml (or strips the frontmatter from the 00*.md file), then parses
agent.yaml with the real manifest loader; a manifest that does not parse is
put back.

The rewrite keeps the file's lines and comments: `reasoning_effort:` becomes
`reasoning:`, each bare `tools:` entry NAME becomes `NAME:eager` (the agent
listed it, so it is on its boot surface), and the removed todo tools are
dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from js import paths, persona

REMOVED_TOOLS = ("todo_write", "todo_read", "todo_*")
_KEY_RENAMES = {"reasoning_effort": "reasoning"}
_OLD_KEYS = ("model", "secondary_model", "reasoning_effort", "max_tokens", "sampling", "tools")
_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)(\s*):(.*)$")
_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*?)\s*$")
_PLAIN_ENTRY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_*?:.\-]*$")


@dataclass
class Result:
    agent_dir: Path
    action: str  # "migrate", "skip", "error", "none"
    message: str = ""
    manifest_text: str = ""
    dropped: list[str] = field(default_factory=list)


def _convert_entry(raw: Any) -> str | None:
    """Old selector -> noun:modifier; None for a removed tool."""
    text = str(raw).strip()
    if text in REMOVED_TOOLS:
        return None
    return text if ":" in text else f"{text}:eager"


def _scalar(entry: str) -> str:
    return entry if _PLAIN_ENTRY.match(entry) else json.dumps(entry)


def _split_comment(text: str) -> tuple[str, str]:
    """Split `value  # comment`; a '#' inside quotes is part of the value."""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i].rstrip(), text[i:]
    return text.rstrip(), ""


def _expected(data: dict[str, Any], dropped: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "tools" and isinstance(value, list):
            converted = []
            for item in value:
                entry = _convert_entry(item)
                if entry is None:
                    dropped.append(str(item).strip())
                else:
                    converted.append(entry)
            value = converted
        out[_KEY_RENAMES.get(key, key)] = value
    return out


def _rewrite_lines(text: str) -> str:
    """Line-preserving rewrite of a block-style manifest."""
    out: list[str] = []
    in_tools = False
    for line in text.splitlines():
        top = _TOP_KEY.match(line)
        if top:
            key, space, rest = top.groups()
            in_tools = key == "tools"
            out.append(f"{_KEY_RENAMES.get(key, key)}{space}:{rest}")
            continue
        item = _LIST_ITEM.match(line)
        if in_tools and item:
            indent, body = item.groups()
            value, comment = _split_comment(body)
            parsed = yaml.safe_load(value) if value else value
            entry = _convert_entry(parsed)
            if entry is None:
                continue
            out.append(f"{indent}- {_scalar(entry)}" + (f"  {comment}" if comment else ""))
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _leading_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "\n".join(lines).strip("\n") + ("\n" if lines else "")


def render_manifest(text: str, data: dict[str, Any]) -> tuple[str, list[str]]:
    """agent.yaml text for an old manifest's text and its parsed mapping."""
    dropped: list[str] = []
    expected = _expected(data, dropped)
    try:
        rewritten = _rewrite_lines(text)
        if yaml.safe_load(rewritten) == expected:
            return rewritten, dropped
    except yaml.YAMLError:
        pass
    dumped = yaml.safe_dump(expected, sort_keys=False, allow_unicode=True)
    return _leading_comments(text) + dumped, dropped


def _zero_md_with_frontmatter(agent_dir: Path) -> Path | None:
    for path in sorted(agent_dir.glob("*.md")):
        if persona._is_zero_file(path) and path.read_text(encoding="utf-8").startswith("---"):
            return path
    return None


def plan(agent_dir: Path) -> tuple[Result, Path | None, str | None]:
    """What migrating one agent dir would do: (result, old file, new md body)."""
    legacy = persona._find_yaml_zero_file(agent_dir)
    frontmatter_md = _zero_md_with_frontmatter(agent_dir)
    if legacy is None and frontmatter_md is None:
        return Result(agent_dir, "none"), None, None
    if (agent_dir / persona.AGENT_MANIFEST).exists():
        old = legacy or frontmatter_md
        return Result(agent_dir, "skip", f"both agent.yaml and {old.name} exist; merge by hand"), None, None
    new_body: str | None = None
    if legacy is not None:
        zero_md = [p.name for p in agent_dir.glob("*.md") if persona._is_zero_file(p)]
        if zero_md:
            return Result(agent_dir, "skip",
                          f"{', '.join(sorted(zero_md))} were ignored beside {legacy.name} and would "
                          "load as prompt text after migration; move them first"), None, None
        old = legacy
        text = legacy.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as exc:
            return Result(agent_dir, "error", f"{legacy.name}: invalid YAML: {' '.join(str(exc).split())}"), None, None
    else:
        old = frontmatter_md
        md_text = frontmatter_md.read_text(encoding="utf-8")
        try:
            data, new_body = persona._split_frontmatter(frontmatter_md, md_text)
        except ValueError as exc:
            return Result(agent_dir, "error", str(exc)), None, None
        first = md_text.find("\n")
        text = md_text[first + 1:md_text.find("\n---", first)] + "\n"
    data = data or {}
    if not isinstance(data, dict):
        return Result(agent_dir, "error", f"{old.name} is not a mapping"), None, None
    unknown = sorted(str(key) for key in data if key not in _OLD_KEYS)
    if unknown:
        return Result(agent_dir, "error", f"{old.name}: unknown key(s) {', '.join(unknown)}"), None, None
    manifest_text, dropped = render_manifest(text, data)
    return Result(agent_dir, "migrate", f"{old.name} -> agent.yaml", manifest_text, dropped), old, new_body


def migrate_agent_dir(agent_dir: Path, *, apply: bool = False) -> Result:
    result, old, new_body = plan(agent_dir)
    if result.action != "migrate" or not apply:
        return result
    manifest = agent_dir / persona.AGENT_MANIFEST
    old_text = old.read_text(encoding="utf-8")
    manifest.write_text(result.manifest_text, encoding="utf-8")
    if new_body is None:
        old.unlink()
    elif new_body.strip():
        old.write_text(new_body.rstrip() + "\n", encoding="utf-8")
    else:
        old.unlink()
    try:
        persona.load_agent_manifest(manifest)
    except (OSError, ValueError) as exc:
        manifest.unlink()
        old.write_text(old_text, encoding="utf-8")
        return Result(agent_dir, "error", f"restored {old.name}; agent.yaml did not load: {exc}")
    return result


def migrate_root(root: Path, *, apply: bool = False) -> list[Result]:
    results = []
    for agent_dir in sorted(root.iterdir()):
        if agent_dir.is_symlink():
            results.append(Result(agent_dir, "skip", f"symlink to {agent_dir.readlink()}; migrate it where it lives"))
            continue
        if agent_dir.is_dir():
            results.append(migrate_agent_dir(agent_dir, apply=apply))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_agents", description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="*", type=Path, help="agent roots (default: the global agents dir)")
    parser.add_argument("--apply", action="store_true", help="write the changes; default is a dry run")
    parser.add_argument("--show", action="store_true", help="print each agent.yaml that would be written")
    args = parser.parse_args(argv)
    roots = args.roots or [paths.global_agents_dir()]
    failed = False
    counts: dict[str, int] = {}
    for root in roots:
        if not root.is_dir():
            print(f"{root}: not a directory", file=sys.stderr)
            failed = True
            continue
        for result in migrate_root(root, apply=args.apply):
            counts[result.action] = counts.get(result.action, 0) + 1
            if result.action == "none":
                continue
            verb = {"migrate": "migrated" if args.apply else "would migrate"}.get(result.action, result.action)
            line = f"{verb}: {result.agent_dir}: {result.message}"
            if result.dropped:
                line += f" (dropped removed tools: {', '.join(result.dropped)})"
            print(line, file=sys.stderr if result.action == "error" else sys.stdout)
            failed = failed or result.action == "error"
            if args.show and result.manifest_text:
                print("    " + result.manifest_text.rstrip("\n").replace("\n", "\n    "))
    summary = ", ".join(f"{count} {action}" for action, count in sorted(counts.items()))
    print(f"{'applied' if args.apply else 'dry run'}: {summary or 'nothing found'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
