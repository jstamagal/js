"""Core tool contracts for the js agent.

The runtime depends on this module, not on individual tool implementations.
Tools are small Python objects with an OpenAI-compatible schema and a handler
that receives a shared ToolContext. The context carries per-session state: the
read ledger behind read-before-write checks (`reads`), undo snapshots, and
search deduplication.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import math
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

from .. import jail as _jail
from .. import turn_settings as _turn_settings
from ..skills import SkillCatalog
from . import reads as _reads
from .reads import ReadLedger
from .undo import UndoStore, session_directory

if TYPE_CHECKING:
    from ..config import Config
    from ..context_budget import TokenState
    from .registry import ToolRegistry


Handler = Callable[..., Any]
# A tool result that carries an image: prefix, host path, mime, text stub.
_IMAGE_RESULT_PREFIX = "IMAGE_RESULT\t"


# What the running call's tool hands the screen beside its result (`view`).
_VIEW: contextvars.ContextVar[dict | None] = contextvars.ContextVar("js_tool_call_view", default=None)


@contextmanager
def call_scope(call_id: str, *, for_model: bool = True):
    """Run the body as tool call ``call_id``. ``for_model`` is False when the
    result goes to code rather than to the model's history."""
    token = _reads.CALL_ID.set(call_id)
    model_token = _reads.FOR_MODEL.set(for_model)
    view_token = _VIEW.set({})
    try:
        yield
    finally:
        _VIEW.reset(view_token)
        _reads.FOR_MODEL.reset(model_token)
        _reads.CALL_ID.reset(token)


def view(**fields: Any) -> None:
    """Hand the screen structured fields that describe the running call's
    result (a path, the text before and after, line numbers, an exit code),
    beside the text the model gets. Outside a `call_scope` it does nothing."""
    holder = _VIEW.get()
    if holder is not None:
        holder.update(fields)


def call_view() -> dict | None:
    """What the running call's tool handed the screen with `view`, or None."""
    holder = _VIEW.get()
    return dict(holder) if holder else None


# The registry the running tool call was dispatched through. The kernel's tool
# bridge dispatches a cell's `tools.<name>(...)` calls through it, so a cell
# reaches exactly the tools, and the argument bans, of the agent that ran it.
_REGISTRY: contextvars.ContextVar[Any] = contextvars.ContextVar("js_tool_registry", default=None)
# What the dispatcher of the running call wants told about calls made on its
# behalf (the bridge's cell calls): the runtime traces and logs them with it.
_OBSERVER: contextvars.ContextVar[Any] = contextvars.ContextVar("js_tool_observer", default=None)


@contextmanager
def registry_scope(registry: Any, observer: Any = None):
    """Run the body as a call dispatched through ``registry``; ``observer``
    is told about the tool calls made on the body's behalf."""
    token = _REGISTRY.set(registry)
    observer_token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(observer_token)
        _REGISTRY.reset(token)


def current_registry() -> Any:
    """The registry the running tool call was dispatched through, or None."""
    return _REGISTRY.get()


def current_observer() -> Any:
    """The observer the running tool call was dispatched with, or None."""
    return _OBSERVER.get()


def call_is_read_only(tool: Tool, args: dict[str, Any]) -> bool:
    """True when calling ``tool`` with ``args`` writes nothing."""
    if tool.read_only:
        return True
    if tool.read_only_when is None:
        return False
    try:
        return bool(tool.read_only_when(args))
    except Exception:  # noqa: BLE001 - odd arguments make the call exclusive
        return False


class Output(str):
    """A tool result that is the content the tool was asked for, such as a
    file's text. A caller that tells a failure by its ERROR prefix treats an
    Output as a value even when the content itself starts with ERROR."""


