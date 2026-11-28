"""Load agent prompt directories: *.md prompt text plus an agent.yaml manifest."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from . import (
    agent_files,
    messages as msgs,
    model_profiles,
    paths,
    secret_patterns,
    settings,
    snippets,
)
from .agent_files import AGENT_MANIFEST
from .cli_options import current
from .config import apply_route
from .promptexpand import expand_prompt, session_variables
from .routing import resolve_model_route
from .toolkit import policy
from .toolkit.registry import registry_for_roots


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
    exchanges: tuple[AgentExchange, ...] = ()
    models: tuple[model_profiles.ModelProfile, ...] = ()  # agent.yaml `models:` entries
    # The profile matched for the model and provider the spec was built for,
    # whether or not it was applied; `for_model` compares against it.
    model_profile: model_profiles.ModelProfile | None = None
    tool_aliases: dict[str, str] = field(default_factory=dict)

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


@dataclass(frozen=True)
class AgentExchange:
    """One numbered user/agent exchange, kept unexpanded until initialization."""
    number: int
    user: str | None = None
    agent: str | None = None


def _load_exchanges(md_files: list[Path]) -> tuple[AgentExchange, ...]:
    numbered: dict[int, dict[str, str]] = {}
    for path in md_files:
        match = agent_files.MESSAGE_FILE.fullmatch(path.stem)
        if match is None:
            continue
        number = int(match[1] or 0)
        role = match[2]
        exchange = numbered.setdefault(number, {})
        if role in exchange:
            raise ValueError(msgs.AGENT_MANIFEST_DUPLICATE_FILE_FOR_EXCHANGE.format(role=role, number=number, path=path))
        with path.open(encoding="utf-8", newline="") as stream:
            exchange[role] = stream.read()
    return tuple(AgentExchange(number, **numbered[number]) for number in sorted(numbered))


_MANIFEST_KEYS = ("model", "secondary_model", "reasoning", "max_tokens", "sampling", "tools", "skills", "models", "aliases")
_MIGRATE_HINT = msgs.AGENT_MANIFEST_MIGRATION_HINT.format()


def _find_yaml_zero_file(prompts_dir: Path) -> Path | None:
    candidates = sorted(path for path in prompts_dir.glob("00*.yaml") if agent_files.is_zero_file(path))
    return candidates[0] if candidates else None


def _load_yaml_manifest(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(msgs.AGENT_MANIFEST_INVALID_YAML_MANIFEST_IN.format(path=path, exc=exc)) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(msgs.AGENT_MANIFEST_YAML_MANIFEST_IN_MUST_BE_A_MAPPING.format(path=path))
    return data


def _refuse_legacy_manifest(md_files: list[Path]) -> None:
    for path in md_files:
        if (
            agent_files.is_zero_file(path)
            and not agent_files.is_message_file(path)
            and not agent_files.is_benchmark_file(path)  # load_benchmarks reads its frontmatter
            and path.read_text(encoding="utf-8").startswith("---")
        ):
            raise ValueError(msgs.AGENT_MANIFEST_FRONTMATTER_IN_IS_NO_LONGER_READ.format(path=path, _MIGRATE_HINT=_MIGRATE_HINT))


def _coerce_skills(path: Path, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(msgs.AGENT_MANIFEST_SKILLS_IN_MUST_BE_A_LIST_OF.format(path=path))
    out: list[str] = []
    for item in raw:
        family, sep, name = str(item).strip().partition(":") if isinstance(item, str) else ("", "", "")
        if not sep or not family.strip() or not name.strip():
            raise ValueError(msgs.AGENT_MANIFEST_SKILLS_ENTRY_IN_IS_NOT_FAMILY_NAME.format(item=item, path=path))
        out.append(item.strip())
    return tuple(out)


def _frontmatter_source(path: Path, text: str) -> tuple[str, str]:
    """The source YAML and body separated by an exact closing delimiter line."""
    first_line_end = text.find("\n")
    close = None if first_line_end == -1 else re.compile(r"(?m)^---(?:\r?\n|$)").search(text, first_line_end + 1)
    if close is None:
        raise ValueError(msgs.AGENT_MANIFEST_FRONTMATTER_IN_IS_MISSING_A_CLOSING.format(path=path))
    return text[first_line_end + 1:close.start()], text[close.end():]


def split_frontmatter(path: Path, text: str) -> tuple[dict[str, Any] | None, str]:
    """The YAML mapping and right-stripped body of an agent prompt file; the
    mapping is None when ``text`` has no frontmatter. Malformed frontmatter
    raises ValueError naming ``path``."""
    if not text.startswith("---"):
        return None, text.rstrip()
    yaml_text, body = _frontmatter_source(path, text)
    try:
        data = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(msgs.AGENT_MANIFEST_INVALID_YAML_FRONTMATTER_IN.format(path=path, exc=exc)) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(msgs.AGENT_MANIFEST_FRONTMATTER_IN_MUST_BE_A_MAPPING.format(path=path))
    return data, body.rstrip()


def _coerce_tool_selectors(path: Path, raw: Any) -> tuple[str, ...]:
    return policy.parse_entries(raw, str(path))


def _coerce_tool_aliases(path: Path, raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: aliases must be a mapping of tool names to public names")
    for canonical, public in raw.items():
        if not isinstance(canonical, str) or not canonical.strip() or canonical.strip() != canonical:
            raise ValueError(f"{path}: invalid canonical tool name in aliases: {canonical!r}")
        if not isinstance(public, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", public):
            raise ValueError(f"{path}: invalid public name for tool {canonical!r}: {public!r}")
    return raw


def _coerce_model(raw: Any) -> str:
    model = str(raw or "").strip()
    return "" if model.lower() == model_profiles.INHERIT else model


def _coerce_max_tokens(path: Path, raw: Any) -> int | None:
    """Per-call output cap from a manifest/frontmatter. <= 0 (e.g. -1) means
    uncapped — fall back to the provider/metadata default (None)."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(msgs.AGENT_MANIFEST_MAX_TOKENS_IN_MUST_BE_AN_INTEGER.format(path=path))
    return raw if raw > 0 else None




