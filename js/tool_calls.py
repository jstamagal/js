"""The tool calls a model made, made fit to run and to record: aliases resolved, arguments repaired and canonical, a batch normalized, the assistant message that carries them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

import ai

from . import held, snippets, tool_args
from .toolkit.core import ToolContext, compact_json
from .toolkit.registry import ToolRegistry
from .turn_call import dangling_arguments


def _usable_tool_aliases(alias_map: dict[str, str], tool_names: set[str]) -> dict[str, str]:
    original_name_owners = {name.lower(): name for name in tool_names}
    reserved_names = {"tool_discovery"}
    return {
        canon: alias
        for canon, alias in alias_map.items()
        if canon in tool_names
        and (alias.lower() not in reserved_names or canon == alias.lower())
        and (original_name_owners.get(alias.lower()) in (None, canon))
    }


def resolve_alias_profile(
    settings: dict,
    model: str,
    provider_id: str | None,
    registry: ToolRegistry | None = None,
) -> dict[str, str]:
    """Pick the canonical->alias map for the first matching tool alias profile.

    Profiles live under ``[[TOOL_ALIASES]]`` in config; each entry has
    a ``match`` string or list of case-insensitive substrings tested against the
    model id and provider id, plus an ``aliases`` table mapping canonical tool
    names to the model-facing names. The first profile whose ``match`` hits
    wins. No profiles configured (the default) → empty map → default tool
    names. When a registry is provided, matching profiles with no usable
    aliases are skipped.
    """
    profiles = (settings or {}).get("TOOL_ALIASES")
    if not isinstance(profiles, list):
        return {}
    haystacks = [model.lower()]
    if provider_id:
        haystacks.append(str(provider_id).lower())
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        match = profile.get("match")
        if isinstance(match, str):
            match = [match]
        aliases = profile.get("aliases")
        if not isinstance(aliases, dict) or not isinstance(match, list):
            continue
        needles = [str(m).strip().lower() for m in match if str(m).strip()]
        if needles and any(n in h for n in needles for h in haystacks):
            alias_map = {str(k): str(v) for k, v in aliases.items() if str(v).strip()}
            if registry is not None:
                alias_map = _usable_tool_aliases(alias_map, set(registry.by_name))
                if not alias_map:
                    continue
            return alias_map
    return {}


def aliased_tool_specs(specs: list[dict], alias_map: dict[str, str]) -> list[dict]:
    """Rewrite outgoing tool schema names and descriptions per ``alias_map``.

    Each spec's function name is swapped for its alias, and backtick-wrapped
    canonical names inside every tool description are rewritten so the
    cross-references the model reads stay consistent. ``specs`` is returned
    untouched (same object) when ``alias_map`` is empty."""
    if not alias_map:
        return specs
    original_names = {
        fn_name
        for spec in specs
        if isinstance((fn := spec.get("function")), dict)
        and isinstance((fn_name := fn.get("name")), str)
    }
    usable_aliases = _usable_tool_aliases(alias_map, original_names)
    if not usable_aliases:
        return specs
    desc_subs = {f"`{canon}`": f"`{alias}`" for canon, alias in usable_aliases.items()}
    transformed: list[dict] = []
    for spec in specs:
        cloned = json.loads(json.dumps(spec))
        fn = cloned.get("function", {})
        name = fn.get("name")
        if name in usable_aliases:
            fn["name"] = usable_aliases[name]
        desc = fn.get("description")
        if isinstance(desc, str) and desc:
            for needle, repl in desc_subs.items():
                if needle in desc:
                    desc = desc.replace(needle, repl)
            fn["description"] = desc
        transformed.append(cloned)
    return transformed


def canonical_tool_call_name(name: str, registry: ToolRegistry) -> str:
    tool = registry.resolve(name)
    if tool is not None:
        return tool.name
    # An unloaded lazy tool has no Tool object in this dispatch batch, but
    # its alias still maps to a canonical name; persisted history must
    # record the canonical, never a configured alias.
    return registry.aliases.get(str(name).strip().lower(), name)


def pending_with_name(pc: PendingToolCall, name: str) -> PendingToolCall:
    return PendingToolCall(
        id=pc.id,
        name=name,
        arg_chunks=list(pc.arg_chunks),
        validation_error=pc.validation_error,
        unavailable=pc.unavailable,
        refused=pc.refused,
        called_name=pc.called_name,
    )


@dataclass
class PendingToolCall:
    id: str
    name: str = ""
    arg_chunks: list[str] = field(default_factory=list)
    validation_error: str | None = None
    unavailable: bool = False
    # A tools.yaml argument ban matched; validation_error is the ERROR line.
    refused: bool = False
    called_name: str = ""

    def arguments(self) -> str:
        return "".join(self.arg_chunks)


@dataclass(frozen=True)
class _ToolBatchStats:
    received: int
    retained: int
    duplicate: int
    invalid: int
    capped: int
    # (name the model sent, published tool it was dispatched as) per call
    # whose proxy-cloaked name was repaired.
    repaired: tuple[tuple[str, str], ...] = ()


def normalize_tool_call_batch(
    calls: list[PendingToolCall],
    registry: ToolRegistry,
    limit: int,
    context: ToolContext | None = None,
) -> tuple[list[PendingToolCall], _ToolBatchStats]:
    """Canonicalize, deduplicate, validate, and cap one assistant batch."""
    retained: list[PendingToolCall] = []
    seen: set[tuple[str, str]] = set()
    duplicate = invalid = capped = 0
    repaired: list[tuple[str, str]] = []
    scheduled_loads = set()
    for call in calls:
        if canonical_tool_call_name(call.name, registry) == "tool_discovery":
            try:
                args = repair_jsonish(call.arguments())
            except ValueError:
                continue
            if isinstance(args, dict) and isinstance(args.get("load"), str):
                scheduled_loads.add(args["load"])

    for call in calls:
        tool = registry.resolve(call.name)
        if tool is None and (repaired_name := registry.repaired_name(call.name)):
            tool = registry.resolve(repaired_name)
            repaired.append((call.name, repaired_name))
        canonical_name = tool.name if tool is not None else canonical_tool_call_name(
            call.name, registry
        )
        schema = tool.openai_spec()["function"]["parameters"] if tool is not None else None
        validation_error: str | None = None
        raw_args = call.arguments()
        try:
            parsed = repair_jsonish(raw_args)
        except ValueError as exc:
            parsed = arguments = {}
            validation_error = f'could not parse arguments: {exc}'
        else:
            # Before validation, not after: a container or number the model
            # serialized as a string is the right value in the wrong type, and
            # rejecting it here means the handler never runs (patch's `edits`
            # batch form and shell's timeout=15 were unusable for this reason).
            # A tool's own prepare_args runs first, mapping the shapes it
            # accepts onto its schema. An undeclared `_`-named key
            # (`_message`) is then dropped, not refused.
            prepared = tool.prepare_args(parsed) if tool is not None and tool.prepare_args else parsed
            arguments = tool_args.without_underscore_extras(
                tool_args.coerce_to_schema(prepared, schema), schema)
        # The sorted form is only the duplicate key. History keeps the model's
        # own text: a server's prompt cache holds the tokens the model wrote,
        # and a replay with its keys reordered re-renders a different prefix.
        identity = (canonical_name, json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ))
        if arguments == parsed and tool_args.is_json_object(raw_args):
            replay_args = raw_args
        else:
            replay_args = compact_json(arguments)
        if identity in seen:
            duplicate += 1
            continue
        seen.add(identity)

        if tool is None:
            validation_error = registry.unavailable_error(canonical_name)
            if scheduled_loads & {f"native:{canonical_name}", f"mcp:{canonical_name}"}:
                validation_error += ' A load in this same response cannot authorize a sibling call.'
        if validation_error is None:
            validation_error = tool_args.schema_error(arguments, schema)
        refused = False
        if validation_error is None and tool is not None:
            refusal = registry.argument_refusal(tool.name, arguments)
            if refusal is not None:
                validation_error, refused = refusal, True
        if validation_error is not None:
            invalid += 1
            if canonical_name == "write":
                validation_error += held.hold_failed_call(context, raw_args, arguments)

        if len(retained) >= limit:
            capped += 1
            continue
        retained.append(
            PendingToolCall(
                id=call.id,
                name=canonical_name,
                called_name=call.called_name or call.name,
                arg_chunks=[replay_args],
                validation_error=validation_error,
                unavailable=tool is None,
                refused=refused,
            )
        )

    return retained, _ToolBatchStats(
        received=len(calls),
        retained=len(retained),
        duplicate=duplicate,
        invalid=invalid,
        capped=capped,
        repaired=tuple(repaired),
    )


def assistant_message_with_tool_calls(
    message: ai.messages.Message,
    calls: list[PendingToolCall],
    *,
    diagnostic_suffix: str = "",
) -> ai.messages.Message:
    """Replace raw SDK tool-call parts with the normalized batch.

    Parts keep their order and the diagnostic text goes before the first call.
    A signed reasoning part that follows a call the batch dropped is left out:
    its signature covers the parts before it.
    """
    kept = {call.id: call for call in calls}
    parts: list[Any] = []
    placed: set[str] = set()
    dropped_call = False
    suffix = diagnostic_suffix

    def _place_suffix() -> None:
        nonlocal suffix
        if suffix:
            if parts and isinstance(parts[-1], ai.types.messages.TextPart):
                parts[-1] = parts[-1].model_copy(update={"text": parts[-1].text + suffix})
            else:
                parts.append(ai.types.messages.TextPart(text=suffix))
            suffix = ""

    for part in message.parts:
        if isinstance(part, ai.types.messages.ToolCallPart):
            _place_suffix()
            call = kept.get(part.tool_call_id)
            if call is None or call.id in placed:
                dropped_call = True
                continue
            placed.add(call.id)
            parts.append(part.model_copy(update={"tool_name": call.name, "tool_args": call.arguments()}))
        elif isinstance(part, ai.types.messages.ReasoningPart) and part.provider_metadata and dropped_call:
            continue
        else:
            parts.append(part)
    _place_suffix()
    for call in calls:
        if call.id not in placed:
            parts.append(ai.types.messages.ToolCallPart(
                tool_call_id=call.id,
                tool_name=call.name,
                tool_args=call.arguments(),
            ))
    if not parts:
        parts.append(ai.types.messages.TextPart(text=""))
    return message.model_copy(update={"parts": parts})


def incomplete_has_dangling_tool_args(pending_calls: list[PendingToolCall]) -> bool:
    return dangling_arguments(pc.arguments() for pc in pending_calls)


def truncated_tool_call_notice(reason: str, pending_calls: list[PendingToolCall]) -> str:
    names = sorted({pc.name or 'unknown' for pc in pending_calls})
    joined = ", ".join(names)
    suffix = f' for {joined}' if joined else ""
    return (
        snippets.render('incomplete-tool-call', REASON=reason, TOOLS=suffix)
    )


def repair_jsonish(raw: str) -> dict:
    return tool_args.repair_jsonish(raw)


def canonical_tool_args(raw: str) -> str:
    """Return tool-call args as a string the `ai` SDK will accept verbatim.

    The model occasionally emits args that are not strictly valid JSON (a
    trailing comma, an unclosed brace, a double-encoded string) — typically on
    large `write`/`patch` content payloads. `_dispatch` repairs these for
    execution via `repair_jsonish`, but the *history* we resend each turn
    carries the raw string. The SDK's integrity pass (`ai.types.integrity`)
    re-validates every prior tool call with `json.loads` and **blanks**
    unparseable args to ``"{}"`` (logging "invalid-tool-args"), so the model
    sees its own prior call as empty and flails.

    To keep history and execution consistent we return the raw bytes untouched
    only when they are already a JSON *object*. Otherwise — unparseable args, or
    args that are valid JSON of the wrong shape (a double-encoded string wrapping
    the real object) — we substitute the repaired, canonical object so history
    matches what executed. If even the repair fails, the raw string is returned
    unchanged (the SDK will blank it — nothing we can recover there).
    """
    return tool_args.canonical_tool_args(raw)


def sanitize_assistant_message(msg: ai.messages.Message) -> ai.messages.Message:
    """Repair raw tool-call args inside an SDK assistant message before it is
    resent as history, so the SDK's integrity pass does not blank them to
    ``{}``. Returns the message unchanged when nothing needs repair (frozen
    pydantic models require a copy to mutate)."""
    new_parts: list[Any] = []
    changed = False
    for part in msg.parts:
        if isinstance(part, ai.types.messages.ToolCallPart):
            fixed = canonical_tool_args(part.tool_args)
            if fixed != part.tool_args:
                part = part.model_copy(update={"tool_args": fixed})
                changed = True
        new_parts.append(part)
    if not changed:
        return msg
    return msg.model_copy(update={"parts": new_parts})


def rewritten_call(pc: PendingToolCall, arguments_json: str, registry: ToolRegistry) -> PendingToolCall:
    """``pc`` with the arguments an `on tool_call` handler rewrote it to. The
    rewrite is held to the same schema and argument bans as the model's own
    arguments; one that fails them refuses the call instead of running it.
    History keeps the model's arguments: only the dispatch sees these."""
    tool = registry.resolve(pc.name)
    if tool is None:
        return pc
    schema = tool.openai_spec()["function"]["parameters"]
    try:
        parsed = json.loads(arguments_json)
    except ValueError as exc:
        return replace(pc, validation_error=f"ERROR: rewritten arguments do not parse: {exc}", refused=True)
    prepared = tool.prepare_args(parsed) if tool.prepare_args else parsed
    arguments = tool_args.without_underscore_extras(tool_args.coerce_to_schema(prepared, schema), schema)
    problem = tool_args.schema_error(arguments, schema)
    if problem is not None:
        return replace(pc, validation_error=f"ERROR: an on tool_call handler rewrote {tool.name} to invalid arguments: {problem}", refused=True)
    banned = registry.argument_refusal(tool.name, arguments)
    if banned is not None:
        return replace(pc, validation_error=banned, refused=True)
    return replace(pc, arg_chunks=[compact_json(arguments)])
