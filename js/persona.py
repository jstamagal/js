"""Load agent prompts and optional tool-surface frontmatter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .promptexpand import expand_prompt


@dataclass(frozen=True)
class PromptSpec:
    system: str
    tool_selectors: tuple[str, ...]


def _is_zero_file(path: Path) -> bool:
    stem = path.stem
    return stem == "00" or stem.startswith("00-") or stem.startswith("00_")


def _split_frontmatter(path: Path, text: str) -> tuple[dict[str, Any] | None, str]:
    if not text.startswith("---"):
        return None, text.rstrip()
    first_line_end = text.find("\n")
    if first_line_end == -1:
        raise ValueError(f"frontmatter in {path} is missing a closing ---")
    close = text.find("\n---", first_line_end + 1)
    if close == -1:
        raise ValueError(f"frontmatter in {path} is missing a closing ---")
    yaml_text = text[first_line_end + 1:close]
    body_start = close + len("\n---")
    if body_start < len(text) and text[body_start:body_start + 1] == "\r":
        body_start += 1
    if body_start < len(text) and text[body_start:body_start + 1] == "\n":
        body_start += 1
    try:
        data = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter in {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"frontmatter in {path} must be a mapping")
    return data, text[body_start:].rstrip()


def _coerce_tool_selectors(path: Path, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"tools frontmatter in {path} must be a list")
    selectors: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"tools frontmatter in {path} must contain only strings")
        selector = item.strip()
        if selector:
            selectors.append(selector)
    return tuple(selectors)



def _existing_text_parts(paths: list[Path]) -> list[str]:
    parts: list[str] = []
    for path in paths:
        if path.is_file():
            text = path.read_text(encoding="utf-8").rstrip()
            if text:
                parts.append(text)
    return parts


def _most_specific_prompt_dir(agent_id: str, repo_prompts_root: Path, global_agents_root: Path, project_agents_root: Path) -> Path:
    """Resolve project > global > repo prompt roots for one agent id."""
    for root in (project_agents_root, global_agents_root, repo_prompts_root):
        candidate = root / agent_id
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    return repo_prompts_root / agent_id


def load_agent_prompt_spec(
    agent_id: str,
    *,
    repo_prompts_root: Path,
    global_agents_root: Path,
    project_agents_root: Path,
    agents_files: list[Path] | tuple[Path, ...] = (),
) -> PromptSpec:
    prompt_dir = _most_specific_prompt_dir(agent_id, repo_prompts_root, global_agents_root, project_agents_root)
    spec = load_prompt_spec(prompt_dir)
    agents_parts = _existing_text_parts(list(agents_files))
    if not agents_parts:
        return spec
    system = "\n\n".join([*agents_parts, spec.system.rstrip()]).rstrip() + "\n"
    return PromptSpec(system=system, tool_selectors=spec.tool_selectors)

def load_prompt_spec(prompts_dir: Path) -> PromptSpec:
    if not prompts_dir.is_dir():
        raise FileNotFoundError(
            f"prompts directory missing at {prompts_dir}. "
            f"Drop .md files in there to set the system prompt."
        )
    files = sorted(prompts_dir.glob("*.md"))
    if not files:
        raise FileNotFoundError(
            f"prompts directory {prompts_dir} has no .md files."
        )

    zero_file = next((p for p in files if _is_zero_file(p)), None)
    selectors: tuple[str, ...] = ()
    parts: list[str] = []

    for path in files:
        text = path.read_text(encoding="utf-8")
        body = text.rstrip()
        if path == zero_file:
            frontmatter, body = _split_frontmatter(path, text)
            if frontmatter is not None:
                selectors = _coerce_tool_selectors(path, frontmatter.get("tools"))
        if body:
            parts.append(body)

    return PromptSpec(system="\n\n".join(parts) + "\n", tool_selectors=selectors)



def load_configured_prompt_spec(cfg) -> PromptSpec:
    roots = tuple(getattr(cfg, "prompt_roots", ()))
    if len(roots) >= 3:
        spec = load_agent_prompt_spec(
            cfg.agent_id,
            repo_prompts_root=roots[0],
            global_agents_root=roots[1],
            project_agents_root=roots[2],
            agents_files=getattr(cfg, "agents_files", ()),
        )
    else:
        spec = load_prompt_spec(cfg.prompts_dir)
        agents_parts = _existing_text_parts(list(getattr(cfg, "agents_files", ())))
        if agents_parts:
            spec = PromptSpec(
                system="\n\n".join([*agents_parts, spec.system.rstrip()]).rstrip() + "\n",
                tool_selectors=spec.tool_selectors,
            )
    return _expand_spec(spec, cfg)


def _expand_spec(spec: PromptSpec, cfg) -> PromptSpec:
    """Expand {{VAR}} / !{sub ...} / ```!sub directives in the assembled system prompt."""
    allow_code = bool(getattr(cfg, "allow_inline_code", False))
    system = expand_prompt(spec.system, allow_code=allow_code)
    if system == spec.system:
        return spec
    return PromptSpec(system=system, tool_selectors=spec.tool_selectors)

def load_prompt(prompts_dir: Path) -> str:
    return load_prompt_spec(prompts_dir).system