def _existing_text_parts(paths: list[Path]) -> list[str]:
    parts: list[str] = []
    for path in paths:
        if path.is_file():
            text = path.read_text(encoding="utf-8").rstrip()
            if text:
                parts.append(text)
    return parts


def _prompt_files(sources: Path | tuple[Path, ...] | list[Path]) -> list[Path]:
    """Assemble source directories or files by filename, highest layer winning."""
    entries = (sources,) if isinstance(sources, Path) else sources
    selected: dict[str, Path] = {}
    for source in entries:
        try:
            candidates = source.iterdir() if source.is_dir() else (source,)
            for path in candidates:
                if path.is_file():
                    selected[path.name] = path
        except OSError as exc:
            raise ValueError(f"agent source {source} is unreadable: {exc}") from exc
    return [selected[name] for name in sorted(selected)]


def load_agent_prompt_spec(
    agent_id: str,
    roots: tuple[Path, ...],
    *,
    agents_files: list[Path] | tuple[Path, ...] = (),
    env: bool = True,
    prompt: str | None = None,
) -> PromptSpec:
    """Load the nearest definition of this agent, with ``agents_files`` ahead of its prompt."""
    spec = load_prompt_spec(paths.agent_dirs(agent_id, roots), env=env, prompt=prompt, agent_id=agent_id)
    return _with_agents_files(spec, agents_files)


def _with_agents_files(spec: PromptSpec, agents_files) -> PromptSpec:
    agents_parts = _existing_text_parts(list(agents_files))
    if not agents_parts:
        return spec
    return _with_system(spec, "\n\n".join([*agents_parts, spec.system.rstrip()]).rstrip() + "\n")


def config_roots(cfg) -> tuple[Path, ...]:
    """The configured agent roots, inferred from the prompt directory when omitted."""
    roots = tuple(getattr(cfg, "prompt_roots", ()) or ())
    if roots:
        return roots
    first = cfg.prompts_dir if isinstance(cfg.prompts_dir, Path) else cfg.prompts_dir[0]
    if ":" in getattr(cfg, "agent_id", ""):
        parts = paths.agent_path(cfg.agent_id).parts
        if first.parts[-len(parts):] == parts:
            return (first.parents[len(parts) - 1],)
    return (first.parent,)