@dataclass(frozen=True)
class ToolOutput:
    """A tool's result in two parts: ``model``, what the model receives, cut
    to its limits, and ``whole``, everything the call produced, which is what
    the operator sees on the screen, on stdout and in the transcript. A tool
    returns one when it cuts its own result for the model; any other result
    is both at once (`as_output`)."""

    model: Any
    whole: Any = None

    def shown(self) -> Any:
        """What the operator sees: ``whole``, or ``model`` when nothing was cut."""
        return self.model if self.whole is None else self.whole

    def each(self, change: Callable[[Any], Any]) -> ToolOutput:
        """``change`` made to both parts, as a jail path rewrite or a notice is."""
        return ToolOutput(change(self.model), None if self.whole is None else change(self.whole))


def as_output(result: Any) -> ToolOutput:
    """``result`` as a `ToolOutput`."""
    return result if isinstance(result, ToolOutput) else ToolOutput(result)


def each_part(result: Any, change: Callable[[Any], Any]) -> Any:
    """``change`` made to ``result``, or to both parts of a `ToolOutput`."""
    return result.each(change) if isinstance(result, ToolOutput) else change(result)


def for_model(result: Any) -> Any:
    """What the model receives of ``result``: a `ToolOutput`'s ``model``."""
    return result.model if isinstance(result, ToolOutput) else result


@dataclass(frozen=True)
class ToolResult:
    """A provider-facing mixed tool result plus a safe persistence descriptor."""

    blocks: list[dict[str, Any]]
    is_error: bool = False

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> ToolResult:
        return cls([{"type": "text", "text": text}], is_error=is_error)

    def dehydrated(self) -> str:
        lines: list[str] = []
        for block in self.blocks:
            kind = str(block.get("type", "unknown"))
            if kind == "text":
                lines.append(str(block.get("text", "")))
            elif kind in {"image", "audio"}:
                lines.append('[{kind} {value_1} omitted from history]'.format(kind=kind, value_1=block.get('mimeType', 'application/octet-stream')))
            elif kind == "resource_link":
                lines.append('[resource link {block_get_name}: {block_get_uri}]'.format(block_get_name=block.get('name', ''), block_get_uri=block.get('uri', '')))
            elif kind == "resource":
                resource = block.get("resource")
                if isinstance(resource, dict):
                    uri = resource.get("uri", "")
                    text = resource.get("text")
                    lines.append(f'[embedded resource {uri}]' + (f'\n{text}' if isinstance(text, str) else ' [binary omitted]'))
            elif kind == "structured":
                lines.append(compact_json(block.get("value")))
            else:
                lines.append(f'[{kind} content omitted from history]')
        value = "\n".join(lines)
        return f"ERROR: {value}" if self.is_error and not value.startswith("ERROR") else value


@dataclass(frozen=True)
class CatalogEntry:
    """Compact metadata for one discoverable turn-scoped capability."""

    id: str
    name: str
    description: str
    kind: str
    source: str
    loadable: bool = True
    # Words discovery ranks on but never shows: the full description and the
    # schema's property names and descriptions.
    search_text: str = ""


