"""Load agent prompt directories: *.md prompt text plus an agent.yaml manifest."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .promptexpand import expand_prompt
from .toolkit import policy
from . import settings


@dataclass(frozen=True)
class PromptSpec:
    system: str
    tool_selectors: tuple[str, ...]
    sampling: dict[str, Any] = field(default_factory=dict)
    model: str = ""              # preferred/primary model for this agent and the subagents it spawns
    secondary_model: str = ""    # backup model — reserved for a future (non-config) selection flag
    reasoning_effort: str | None = None  # child thinking default; None inherits parent/provider default
    max_output_tokens: int | None = None  # agent-default per-call cap from agent.yaml; None = provider/metadata default
    skills: tuple[str, ...] = ()  # agent.yaml `skills:` entries, family:name or family:*
    # The assembled prompt files before directive expansion. `system` is what the
    # model is sent; for a spec that was never expanded the two are equal.
    source: str = ""

    def __post_init__(self) -> None:
        if not self.source:
            object.__setattr__(self, "source", self.system)


def _with_system(spec: PromptSpec, system: str) -> PromptSpec:
    """`spec` with new unexpanded prompt text; `source` is that same text."""
    return replace(spec, system=system, source=system)


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


AGENT_MANIFEST = "agent.yaml"
_MANIFEST_KEYS = ("model", "secondary_model", "reasoning", "max_tokens", "sampling", "tools", "skills")
_MIGRATE_HINT = "move its settings to agent.yaml beside the prompt (just migrate-agents)"


def _find_yaml_zero_file(prompts_dir: Path) -> Path | None:
    """A pre-agent.yaml manifest (00-tools.yaml and kin). It is refused, never read."""
    candidates = sorted(p for p in prompts_dir.glob("00*.yaml") if _is_zero_file(p))
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


def _refuse_legacy_manifest(prompts_dir: Path, md_files: list[Path]) -> None:
    legacy = _find_yaml_zero_file(prompts_dir)
    if legacy is not None:
        raise ValueError(f"{legacy} is no longer read; {_MIGRATE_HINT}")
    for path in md_files:
        if _is_zero_file(path) and path.read_text(encoding="utf-8").startswith("---"):
            raise ValueError(f"frontmatter in {path} is no longer read; {_MIGRATE_HINT}")


def _coerce_skills(path: Path, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"skills in {path} must be a list of family:name entries")
    out: list[str] = []
    for item in raw:
        family, sep, name = str(item).strip().partition(":") if isinstance(item, str) else ("", "", "")
        if not sep or not family.strip() or not name.strip():
            raise ValueError(f"skills entry {item!r} in {path} is not family:name or family:*")
        out.append(item.strip())
    return tuple(out)


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
    return policy.parse_entries(raw, str(path))


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


def _coerce_reasoning_effort(path: Path, raw: Any) -> str | None:
    if raw is None:
        return None
    # PyYAML's YAML 1.1 resolver parses an unquoted `off` as False.
    if raw is False:
        return "none"
    if not isinstance(raw, str):
        raise ValueError(f"reasoning in {path} must be a string")
    spec = settings.SPEC_BY_KEY["model.reasoning_effort"]
    value, error = settings.coerce_value(spec, raw)
    if error:
        raise ValueError(f"reasoning in {path}: {error}")
    return value



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
        if _has_manifest(candidate) or any(candidate.glob("*.md")):
            return candidate
    return repo_prompts_root / agent_id


def _has_manifest(prompts_dir: Path) -> bool:
    """Whether a prompt dir carries a manifest of its own: agent.yaml, or a
    refused 00-tools.yaml that must fail the load rather than be skipped."""
    return (prompts_dir / AGENT_MANIFEST).is_file() or _find_yaml_zero_file(prompts_dir) is not None


def _find_manifest_dir(agent_id: str, *roots: Path) -> Path | None:
    """Highest-priority root (in the order given) whose agent_id dir actually
    carries a manifest. Used to fall back past a winning prompt dir that
    supplies prompt text but no agent.yaml, so it doesn't silently shadow a
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
        # nearest lower layer's agent.yaml rather than silently booting with
        # zero tools and default model/sampling.
        manifest_dir = _find_manifest_dir(agent_id, project_agents_root, global_agents_root, repo_prompts_root)
        if manifest_dir is not None and manifest_dir != prompt_dir:
            spec = _with_system(load_prompt_spec(manifest_dir), spec.system)
    agents_parts = _existing_text_parts(list(agents_files))
    if not agents_parts:
        return spec
    system = "\n\n".join([*agents_parts, spec.system.rstrip()]).rstrip() + "\n"
    return _with_system(spec, system)