def load_agent_manifest(manifest_path: Path) -> PromptSpec:
    """One agent.yaml as a PromptSpec with an empty system prompt."""
    manifest = _load_yaml_manifest(manifest_path)
    unknown = sorted(str(key) for key in manifest if key not in _MANIFEST_KEYS)
    if unknown:
        raise ValueError(
            msgs.AGENT_MANIFEST_UNKNOWN_KEY_S_IN_ALLOWED.format(value=', '.join(unknown), manifest_path=manifest_path, value2=', '.join(_MANIFEST_KEYS))
        )
    return PromptSpec(
        system="",
        tool_selectors=_coerce_tool_selectors(manifest_path, manifest.get("tools")),
        tool_aliases=_coerce_tool_aliases(manifest_path, manifest.get("aliases")),
        sampling=model_profiles.coerce_sampling(manifest_path, manifest.get("sampling")),
        model=_coerce_model(manifest.get("model")),
        secondary_model=str(manifest.get("secondary_model") or "").strip(),
        reasoning_effort=model_profiles.coerce_reasoning_effort(manifest_path, manifest.get("reasoning")),
        max_output_tokens=_coerce_max_tokens(manifest_path, manifest.get("max_tokens")),
        skills=_coerce_skills(manifest_path, manifest.get("skills")),
        models=model_profiles.parse_profiles(manifest.get("models"), f"{manifest_path} models"),
    )


def load_prompt_spec(
    prompts_dir: Path | tuple[Path, ...] | list[Path], *,
    env: bool = True, prompt: str | None = None, agent_id: str | None = None,
) -> PromptSpec:
    """Load assembled files or directories, with each higher filename replacing
    its lower copy. ``env=False`` skips env files; ``prompt`` replaces prompt files."""
    sources = (prompts_dir,) if isinstance(prompts_dir, Path) else tuple(prompts_dir)
    if not any(source.is_dir() or source.is_file() for source in sources):
        name = agent_id or sources[0].name
        raise FileNotFoundError(f"unknown agent {name!r}; searched " + paths.searched(roots=sources))
    files = _prompt_files(sources)
    md_files = [path for path in files if path.suffix == ".md"]
    legacy = next((path for path in files if path.suffix == ".yaml" and agent_files.is_zero_file(path)), None)
    if legacy is not None:
        raise ValueError(msgs.AGENT_MANIFEST_IS_NO_LONGER_READ.format(legacy=legacy, _MIGRATE_HINT=_MIGRATE_HINT))
    _refuse_legacy_manifest(md_files)
    manifest_path = next((path for path in files if path.name == AGENT_MANIFEST), None)
    spec = load_agent_manifest(manifest_path) if manifest_path is not None else PromptSpec(system="", tool_selectors=())
    parts: list[str] = []
    prompt_placed = False
    for path in md_files:
        kind = agent_files.system_kind(path)
        if kind == agent_files.ENV_KIND and not env:
            continue
        if kind == agent_files.PROMPT_KIND and prompt is not None:
            if not prompt_placed and prompt.rstrip():
                parts.append(prompt.rstrip())
            prompt_placed = True
            continue
        if kind is None:
            continue
        body = path.read_text(encoding="utf-8").rstrip()
        if body:
            parts.append(body)
    if prompt is not None and not prompt_placed and prompt.rstrip():
        parts.insert(0, prompt.rstrip())
    return replace(_with_system(spec, "\n\n".join(parts) + "\n"), exchanges=_load_exchanges(md_files))



def agent_model_applies(cfg, prompt_spec, model: str | None, *, config_pins: bool = True) -> bool:
    """Whether the agent's agent.yaml `model:` picks the model: it names one,
    no ``model`` was given for this run, and the operator pinned none."""
    agent_model = getattr(prompt_spec, "model", "") if prompt_spec is not None else ""
    pinned = getattr(cfg, "explicit_model" if config_pins else "invocation_model", False)
    return bool(agent_model) and model is None and not pinned


def apply_agent_model(cfg, prompt_spec, model: str | None, *, config_pins: bool = True):
    """``cfg`` routed to the agent's agent.yaml `model:`, unless the operator
    pinned a model. -m, a JS_* env var and --extra always pin; a jsrc `MODEL`
    pins only when ``config_pins``."""
    if not agent_model_applies(cfg, prompt_spec, model, config_pins=config_pins):
        return cfg

    route = resolve_model_route(
        prompt_spec.model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", {}),
        explicit_model=True,
        prefix_overrides_provider=not getattr(cfg, "explicit_provider", False),
    )
    return apply_route(cfg, route)