@dataclass(frozen=True)
class Tool:
    """OpenAI-compatible tool declaration plus Python handler."""

    name: str
    description: str
    handler: Handler
    params: dict[str, dict]
    required: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    input_schema: dict[str, Any] | None = None
    # True when the tool writes nothing: no files, no processes, no remote
    # state. It defines the intrinsic `tag:read_only` of tools.yaml.
    read_only: bool = False
    # For a tool that writes only under some arguments: True when a call with
    # these arguments writes nothing. `tag:read_only` does not match such a
    # tool; dispatch runs its read-only calls beside other read-only calls.
    read_only_when: Callable[[dict[str, Any]], bool] | None = None
    # Catalog label shown by tool_discovery (fs, shell, search, agent, ...).
    source: str = "native"
    # Maps the arguments a model sent to the ones the schema declares, before
    # schema coercion and validation; it returns a new dict and leaves its
    # input as it was. None when the tool takes its arguments as sent.
    prepare_args: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def openai_spec(self) -> dict:
        parameters = self.input_schema if self.input_schema is not None else {
            "type": "object",
            "properties": self.params,
            "required": list(self.required),
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


@dataclass
class TurnStatus:
    """What the running turn is doing right now, for the REPL status bar.

    The runtime writes it; the bar reads it on each repaint. A read may land
    between two writes from another thread, so every field is a plain value and
    no reader relies on two fields agreeing.
    """

    output_tokens: int = 0       # this turn so far, reasoning included
    tool: str = ""               # registry name of the executing tool; "" when none
    tool_extra: int = 0          # parallel siblings of `tool` in the same batch
    tool_started: float = 0.0    # time.monotonic() when the batch started
    compacting: bool = False
    waiting_since: float = 0.0   # time.monotonic() when the request waiting for its first byte was sent; 0 when none
    retry: int = 0               # the retry being waited for; 0 when none
    retry_of: int = 0            # the retries the budget allows
    retry_at: float = 0.0        # time.monotonic() when that retry is sent
    _settled: int = field(default=0, repr=False)     # tokens of the finished calls
    _chars: int = field(default=0, repr=False)       # streamed chars of this call

    def first_byte_wait(self, since: float) -> None:
        """A model request went out at `since` and waits for its first byte."""
        self.waiting_since = since

    def first_byte_done(self) -> None:
        self.waiting_since = 0.0

    def stream(self, text: str) -> None:
        """A reasoning or text chunk arrived: estimate at four chars a token."""
        self._chars += len(text)
        self.output_tokens = self._settled + self._chars // 4

    def settle(self, output_tokens: int) -> None:
        """The call ended: replace this call's estimate with the provider's count."""
        self._settled += output_tokens if output_tokens > 0 else self._chars // 4
        self._chars = 0
        self.output_tokens = self._settled

    def tool_begin(self, names: list[str]) -> None:
        self.tool_started = time.monotonic()
        self.tool_extra = max(0, len(names) - 1)
        self.tool = names[0] if names else ""

    def tool_end(self) -> None:
        self.tool = ""
        self.tool_extra = 0
        self.tool_started = 0.0

    def retry_wait(self, n: int, of: int, delay: float) -> None:
        self.retry_at = time.monotonic() + delay
        self.retry_of = of
        self.retry = n

    def retry_done(self) -> None:
        self.retry = 0
        self.retry_of = 0
        self.retry_at = 0.0

    def reset(self) -> None:
        for name, spec in self.__dataclass_fields__.items():
            setattr(self, name, spec.default)


@dataclass
class ToolContext(_turn_settings.ContextSettings):
    """Mutable state shared across tool calls in a js process. The per-turn
    settings are fields of the ``ContextSettings`` base, one per ``on_context``
    row of ``js.turn_settings.TURN_SETTINGS``."""

    cwd: Path = field(default_factory=Path.cwd)
    vision_enabled: bool = False
    model: str = ""                       # model id, for toolbox revision provenance
    kernel_session: Any = None            # the live IPython kernel, one per process
    lsp_configuration: Any = field(default=None, repr=False)
    subagent_assembly: Any = field(default=None, repr=False)
    skill_catalog: SkillCatalog | None = field(default=None, repr=False)
    swarm: Any = None                     # js.swarm.Agent when this process runs with --swarm
    # The `on` table whose tool_call handlers vet the calls of subagents started
    # through this context; set by the turn that runs with it.
    tool_call_hooks: Any = field(default=None, repr=False)
    # The approval (js.events.Approval) that holds the calls of this turn, of
    # the subagents it starts and of its kernel cells; None runs every call.
    tool_approval: Any = field(default=None, repr=False)
    # The turn's Config with the model, provider and effort the turn runs on;
    # None until a turn sets it. Subagents started here build theirs from it.
    config: Config | None = field(default=None, repr=False)
    # The registry the turn dispatches through; None until a turn sets it.
    tool_registry: ToolRegistry | None = field(default=None, repr=False)
    agent_id: str = ""
    task_depth: int = 0                   # subagent nesting: 0 on the main turn, parent's + 1 on a child
    # The context size the turn last measured, in tokens, and whether that
    # number came from the provider's usage rather than an estimate.
    context_tokens: int = 0
    context_tokens_used_provider_usage: bool = False
    tokens_until_compaction: int | None = None
    compacted_during_turn: bool = False
    # Token accounting kept across the turns of this context; the runtime
    # creates it on the first turn.
    context_budget_state: TokenState | None = field(default=None, repr=False)
    # What the model has seen of each file; a subagent's context has its own.
    reads: ReadLedger = field(default_factory=ReadLedger, repr=False)
    # The state of each path before a tool changed it, for undo.
    undo: UndoStore = field(default_factory=UndoStore, repr=False)
    search_cache: dict[str, str] = field(default_factory=dict)
    terminal_sessions: dict[str, Any] = field(default_factory=dict)
    last_prompt_tokens: int = 0
    last_cached_tokens: int = 0
    last_output_tokens: int = 0
    last_max_output_tokens: int | None = None
    last_incomplete_reason: str | None = None
    # Wall-clock time the last main-conversation response finished; None
    # until one has. Compaction reads it as the age of the prompt cache.
    last_request_at: float | None = None
    # Cache-read tokens of the last response and the model that served it:
    # the baseline the next comparable response is measured against. None
    # after a deliberate history rewrite.
    cache_read_baseline: int | None = None
    cache_read_model: str = ""
    # Automatic summaries that failed in a row; COMPACTION_FAILURES_MAX
    # of them pause automatic compaction until a summary succeeds.
    summary_failures: int = 0
    turn_status: TurnStatus = field(default_factory=TurnStatus)
    net_label: str = ""                   # "Subagent N" on a fan-out child; "" on the main turn
    # The sessions above a `task` worker, nearest first: its model calls are
    # charged to each of them as well as to its own (js.usage). An unsaved
    # session above a worker appears as its in-memory key (usage.session_key).
    usage_chain: tuple[Path | str, ...] = ()
    # Names this context as the owner of the background tasks it starts; only
    # the owner can poll, wait on or kill them.
    task_owner: str = field(default_factory=lambda: secrets.token_hex(8), repr=False)

    def resolve_path(self, raw: str | os.PathLike[str], *, write: bool = False,
                     follow: bool = True) -> Path:
        """``raw`` as an absolute host path, symlinks resolved (all but the last
        component when ``follow`` is false). Under `js -C` it raises JailError
        for a path the jail does not show, or shows read-only when ``write``."""
        path = Path(os.path.expanduser(str(raw)))
        if not path.is_absolute():
            path = self.cwd / path
        jail = _jail.active()
        if jail is not None:
            return jail.confine(path, write=write, follow=follow, setting=self.jail_bind,
                                hide=self.jail_hide)
        if not follow:
            return Path(os.path.abspath(path))
        return path.resolve()

    def configure_snapshot_store(
        self,
        agent_id: str,
        session_file: Path,
        *,
        state_dir: Path | None = None,
    ) -> None:
        """Attach this context to one session's bounded persistent undo store,
        and keep the read ledger beside it."""
        store = session_directory(agent_id, session_file, state_dir=state_dir)
        previous = self.undo.directory
        if store == previous:
            return
        # Reads made before the context had a session carry into its first one.
        if previous is not None:
            self.reads.clear()
        self.undo.attach(store)
        self.reads.persist_to(None if store is None else store / "reads.json")

    def invalidate_search_cache(self) -> None:
        """Drop memoized fs_search results after anything may have changed the tree.

        A `fs_search` entry is keyed on the search arguments plus the root's
        `(mtime_ns, size, inode)` when the root is a regular file; a directory
        root is not memoized at all, because a directory's own stat does not move
        when a nested file changes. The `ast_search` memo is keyed on its
        arguments only. Without invalidation a model that edits a file and
        re-runs the same search gets the PRE-EDIT hit list back, labelled
        `[deduplicated repeated search]` — it looks like a confirmation that
        nothing changed. Every mutating fs tool funnels through `snapshot()`;
        `shell` clears it directly because a command can touch anything, and
        `task` clears it when a subagent returns for the same reason."""
        self.search_cache.clear()

    def snapshot(self, path: Path) -> None:
        """Keep ``path``'s current state for undo, with what the model had
        seen of it; memoized searches go stale."""
        self.invalidate_search_cache()
        self.undo.capture(path, self.reads.coverage(path))


def coerce_value(value: Any, schema_type: str | None) -> Any:
    if schema_type is None or value is None:
        return value
    numeric = schema_type in ("integer", "number")
    if numeric and isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{schema_type} argument must be finite")
    if schema_type == "integer" and isinstance(value, float) and not value.is_integer():
        raise ValueError("integer argument must be a whole number")
    try:
        if schema_type == "integer" and not isinstance(value, bool) and not isinstance(value, int):
            return int(value)
        if schema_type == "number" and not isinstance(value, (int, float, bool)):
            value = float(value)
        if schema_type == "string" and not isinstance(value, str):
            return str(value)
        if schema_type == "boolean" and not isinstance(value, bool):
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"1", "true", "yes", "y", "on"}:
                    return True
                if lowered in {"0", "false", "no", "n", "off"}:
                    return False
    except (TypeError, ValueError):
        pass
    if numeric and isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{schema_type} argument must be finite")
    return value


