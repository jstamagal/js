"""Load agent prompts and optional YAML tool manifests."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .promptexpand import expand_prompt


@dataclass(frozen=True)
class PromptSpec:
    system: str
    tool_selectors: tuple[str, ...]
    sampling: dict[str, Any] = field(default_factory=dict)
    model: str = ""              # preferred/primary model for this agent and the subagents it spawns
    secondary_model: str = ""    # backup model — reserved for a future (non-config) selection flag
    max_output_tokens: int | None = None  # agent-default per-call cap from 00-tools.yaml; None = provider/metadata default


@dataclass(frozen=True)
class Benchmark:
    """One clean-slate benchmark turn from a NN-benchmark.md file."""
    name: str                    # file stem, e.g. "02-benchmark"
    prompt: str                  # the one-shot user turn (file body)
    max_tokens: int | None       # coerced per-benchmark cap; None = uncapped/default
    max_tokens_set: bool         # whether the frontmatter set max_tokens (distinguishes absent from -1)


def _is_zero_file(path: Path) -> bool:
    stem = path.stem
    return stem == "00" or stem.startswith("00-") or stem.startswith("00_")


def _is_benchmark_file(path: Path) -> bool:
    """A NN-benchmark.md (or bare benchmark.md) file: a --bench turn, never part
    of the system prompt. Excluded universally so `--agent` does not suck
    benchmark bodies into the persona."""
    stem = path.stem
    return stem == "benchmark" or stem.endswith("-benchmark") or stem.endswith("_benchmark")


_DEPRECATED_MD_MANIFEST_NOTES: set[Path] = set()


def _find_yaml_zero_file(prompts_dir: Path) -> Path | None:
    candidates = sorted(p for p in prompts_dir.glob("00*.yaml") if _is_zero_file(p))
    preferred = prompts_dir / "00-tools.yaml"
    if preferred in candidates:
        return preferred
    return candidates[0] if candidates else None


def _load_yaml_manifest(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML manifest in {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML manifest in {path} must be a mapping")
    return data


def _emit_deprecated_md_manifest_note(path: Path) -> None:
    key = path.resolve()
    if key in _DEPRECATED_MD_MANIFEST_NOTES:
        return
    _DEPRECATED_MD_MANIFEST_NOTES.add(key)
    print(
        "NOTE: 00-tools.md frontmatter manifests are deprecated in favor of "
        f"00-tools.yaml (sunset after 2 releases): {path}",
        file=sys.stderr,
    )


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


# Sampling params an agent may set in its YAML manifest. Transport-specific
# filtering happens later from the typed Sampling object; loading a prompt spec
# must never mutate process environment.
_SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "repetition_penalty",
    "presence_penalty",
)


def _coerce_max_tokens(path: Path, raw: Any) -> int | None:
    """Per-call output cap from a manifest/frontmatter. <= 0 (e.g. -1) means
    uncapped — fall back to the provider/metadata default (None)."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"max_tokens in {path} must be an integer")
    return raw if raw > 0 else None


def _coerce_sampling(path: Path, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"sampling frontmatter in {path} must be a mapping")
    out: dict[str, Any] = {}
    for key, val in raw.items():
        if key not in _SAMPLING_KEYS:
            raise ValueError(
                f"unknown sampling key '{key}' in {path}; allowed: {', '.join(_SAMPLING_KEYS)}"
            )
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"sampling.{key} in {path} must be a number")
        out[key] = val
    return out




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
        if not candidate.is_dir():
            continue
        if any(candidate.glob("*.md")) or _find_yaml_zero_file(candidate) is not None:
            return candidate
    return repo_prompts_root / agent_id


def _has_manifest(prompts_dir: Path) -> bool:
    """Whether a prompt dir carries its own tool manifest (00-tools.yaml, or a
    deprecated 00*.md with frontmatter) — independent of whether it has any
    other prompt text."""
    if _find_yaml_zero_file(prompts_dir) is not None:
        return True
    return any(_is_zero_file(p) for p in prompts_dir.glob("*.md"))


def _find_manifest_dir(agent_id: str, *roots: Path) -> Path | None:
    """Highest-priority root (in the order given) whose agent_id dir actually
    carries a manifest. Used to fall back past a winning prompt dir that
    supplies prompt text but no 00-tools.yaml, so it doesn't silently shadow a
    lower layer's tool selection."""
    for root in roots:
        candidate = root / agent_id
        if candidate.is_dir() and _has_manifest(candidate):
            return candidate
    return None


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
    if not _has_manifest(prompt_dir):
        # This dir won on prompt text alone (e.g. a project override that only
        # tweaks wording) and carries no manifest of its own — fall back to the
        # nearest lower layer's 00-tools.yaml rather than silently booting with
        # zero tools and default model/sampling.
        manifest_dir = _find_manifest_dir(agent_id, project_agents_root, global_agents_root, repo_prompts_root)
        if manifest_dir is not None and manifest_dir != prompt_dir:
            manifest_spec = load_prompt_spec(manifest_dir)
            spec = PromptSpec(
                system=spec.system,
                tool_selectors=manifest_spec.tool_selectors,
                sampling=manifest_spec.sampling,
                model=manifest_spec.model,
                secondary_model=manifest_spec.secondary_model,
                max_output_tokens=manifest_spec.max_output_tokens,
            )
    agents_parts = _existing_text_parts(list(agents_files))
    if not agents_parts:
        return spec
    system = "\n\n".join([*agents_parts, spec.system.rstrip()]).rstrip() + "\n"
    return PromptSpec(system=system, tool_selectors=spec.tool_selectors, sampling=spec.sampling, model=spec.model, secondary_model=spec.secondary_model, max_output_tokens=spec.max_output_tokens)

