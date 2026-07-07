"""Runtime configuration. Read env once, freeze."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path

from . import paths as _paths
from . import providers as _providers
from . import settings as _settings
from . import routing as _routing

from .sampling import Sampling

_DEFAULT_MODEL = _settings.DEFAULT_MODEL
_DEFAULT_AGENT_ID = "defaultagent"
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_EFFORT_ALIASES = {"min": "low", "none": "none", "off": "none", "0": "none", "": None}

# DeepSeek reasoning: xhigh is forwarded verbatim for deepseek-native models.
# `max` is a real ladder stop (reasoning.py) and is NOT aliased away here —
# reasoning.snap_effort floors it for families that don't serve it.

def _norm_effort(raw: str | None) -> str | None:
    """Normalize a thinking-effort value. off/none->\"none\", unset->None.
    Pass through low|medium|high|minimal|xhigh|max."""
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[v]
    return v  # low | medium | high | minimal | xhigh | max — forwarded as-is to the SDK


def validate_agent_id(agent_id: str) -> str:
    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError("agent id must contain only letters, numbers, '_' or '-'")
    return agent_id


def _env_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _env_int(*names: str, default: int | None = None) -> int | None:
    for name in names:
        raw = os.environ.get(name)
        if raw not in (None, ""):
            return int(raw)
    return default


def _numeric_setting(root: dict, path: tuple[str, ...], default: int | None) -> int | None:
    raw = _settings.get_dotted(root, path, default)
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _sampling_from_extras(extras: list[str] | None) -> Sampling:
    values: dict[str, object] = {}
    for arg in extras or []:
        path, value = _settings.parse_extra_arg(arg)
        if len(path) == 2 and path[0] == "sampling":
            values[path[1]] = value
    return Sampling.from_mapping(values)

# Vision-capable model-name fragments the SDK registry does not cover (proxy-
# and ollama-served ids it has never heard of). Matched case-insensitively as substrings.
_VISION_NAME_HINTS = (
    "-vl", ":vl", "vl-", "vl:", "vision", "llava", "pixtral", "internvl",
    "minicpm-v", "minicpm5", "gemma3", "gemma-3", "gemma4", "gemma-4",
    "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "glm-4v", "glm-4.5v",
    "gemini", "gpt-4o", "gpt-4.1", "gpt-5",
    "claude-3", "claude-4", "claude-opus-4", "claude-sonnet-4", "claude-haiku-4",
)
# Text/audio/image-generation variants that share a vision-family substring but take
# no image *input* — never enable image bytes for these.
_VISION_NAME_ANTI = (
    "coder", "codex", "-code", "embed", "rerank", "guard", "tts", "whisper", "-image",
)


def _vision_by_name(model: str) -> bool:
    name = model.lower()
    if any(anti in name for anti in _VISION_NAME_ANTI):
        return False
    return any(hint in name for hint in _VISION_NAME_HINTS)


def vision_enabled_for_model(model: str) -> bool:
    """Whether image bytes should be sent to ``model``.

    Order: explicit JS_VISION override → curated name heuristic. There is no
    public ai-python model-capability registry in the source inspected."""
    override = os.environ.get("JS_VISION")
    if override is not None:
        parsed = _env_bool(override)
        if parsed is not None:
            return parsed
    return _vision_by_name(model)


@dataclass(frozen=True)
class Config:
    agent_id: str
    agent_dir: Path
    model: str
    provider_id: str | None       # explicit js provider id (e.g. "deepseek")
    provider_base_url: str | None # explicit provider base URL
    provider_api_key: str | None   # explicit provider API key
    reasoning_effort: str | None   # deepseek/o1 thinking: low|medium|high|minimal (None = provider default)
    max_output_tokens: int | None  # per-call max_tokens; None = models.dev metadata or provider default
    max_tool_iterations: int
    max_bash_output_bytes: int
    max_tool_result_bytes: int
    fetch_timeout_s: int
    debug_log: Path | None
    trace: bool            # print the pretty tool-call trace line
    history_file: Path
    sessions_dir: Path
    session_file: Path
    prompts_dir: Path
    inline_code_timeout_s: int = _settings.DEFAULT_INLINE_CODE_TIMEOUT_S
    provider_headers: dict[str, str] = field(default_factory=dict)
    sampling_setscript: Sampling = field(default_factory=Sampling)
    sampling_env: Sampling = field(default_factory=Sampling)
    sampling_cli: Sampling = field(default_factory=Sampling)
    explicit_model: bool = False  # model.id was set by JS_MODEL or config (not the built-in default); gates --agent frontmatter model
    vision_enabled: bool = False
    settings: dict = field(default_factory=dict, compare=False)  # raw merged view, for the runtime
    prompt_roots: tuple[Path, ...] = field(default_factory=tuple, compare=False)
    agents_files: tuple[Path, ...] = field(default_factory=tuple, compare=False)
    project_dir: Path = field(default_factory=Path.cwd, compare=False)
    max_read_lines: int = _settings.DEFAULT_MAX_READ_LINES
    max_line_chars: int = _settings.DEFAULT_MAX_LINE_CHARS
    jsonl_max_line_chars: int = _settings.DEFAULT_JSONL_MAX_LINE_CHARS
    max_file_bytes: int = _settings.DEFAULT_MAX_FILE_BYTES
    task_max_depth: int = _settings.DEFAULT_TASK_MAX_DEPTH
    subagent_max_workers: int = _settings.DEFAULT_SUBAGENT_MAX_WORKERS
    wiki_vault_lock_timeout_s: int = _settings.DEFAULT_WIKI_VAULT_LOCK_TIMEOUT_S
    allow_inline_code: bool = True  # !{sh|python|c ...} inline-code execution; on by default, opt out via --im-a-pussy
    prefer_inherit: bool = False  # subagents inherit the parent's model when true; when false (default) they use the agent's own primary (frontmatter `model:`)
    lock_subagent_model: bool = False  # when true, the main agent cannot pick a subagent model via the task tool — the `model` arg is dropped from the tool description and ignored if passed
    artifact_dir: str | None = None  # artifact library dir; None = ARTIFACT_DIR env or built-in default
    artifact_url: str | None = None  # artifact base URL; None = ARTIFACT_URL env or built-in default
    artifact_bin: str | None = None  # artifact CLI binary; None = ARTIFACT_BIN env or built-in default


def _session_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _reserve_session(agent_dir: Path, sessions_dir: Path) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        path = sessions_dir / f"{_session_timestamp()}-{secrets.token_hex(8)}.jsonl"
        try:
            with path.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            continue
        _write_latest_session(agent_dir, path)
        return path
    raise RuntimeError(f"could not reserve unique session under {sessions_dir}")


def resolve_session_file(sessions_dir: Path, session: str) -> Path:
    """Resolve a user-supplied existing session under sessions_dir."""
    raw_path = Path(session).expanduser()
    resolved_sessions_dir = sessions_dir.resolve(strict=False)

    if raw_path.is_absolute():
        if raw_path.suffix != ".jsonl" or not raw_path.is_file():
            raise ValueError(f"session path must be an existing .jsonl file: {session}")
        resolved_path = raw_path.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_sessions_dir):
            raise ValueError(f"session path must be inside {sessions_dir}: {session}")
        return raw_path

    concrete_path = sessions_dir / (raw_path if raw_path.suffix else raw_path.with_suffix(".jsonl"))
    if concrete_path.suffix != ".jsonl" or not concrete_path.is_file():
        raise ValueError(f"session must identify an existing .jsonl file: {session}")

    resolved_path = concrete_path.resolve(strict=True)
    if not resolved_path.is_relative_to(resolved_sessions_dir):
        raise ValueError(f"session path must be inside {sessions_dir}: {session}")
    return concrete_path


def _write_latest_session(agent_dir: Path, session_file: Path) -> None:
    latest_file = agent_dir / "latest.json"
    tmp_file = agent_dir / f".latest.{secrets.token_hex(6)}.tmp"
    latest_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_text(
        json.dumps(
            {
                "session_file": str(session_file),
                "session_name": session_file.name,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_file, latest_file)


def _reserve_default_session(agent_dir: Path, sessions_dir: Path) -> Path:
    return _reserve_session(agent_dir, sessions_dir)



def _select_prompt_dir(agent_id: str, repo_root: Path, global_root: Path, project_root: Path) -> Path:
    for root in (project_root, global_root, repo_root):
        candidate = root / agent_id
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    return repo_root / agent_id

def _preset_config_paths(
    presets: list[str] | None,
    project_dir: Path,
    *,
    ignore_local_config: bool,
    ignore_global_config: bool,
) -> list[Path]:
    """Resolve ``--preset foo,bar`` to ``jsrc.foo``/``jsrc.bar`` files, in order.

    Each preset name layers on top of the base jsrc files like another `set`
    script; later presets win over earlier ones, and a project ``.js/jsrc.<name>``
    wins over the global ``jsrc.<name>``. Missing files are simply skipped by the
    loader, so an unused preset is a no-op (the CLI warns when a name matches
    nothing)."""
    paths: list[Path] = []
    for raw in presets or []:
        name = raw.strip()
        if not name:
            continue
        if not ignore_global_config:
            paths.append(_paths.global_config_file().with_name(f"jsrc.{name}"))
        if not ignore_local_config:
            paths.append(project_dir / ".js" / f"jsrc.{name}")
    return paths


def from_env(
    *,
    save_session: bool = True,
    extras: list[str] | None = None,
    agent_id: str | None = None,
    session: str | None = None,
    cwd: Path | None = None,
    ignore_local_config: bool = False,
    ignore_global_config: bool = False,
    presets: list[str] | None = None,
) -> Config:
    """Read jsrc + env + CLI extras once, populate everything explicitly.

    Provider/model resolution is centralized here:

    - `JS_MODEL=known-provider/model` selects that provider and strips only the
      known provider prefix when no explicit provider is configured, or when the
      prefix names the same configured provider.
    - Unknown slashy model ids pass through unchanged.
    - `JS_PROVIDER`, `JS_BASE_URL`, and `JS_API_KEY` are the override knobs.
    - Provider-specific env vars are scanned so a fresh shell with
      `DEEPSEEK_API_KEY` works without `js --login`.
    """
    env = os.environ
    pkg = Path(__file__).resolve().parent
    js_root = pkg.parent
    project_dir = (cwd or Path.cwd()).resolve(strict=False)

    config_paths: list[Path] = []
    if not ignore_global_config:
        config_file_path = _paths.global_config_file()
        # RULING J: write the first-run template BEFORE it's read below, so a
        # fresh box's stock wiki.aliases (creative/general) are live on run 1
        # instead of only from run 2 onward.
        _settings.write_default_template(config_file_path)
        config_paths.append(config_file_path)
    if not ignore_local_config:
        config_paths.extend([
            project_dir / ".js" / "jsrc",
            project_dir / ".js" / "jsrc.local",
        ])
    # Presets layer on top of the base jsrc files (still below env / --extra).
    config_paths.extend(_preset_config_paths(
        presets,
        project_dir,
        ignore_local_config=ignore_local_config,
        ignore_global_config=ignore_global_config,
    ))

    jsrc_settings = _settings.collect_settings(
        config_paths=config_paths,
        env={},
        extras=None,
    )
    js_root_settings = _settings.collect_settings(
        config_paths=config_paths,
        extras=extras,
    )
    sampling_setscript = Sampling.from_mapping(jsrc_settings.get("sampling", {}))
    sampling_env = Sampling.from_env(env)
    sampling_cli = _sampling_from_extras(extras)
    raw_model = _settings.get_dotted(js_root_settings, ("model", "id")) or _DEFAULT_MODEL
    explicit_model = bool(env.get("JS_MODEL")) or raw_model != _DEFAULT_MODEL
    _cfg_provider_id = _settings.get_dotted(js_root_settings, ("provider", "id"))
    _cfg_base_url = _settings.get_dotted(js_root_settings, ("provider", "base_url"))
    _cfg_api_key = _settings.get_dotted(js_root_settings, ("provider", "api_key"))
    try:
        route = _routing.resolve_model_route(
            raw_model,
            configured_provider_id=_cfg_provider_id,
            configured_base_url=_cfg_base_url,
            configured_api_key=_cfg_api_key,
            env=env,
            explicit_model=explicit_model,
        )
    except _routing.ProviderNotLoggedInError:
        # The base/default model names a provider the operator never logged into.
        # Don't fail config-build: a -m override, an agent `model:`, or a later
        # /model may replace it, and a plain run surfaces the friendly
        # not-logged-in error at the model boundary (model_client). Carry NO
        # provider so nothing farms the vendor's env keys in the meantime.
        route = _routing.ModelRoute(
            model=raw_model,
            provider_id=None,
            base_url=_cfg_base_url,
            api_key=_cfg_api_key,
        )
    model = route.model
    provider_id = route.provider_id
    provider_base_url = route.base_url
    provider_api_key = route.api_key
    provider_headers = route.headers
    provider_def = _providers.get_provider(provider_id)

    reasoning_effort = _settings.get_dotted(js_root_settings, ("model", "reasoning_effort"))
    if reasoning_effort is None and provider_def is not None and provider_def.reasoning_effort:
        reasoning_effort = provider_def.reasoning_effort

    max_output_tokens = _numeric_setting(js_root_settings, ("model", "max_output_tokens"), None)
    max_tool_iterations = _numeric_setting(js_root_settings, ("limits", "max_tool_iterations"), _settings.DEFAULT_MAX_TOOL_ITERATIONS)
    max_bash_output_bytes = _numeric_setting(js_root_settings, ("limits", "max_bash_output_bytes"), _settings.DEFAULT_MAX_BASH_OUTPUT_BYTES)
    max_tool_result_bytes = _numeric_setting(js_root_settings, ("limits", "max_tool_result_bytes"), _settings.DEFAULT_MAX_TOOL_RESULT_BYTES)
    fetch_timeout_s = _numeric_setting(js_root_settings, ("limits", "fetch_timeout_s"), _settings.DEFAULT_FETCH_TIMEOUT_S)
    inline_code_timeout_s = _numeric_setting(js_root_settings, ("limits", "inline_code_timeout_s"), _settings.DEFAULT_INLINE_CODE_TIMEOUT_S)
    max_read_lines = _numeric_setting(js_root_settings, ("limits", "max_read_lines"), _settings.DEFAULT_MAX_READ_LINES)
    max_line_chars = _numeric_setting(js_root_settings, ("limits", "max_line_chars"), _settings.DEFAULT_MAX_LINE_CHARS)
    jsonl_max_line_chars = _numeric_setting(js_root_settings, ("limits", "jsonl_max_line_chars"), _settings.DEFAULT_JSONL_MAX_LINE_CHARS)
    max_file_bytes = _numeric_setting(js_root_settings, ("limits", "max_file_bytes"), _settings.DEFAULT_MAX_FILE_BYTES)
    task_max_depth = _numeric_setting(js_root_settings, ("limits", "task_max_depth"), _settings.DEFAULT_TASK_MAX_DEPTH)
    subagent_max_workers = _numeric_setting(js_root_settings, ("limits", "subagent_max_workers"), _settings.DEFAULT_SUBAGENT_MAX_WORKERS)
    wiki_vault_lock_timeout_s = _numeric_setting(js_root_settings, ("limits", "wiki_vault_lock_timeout_s"), _settings.DEFAULT_WIKI_VAULT_LOCK_TIMEOUT_S)
    runtime_debug = bool(_settings.get_dotted(js_root_settings, ("runtime", "debug"), False))
    trace = bool(_settings.get_dotted(js_root_settings, ("runtime", "trace"), _settings.DEFAULT_TRACE))
    prefer_inherit = bool(_settings.get_dotted(js_root_settings, ("subagents", "prefer_inherit"), False))
    lock_subagent_model = bool(_settings.get_dotted(js_root_settings, ("subagents", "lock_model"), False))
    artifact_dir = _settings.get_dotted(js_root_settings, ("artifact", "dir"))
    artifact_url = _settings.get_dotted(js_root_settings, ("artifact", "url"))
    artifact_bin = _settings.get_dotted(js_root_settings, ("artifact", "bin"))

    agent_id = validate_agent_id(agent_id or env.get("JS_AGENT", _DEFAULT_AGENT_ID))

    sessions_dir = _paths.sessions_root() / agent_id
    sessions_dir.mkdir(parents=True, exist_ok=True)
    state_dir = _paths.state_root() / agent_id
    state_dir.mkdir(parents=True, exist_ok=True)

    session_name = session if session is not None else env.get("JS_SESSION")
    if session_name:
        session_file = resolve_session_file(sessions_dir, session_name)
    elif save_session:
        session_file = _reserve_default_session(sessions_dir, sessions_dir)
    else:
        session_file = Path(os.devnull)
    debug = state_dir / "debug.log" if runtime_debug else None

    global_agent_files = _paths.global_agents_files()

    return Config(
        agent_id=agent_id,
        agent_dir=sessions_dir,
        model=model,
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
        provider_headers=provider_headers,
        sampling_setscript=sampling_setscript,
        sampling_env=sampling_env,
        sampling_cli=sampling_cli,
        reasoning_effort=_norm_effort(reasoning_effort),
        max_output_tokens=max_output_tokens,
        max_tool_iterations=max_tool_iterations,
        max_bash_output_bytes=max_bash_output_bytes,
        max_tool_result_bytes=max_tool_result_bytes,
        fetch_timeout_s=fetch_timeout_s,
        inline_code_timeout_s=inline_code_timeout_s,
        debug_log=debug,
        trace=trace,
        sessions_dir=sessions_dir,
        session_file=session_file,
        history_file=sessions_dir / ".history",
        prompts_dir=_select_prompt_dir(agent_id, js_root / "prompts", _paths.global_agents_dir(), project_dir / ".js" / "agents"),
        vision_enabled=vision_enabled_for_model(model),
        settings=js_root_settings,
        prompt_roots=(js_root / "prompts", _paths.global_agents_dir(), project_dir / ".js" / "agents"),
        agents_files=tuple(p for p in (*global_agent_files, project_dir / "AGENTS.md", project_dir / "AGENTS.local.md") if p.is_file()),
        project_dir=project_dir,
        max_read_lines=max_read_lines,
        max_line_chars=max_line_chars,
        jsonl_max_line_chars=jsonl_max_line_chars,
        max_file_bytes=max_file_bytes,
        task_max_depth=task_max_depth,
        subagent_max_workers=subagent_max_workers,
        wiki_vault_lock_timeout_s=wiki_vault_lock_timeout_s,
        allow_inline_code=bool(_settings.get_dotted(js_root_settings, ("runtime", "allow_inline_code"), True)),
        prefer_inherit=prefer_inherit,
        lock_subagent_model=lock_subagent_model,
        artifact_dir=artifact_dir,
        artifact_url=artifact_url,
        artifact_bin=artifact_bin,
        explicit_model=explicit_model,
    )