def call_tool(tool: Tool, args: dict[str, Any], context: ToolContext) -> Any:
    """Filter/coerce model args and invoke a tool handler."""

    sig = inspect.signature(tool.handler)
    known = set(sig.parameters)
    has_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())
    # Full-schema **kwargs handlers carry model arguments, including a
    # "context" key admitted by patternProperties or additionalProperties.
    # An explicitly named context parameter remains the injection slot unless
    # it is also a declared model-facing property.
    declares_context = "context" in tool.params or (
        tool.input_schema is not None and has_var_kwargs and "context" not in known
    )
    filtered: dict[str, Any] = {}
    for key, value in args.items():
        if key not in known and not has_var_kwargs:
            continue
        if key == "context" and not declares_context:
            continue
        schema_type = tool.params.get(key, {}).get("type")
        try:
            filtered[key] = coerce_value(value, schema_type)
        except ValueError as exc:
            return f"ERROR: invalid argument {key!r}: {exc}"
    if "context" in known and not declares_context:
        filtered["context"] = context
    try:
        result = tool.handler(**filtered)
    except _jail.JailError as exc:
        return _jail.Refusal(f"ERROR: {exc}")
    notices = context.undo.consume_notices()
    if notices:
        rendered = "\n".join(f'WARNING: {notice}' for notice in notices)
        result = each_part(result, lambda text: _with_notices(text, rendered))
    return each_part(result, _as_jail_shows)


def _with_notices(result: Any, notices: str) -> Any:
    """A text result with the snapshot notices after it; an `Output` stays one."""
    if not isinstance(result, str):
        return result
    text = f"{result}\n{notices}"
    return Output(text) if isinstance(result, Output) else text


def _as_jail_shows(result: Any) -> Any:
    """``result`` with host paths into the jail's /tmp and ~/.js/tmp named as
    the jail shows them. The path field of an image marker stays the host
    path: the model client reads the image from it. An `Output` stays one."""
    if _jail.active() is None or not isinstance(result, str) or isinstance(result, _jail.Refusal):
        return result
    if result.startswith(_IMAGE_RESULT_PREFIX):
        prefix, host, rest = (result.split("\t", 2) + ["", ""])[:3]
        shown = f"{prefix}\t{host}\t{_jail.shown(rest)}"
    else:
        shown = _jail.shown(result)
    if shown == result:
        return result
    return Output(shown) if isinstance(result, Output) else shown


async def call_tool_async(tool: Tool, args: dict[str, Any], context: ToolContext) -> Any:
    """Invoke either a native sync handler or a cancelable async handler."""
    result = call_tool(tool, args, context)
    return each_part(await result, _as_jail_shows) if inspect.isawaitable(result) else result


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