def load_configured_prompt_spec(cfg, *, expand: bool = True) -> PromptSpec:
    """The run's prompt spec, with the model profile that matches ``cfg``'s
    model and provider applied."""
    options = current()
    if options.you_are is not None:
        # _expand_spec writes the whole system prompt.
        spec = PromptSpec(system="", tool_selectors=())
        return _finish_configured_spec(replace(spec, model_profile=_profile_for(spec, cfg)), cfg, expand)
    if options.chat:
        if options.system_prompt_file is not None or options.system_prompt is not None:
            system = options.prompt_text()
        elif options.agent is not None:
            system = load_prompt_spec(cfg.prompts_dir, env=False, agent_id=options.agent).system
        else:
            system = ""
        spec = PromptSpec(system=system, tool_selectors=())
        return _finish_configured_spec(replace(spec, model_profile=_profile_for(spec, cfg)), cfg, expand)
    if options.system_prompt_file is not None:
        base = load_agent_prompt_spec(cfg.agent_id, getattr(cfg, "prompt_roots", ()) or paths.agent_roots(), env=False)
        spec = PromptSpec(system=options.prompt_text(), tool_selectors=base.tool_selectors, models=base.models,
                          tool_aliases=base.tool_aliases)
        return _finish_configured_spec(_with_model_profile(spec, cfg, None), cfg, expand)
    env = not options.skips_env
    agents_files = () if options.skips_agents_md else tuple(getattr(cfg, "agents_files", ()))

    spec = load_prompt_spec(cfg.prompts_dir, env=env, prompt=options.system_prompt, agent_id=getattr(cfg, "agent_id", None))
    spec = _with_agents_files(spec, agents_files)
    spec = _with_model_profile(spec, cfg, lambda agent_id: load_agent_prompt_spec(
        agent_id, config_roots(cfg), agents_files=agents_files, env=env, prompt=options.system_prompt).system)
    return _finish_configured_spec(spec, cfg, expand)


def _profile_for(spec: PromptSpec, cfg) -> model_profiles.ModelProfile | None:
    return model_profiles.choose(
        spec.models, getattr(cfg, "settings", None), getattr(cfg, "model", "") or "", getattr(cfg, "provider_id", None))


def _with_model_profile(spec: PromptSpec, cfg, persona_text) -> PromptSpec:
    """``spec`` with the profile matched for ``cfg``'s model and provider
    applied: its tool chain unless --tools or a zero-tool run sets the tools,
    its `prompt:` agent's prompt text through ``persona_text`` when given, its
    sampling over the agent's and its reasoning in place of the agent's. A
    profile whose chain selects no tool is not applied."""
    options = current()
    profile = _profile_for(spec, cfg)
    spec = replace(spec, model_profile=profile)
    if profile is None:
        return spec
    chain = spec.tool_selectors
    if options.tools is None and not options.zero_tools:
        config = policy.load_tools_config()
        chain = profile.chain(spec.tool_selectors, config)
        registry = registry_for_roots(getattr(cfg, "prompt_roots", ()) or (), flags=policy.schema_flags(cfg))
        chosen = policy.select(registry.tools, options.selectors(chain), config, warn=False)
        if not (chosen.eager or chosen.lazy):
            model_profiles.warn_empty(profile)
            return spec
    if profile.prompt and persona_text is not None:
        spec = _with_system(spec, persona_text(profile.prompt))
    return replace(
        spec,
        tool_selectors=chain,
        sampling={**spec.sampling, **profile.sampling},
        reasoning_effort=profile.reasoning if profile.reasoning is not None else spec.reasoning_effort,
    )


def for_model(spec: PromptSpec, cfg) -> PromptSpec:
    """``spec`` as `load_configured_prompt_spec` builds it for ``cfg``'s model
    and provider. A run loads its spec before agent.yaml `model:`, --model or a
    resumed stamp settle the model; once they have, a model that matches a
    different profile reloads the spec."""
    if _profile_for(spec, cfg) == spec.model_profile:
        return spec
    return load_configured_prompt_spec(cfg, expand=False)


def _finish_configured_spec(spec: PromptSpec, cfg, expand: bool) -> PromptSpec:
    options = current()
    if options.append_system_prompt:
        spec = _with_system(spec, '\n\n'.join((spec.system.rstrip(), *options.append_system_prompt)))
    spec = replace(spec, tool_selectors=options.selectors(spec.tool_selectors),
                   tool_aliases=options.aliases(spec.tool_aliases))
    # Tag references resolve against tools.yaml here, so a bad tag or a tag
    # cycle fails the prompt load with one line instead of a later traceback.
    policy.expand(spec.tool_selectors, policy.load_tools_config(), f"agent {getattr(cfg, 'agent_id', '')!r}")
    if options.tools is not None:
        # The registry the run's surface is selected from; tool entries never
        # select MCP tools, which come only through tool_discovery.
        registry = registry_for_roots(getattr(cfg, "prompt_roots", ()) or (), flags=policy.schema_flags(cfg))
        unmatched = options.unmatched_tools(registry.tools)
        if unmatched:
            raise ValueError(msgs.TOOLS_ENTRY_MATCHES_NOTHING.text(entry=unmatched[0]))
    if expand:
        spec = _expand_spec(spec, cfg)
    return spec