def load_prompt_spec(prompts_dir: Path) -> PromptSpec:
    if not prompts_dir.is_dir():
        raise FileNotFoundError(
            f"prompts directory missing at {prompts_dir}. "
            f"Drop .md prompt files and an optional 00-tools.yaml manifest in there."
        )

    md_files = sorted(prompts_dir.glob("*.md"))
    yaml_zero_file = _find_yaml_zero_file(prompts_dir)
    if not md_files and yaml_zero_file is None:
        raise FileNotFoundError(
            f"prompts directory {prompts_dir} has no .md prompt files or 00*.yaml manifest."
        )

    markdown_zero_file = next((p for p in md_files if _is_zero_file(p)), None)
    selectors: tuple[str, ...] = ()
    sampling: dict[str, Any] = {}
    model: str = ""
    secondary_model: str = ""
    max_output_tokens: int | None = None
    parts: list[str] = []

    if yaml_zero_file is not None:
        manifest = _load_yaml_manifest(yaml_zero_file)
        selectors = _coerce_tool_selectors(yaml_zero_file, manifest.get("tools"))
        sampling = _coerce_sampling(yaml_zero_file, manifest.get("sampling"))
        model = str(manifest.get("model") or "").strip()
        secondary_model = str(manifest.get("secondary_model") or "").strip()
        max_output_tokens = _coerce_max_tokens(yaml_zero_file, manifest.get("max_tokens"))

    for path in md_files:
        if yaml_zero_file is not None and _is_zero_file(path):
            continue
        if _is_benchmark_file(path):
            continue  # --bench turns, never persona text (see load_benchmarks)
        text = path.read_text(encoding="utf-8")
        body = text.rstrip()
        if yaml_zero_file is None and path == markdown_zero_file:
            _emit_deprecated_md_manifest_note(path)
            frontmatter, body = _split_frontmatter(path, text)
            if frontmatter is not None:
                selectors = _coerce_tool_selectors(path, frontmatter.get("tools"))
                sampling = _coerce_sampling(path, frontmatter.get("sampling"))
                model = str(frontmatter.get("model") or "").strip()
                secondary_model = str(frontmatter.get("secondary_model") or "").strip()
                max_output_tokens = _coerce_max_tokens(path, frontmatter.get("max_tokens"))
        if body:
            parts.append(body)

    return PromptSpec(
        system="\n\n".join(parts) + "\n",
        tool_selectors=selectors,
        sampling=sampling,
        model=model,
        secondary_model=secondary_model,
        max_output_tokens=max_output_tokens,
    )



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
                sampling=spec.sampling,
                model=spec.model,
                secondary_model=spec.secondary_model,
                max_output_tokens=spec.max_output_tokens,
            )
    spec = _expand_spec(spec, cfg)
    return spec


def resolve_agent_prompt_dir(cfg) -> Path:
    """The prompt directory `load_configured_prompt_spec` would load for this
    agent (project > global > repo). --bench reads its NN-benchmark.md files from
    the same resolved dir as the persona."""
    roots = tuple(getattr(cfg, "prompt_roots", ()))
    if len(roots) >= 3:
        return _most_specific_prompt_dir(cfg.agent_id, roots[0], roots[1], roots[2])
    return cfg.prompts_dir


def load_benchmarks(prompts_dir: Path) -> list[Benchmark]:
    """Ordered NN-benchmark.md turns from a prompt dir. Each is a clean-slate
    one-shot user turn with an optional `max_tokens` frontmatter override."""
    if not prompts_dir.is_dir():
        return []
    out: list[Benchmark] = []
    for path in sorted(prompts_dir.glob("*.md")):
        if not _is_benchmark_file(path):
            continue
        frontmatter, body = _split_frontmatter(path, path.read_text(encoding="utf-8"))
        body = body.strip()
        if not body:
            continue
        max_tokens: int | None = None
        max_tokens_set = frontmatter is not None and "max_tokens" in frontmatter
        if max_tokens_set:
            max_tokens = _coerce_max_tokens(path, frontmatter.get("max_tokens"))
        out.append(Benchmark(name=path.stem, prompt=body, max_tokens=max_tokens, max_tokens_set=max_tokens_set))
    return out


def _expand_spec(spec: PromptSpec, cfg) -> PromptSpec:
    """Expand {{VAR}} / !{sub ...} / ```!sub directives in the assembled system prompt."""
    allow_code = bool(getattr(cfg, "allow_inline_code", False))
    timeout_s = int(getattr(cfg, "inline_code_timeout_s", 300))
    max_output_bytes = int(getattr(cfg, "max_bash_output_bytes", 256 * 1024))
    system = expand_prompt(
        spec.system,
        allow_code=allow_code,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    if system == spec.system:
        return spec
    return PromptSpec(system=system, tool_selectors=spec.tool_selectors, sampling=spec.sampling, model=spec.model, secondary_model=spec.secondary_model, max_output_tokens=spec.max_output_tokens)

def load_prompt(prompts_dir: Path) -> str:
    return load_prompt_spec(prompts_dir).system