def load_agent_manifest(manifest_path: Path) -> PromptSpec:
    """One agent.yaml as a PromptSpec with an empty system prompt."""
    manifest = _load_yaml_manifest(manifest_path)
    unknown = sorted(str(key) for key in manifest if key not in _MANIFEST_KEYS)
    if unknown:
        raise ValueError(
            f"unknown key(s) {', '.join(unknown)} in {manifest_path}; allowed: {', '.join(_MANIFEST_KEYS)}"
        )
    return PromptSpec(
        system="",
        tool_selectors=_coerce_tool_selectors(manifest_path, manifest.get("tools")),
        sampling=_coerce_sampling(manifest_path, manifest.get("sampling")),
        model=str(manifest.get("model") or "").strip(),
        secondary_model=str(manifest.get("secondary_model") or "").strip(),
        reasoning_effort=_coerce_reasoning_effort(manifest_path, manifest.get("reasoning")),
        max_output_tokens=_coerce_max_tokens(manifest_path, manifest.get("max_tokens")),
        skills=_coerce_skills(manifest_path, manifest.get("skills")),
    )


def load_prompt_spec(prompts_dir: Path) -> PromptSpec:
    if not prompts_dir.is_dir():
        raise FileNotFoundError(
            f"prompts directory missing at {prompts_dir}. "
            f"Drop .md prompt files and an optional {AGENT_MANIFEST} manifest in there."
        )

    md_files = sorted(prompts_dir.glob("*.md"))
    _refuse_legacy_manifest(prompts_dir, md_files)
    manifest_path = prompts_dir / AGENT_MANIFEST
    has_manifest = manifest_path.is_file()
    if not md_files and not has_manifest:
        raise FileNotFoundError(
            f"prompts directory {prompts_dir} has no .md prompt files or {AGENT_MANIFEST}."
        )

    spec = load_agent_manifest(manifest_path) if has_manifest else PromptSpec(system="", tool_selectors=())
    parts: list[str] = []
    for path in md_files:
        if _is_benchmark_file(path):
            continue  # --bench turns, never persona text (see load_benchmarks)
        body = path.read_text(encoding="utf-8").rstrip()
        if body:
            parts.append(body)
    return _with_system(spec, "\n\n".join(parts) + "\n")



def apply_agent_max_tokens(cfg, prompt_spec):
    """Apply the persona default only when config/env/CLI did not set a cap."""
    agent_max = getattr(prompt_spec, "max_output_tokens", None)
    if agent_max is None or cfg.max_output_tokens is not None:
        return cfg
    return replace(cfg, max_output_tokens=agent_max)


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
            spec = _with_system(spec, "\n\n".join([*agents_parts, spec.system.rstrip()]).rstrip() + "\n")
    # Tag references resolve against tools.yaml here, so a bad tag or a tag
    # cycle fails the prompt load with one line instead of a later traceback.
    policy.expand(spec.tool_selectors, policy.load_tools_config(), f"agent {getattr(cfg, 'agent_id', '')!r}")
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
    timeout_s = int(settings.knob_attr(cfg, "inline_code_timeout_s", "limits.inline_code_timeout_s"))
    max_output_bytes = int(settings.knob_attr(cfg, "max_bash_output_bytes", "limits.max_bash_output_bytes"))
    system = expand_prompt(
        spec.system,
        allow_code=allow_code,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    if system == spec.system:
        return spec
    return replace(spec, system=system, source=spec.source)

def load_prompt(prompts_dir: Path) -> str:
    return load_prompt_spec(prompts_dir).system