def load_benchmarks(prompts_dir: Path | tuple[Path, ...]) -> list[Benchmark]:
    """Ordered NN-benchmark.md turns from a prompt dir. Each is a clean-slate
    one-shot user turn with an optional `max_tokens` frontmatter override."""
    out: list[Benchmark] = []
    for path in _prompt_files(prompts_dir):
        if path.suffix != ".md" or not agent_files.is_benchmark_file(path):
            continue
        frontmatter, body = split_frontmatter(path, path.read_text(encoding="utf-8"))
        body = body.strip()
        if not body:
            continue
        max_tokens: int | None = None
        max_tokens_set = frontmatter is not None and "max_tokens" in frontmatter
        if max_tokens_set:
            max_tokens = _coerce_max_tokens(path, frontmatter.get("max_tokens"))
        out.append(Benchmark(name=path.stem, prompt=body, max_tokens=max_tokens, max_tokens_set=max_tokens_set))
    return out


def has_benchmarks(prompts_dir: Path | tuple[Path, ...]) -> bool:
    """Whether this agent selects benchmark mode, including empty benchmark files."""
    return any(path.suffix == ".md" and agent_files.is_benchmark_file(path) for path in _prompt_files(prompts_dir))


def expand_agent_text(text: str, cfg, variables: dict[str, str] | None = None) -> str:
    """Expand trusted agent text with this session's built-ins and code policy.
    ``variables`` adds built-ins only this text sees, such as PROMPT."""
    allow_code = bool(getattr(cfg, "allow_inline_code", False))
    timeout_s = int(settings.knob_attr(cfg, "inline_code_timeout_s", "INLINE_CODE_TIMEOUT"))
    max_output_bytes = int(settings.knob_attr(cfg, "max_bash_output_bytes", "SHELL_OUTPUT_MAX"))
    # The prompt's commands (envctx) report the shell the shell tool runs
    # commands in, which is SHELL_PROGRAM, not the operator's $SHELL.
    program = str(settings.knob(getattr(cfg, "settings", None), "SHELL_PROGRAM"))
    environ = dict(os.environ)
    environ["JS_SHELL"] = shutil.which(program) or program
    environ["JS_HOME"] = str(paths.home())
    # JS_ROOT is where js itself is: the directory holding the js package,
    # defaults/ and tools/, so a prompt reaches `$JS_ROOT/tools/envctx.c`
    # wherever js is installed.
    environ["JS_ROOT"] = str(Path(__file__).resolve().parents[1])
    environ[secret_patterns.ENV_VAR] = str(secret_patterns.patterns_file())
    return expand_prompt(
        text,
        allow_code=allow_code,
        env=environ,
        code_env=environ,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
        **({"variables": session_variables(cfg) | (variables or {})} if "%%" in text else {}),
    )


def _expand_spec(spec: PromptSpec, cfg) -> PromptSpec:
    """Expand directives in the assembled system prompt, retaining its source."""
    options = current()
    text = spec.system
    if options.you_are is not None:
        text = snippets.render("model-identity", MODEL=cfg.model.rsplit("/", 1)[-1])
        if options.you_are:
            text += '\n' + options.you_are
        if options.append_system_prompt:
            text += '\n\n' + '\n\n'.join(options.append_system_prompt)
    system = expand_agent_text(text, cfg)
    if system == spec.system:
        return spec
    return replace(spec, system=system, source=spec.source)

def load_prompt(prompts_dir: Path) -> str:
    return load_prompt_spec(prompts_dir).system


def format_prompt_load_error(cfg, exc: Exception) -> str:
    """Turn a persona-load failure into an operator-legible line. A missing prompt
    directory means the named agent doesn't exist anywhere on the search path —
    say so plainly and point at the global agents dir where one is created,
    instead of leaking the raw 'prompts directory missing at <path>'."""
    if isinstance(exc, FileNotFoundError) and isinstance(cfg.prompts_dir, Path) and not cfg.prompts_dir.is_dir():
        agents_dir = paths.global_agents_dir()
        agent_id = getattr(cfg, "agent_id", None)
        if agent_id:
            return msgs.NO_SUCH_AGENT.said(agent=agent_id, agents_dir=agents_dir, new_dir=agents_dir / paths.agent_path(agent_id))
    return msgs.of_error(exc)
