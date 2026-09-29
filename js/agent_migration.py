"""Agent manifests to agent.yaml, keeping only tools entries that match a tool.

An old manifest (00-tools.yaml, or 00*.md frontmatter) is rewritten as
agent.yaml beside the prompt. An existing agent.yaml is rewritten only when
one of its tools entries matches nothing. The rewrite keeps the file's lines
and comments: `reasoning_effort:` becomes `reasoning:`, each bare tools entry
NAME becomes `NAME:eager` (the agent listed it, so it is on its boot surface),
and an entry that matches no tool is dropped.

An entry is kept when it is a `tag:` entry, a glob that matches at least one
tool, or a name that is a tool or an agent in the roots being loaded. Matching
is `js.toolkit.policy`'s, over the registry `registry_for_roots` builds, as
`warn_unmatched` does at load time. An entry the policy grammar refuses is kept
for the manifest loader to report.

`js.home` converts every moved agent with this; `scripts/migrate_agents.py`
runs it over any agent root.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import messages as msgs
from . import paths, persona
from .toolkit import policy
from .toolkit.registry import registry_for_roots

_KEY_RENAMES = {"reasoning_effort": "reasoning"}
_OLD_KEYS = ("model", "secondary_model", "reasoning_effort", "max_tokens", "sampling", "tools")
_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)(\s*):(.*)$")
_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*?)\s*$")
_PLAIN_ENTRY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_*?:.\-]*$")

Keep = Callable[[str], bool]


def repo_prompts_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "prompts"


def default_roots(*roots: Path) -> tuple[Path, ...]:
    """The repo prompts, the global agents dir, then `roots`, each once."""
    ordered: list[Path] = []
    for root in (repo_prompts_dir(), paths.global_agents_dir(), *roots):
        if root not in ordered:
            ordered.append(root)
    return tuple(ordered)


def tool_matcher(roots: Sequence[Path]) -> Keep:
    """Whether a `noun:modifier` entry matches a tool of the registry for `roots`."""
    tools = registry_for_roots(tuple(roots)).tools

    def keep(entry: str) -> bool:
        try:
            parsed = policy.parse_entry(entry, persona.AGENT_MANIFEST)
        except policy.ToolPolicyError:
            return True
        if parsed.noun == "tag":
            return True
        rule = policy.Rule(parsed)
        return any(rule.matches(tool) for tool in tools)

    return keep


@dataclass
class Result:
    agent_dir: Path
    action: str  # "migrate", "prune", "skip", "error", "none"
    message: str = ""
    manifest_text: str = ""
    dropped: list[str] = field(default_factory=list)


def _convert_entry(raw: Any, keep: Keep) -> str | None:
    """Old selector -> noun:modifier; None for an entry that matches no tool."""
    text = str(raw).strip()
    entry = text if ":" in text else f"{text}:eager"
    return entry if keep(entry) else None


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


def _expected(data: dict[str, Any], dropped: list[str], keep: Keep) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "tools" and isinstance(value, list):
            converted = []
            for item in value:
                entry = _convert_entry(item, keep)
                if entry is None:
                    dropped.append(str(item).strip())
                else:
                    converted.append(entry)
            value = converted
        out[_KEY_RENAMES.get(key, key)] = value
    return out


def _rewrite_lines(text: str, keep: Keep) -> str:
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
            entry = _convert_entry(parsed, keep)
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


def render_manifest(text: str, data: dict[str, Any], keep: Keep) -> tuple[str, list[str]]:
    """agent.yaml text for a manifest's text and its parsed mapping, and the dropped entries."""
    dropped: list[str] = []
    expected = _expected(data, dropped, keep)
    try:
        rewritten = _rewrite_lines(text, keep)
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


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _plan_prune(agent_dir: Path, keep: Keep) -> Result:
    manifest = agent_dir / persona.AGENT_MANIFEST
    if not manifest.is_file():
        return Result(agent_dir, "none")
    text = manifest.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        return Result(agent_dir, "error", msgs.AGENT_MIGRATE_BAD_YAML.text(file=manifest.name, error=_one_line(exc)))
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        return Result(agent_dir, "none")
    manifest_text, dropped = render_manifest(text, data, keep)
    if not dropped:
        return Result(agent_dir, "none")
    return Result(agent_dir, "prune", msgs.AGENT_MIGRATE_PRUNED.text(file=manifest.name), manifest_text, dropped)


def plan(agent_dir: Path, keep: Keep) -> tuple[Result, Path | None, str | None]:
    """What migrating one agent dir would do: (result, file rewritten, new md body)."""
    legacy = persona._find_yaml_zero_file(agent_dir)
    frontmatter_md = _zero_md_with_frontmatter(agent_dir)
    if legacy is None and frontmatter_md is None:
        result = _plan_prune(agent_dir, keep)
        return result, (agent_dir / persona.AGENT_MANIFEST if result.action == "prune" else None), None
    if (agent_dir / persona.AGENT_MANIFEST).exists():
        old = legacy or frontmatter_md
        return Result(agent_dir, "skip", msgs.AGENT_MIGRATE_BOTH.text(file=old.name)), None, None
    new_body: str | None = None
    if legacy is not None:
        zero_md = [p.name for p in agent_dir.glob("*.md") if persona._is_zero_file(p)]
        if zero_md:
            return Result(agent_dir, "skip", msgs.AGENT_MIGRATE_ZERO_MD.text(
                files=", ".join(sorted(zero_md)), file=legacy.name)), None, None
        old = legacy
        text = legacy.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as exc:
            return Result(agent_dir, "error", msgs.AGENT_MIGRATE_BAD_YAML.text(
                file=legacy.name, error=_one_line(exc))), None, None
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
        return Result(agent_dir, "error", msgs.AGENT_MIGRATE_NOT_A_MAPPING.text(file=old.name)), None, None
    unknown = sorted(str(key) for key in data if key not in _OLD_KEYS)
    if unknown:
        return Result(agent_dir, "error", msgs.AGENT_MIGRATE_UNKNOWN_KEYS.text(
            file=old.name, keys=", ".join(unknown))), None, None
    manifest_text, dropped = render_manifest(text, data, keep)
    return Result(agent_dir, "migrate", msgs.AGENT_MIGRATE_CONVERTED.text(file=old.name),
                  manifest_text, dropped), old, new_body


def migrate_agent_dir(agent_dir: Path, *, apply: bool = False, keep: Keep | None = None) -> Result:
    """Convert or prune one agent dir. `keep` defaults to the tools of the repo
    prompts, the global agents dir and this agent's own root."""
    keep = keep if keep is not None else tool_matcher(default_roots(agent_dir.parent))
    result, old, new_body = plan(agent_dir, keep)
    if result.action not in ("migrate", "prune") or not apply:
        return result
    manifest = agent_dir / persona.AGENT_MANIFEST
    old_text = old.read_text(encoding="utf-8")
    manifest.write_text(result.manifest_text, encoding="utf-8")
    if result.action == "migrate":
        if new_body is not None and new_body.strip():
            old.write_text(new_body.rstrip() + "\n", encoding="utf-8")
        else:
            old.unlink()
    try:
        persona.load_agent_manifest(manifest)
    except (OSError, ValueError) as exc:
        if result.action == "migrate":
            manifest.unlink()
        old.write_text(old_text, encoding="utf-8")
        return Result(agent_dir, "error", msgs.AGENT_MIGRATE_RESTORED.text(file=old.name, error=_one_line(exc)))
    return result


def migrate_root(root: Path, *, apply: bool = False, keep: Keep | None = None) -> list[Result]:
    """Every agent dir directly under `root`. A symlinked agent is skipped: it is
    converted where it lives."""
    keep = keep if keep is not None else tool_matcher(default_roots(root))
    results = []
    for agent_dir in sorted(root.iterdir()):
        if agent_dir.is_symlink():
            results.append(Result(agent_dir, "skip", msgs.AGENT_MIGRATE_SYMLINK.text(target=agent_dir.readlink())))
            continue
        if agent_dir.is_dir():
            results.append(migrate_agent_dir(agent_dir, apply=apply, keep=keep))
    return results
