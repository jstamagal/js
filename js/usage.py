"""Token and dollar totals per session.

Every model call a session makes is charged to it: the turn's own calls, the
compaction calls made for it, and the calls of the `task` workers it started
(`ToolContext.usage_chain` names the sessions above a worker). A call adds its
input, output, cache-read, cache-write and reasoning tokens, and its cost when
the models.dev catalog prices the model (`model_metadata.model_prices`). A
call on an unpriced model adds tokens only and counts as unpriced.

`input_tokens` is every prompt token of the call, cache reads and writes
included. The SDK's Anthropic usage leaves cache writes out of
`input_tokens` (its raw payload carries `cache_creation_input_tokens`); they
are added back here so every provider counts the same way.

Each charge appends a `usage` record to the session file: the call and the
session's totals after it. A `usage` record is not on the conversation path
(`session_store.on_path`) and replay skips it. A resumed session starts from
the totals of its last `usage` record.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import session_store

RECORD_KIND = "usage"
RECORD_VERSION = 1
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


@dataclass(frozen=True)
class CallUsage:
    """What one model call used and, when its model is priced, cost."""

    model: str
    provider: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Tally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    unpriced_calls: int = 0

    def add(self, call: CallUsage) -> None:
        self.calls += 1
        for name in _TOKEN_FIELDS:
            setattr(self, name, getattr(self, name) + getattr(call, name))
        if call.cost is None:
            self.unpriced_calls += 1
        else:
            self.cost += call.cost

    @property
    def priced(self) -> bool:
        """Whether at least one call was priced."""
        return self.calls > self.unpriced_calls

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(Tally)}

    @classmethod
    def from_dict(cls, raw: Any) -> Tally:
        tally = cls()
        if not isinstance(raw, dict):
            return tally
        for f in fields(Tally):
            value = raw.get(f.name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(tally, f.name, float(value) if f.name == "cost" else int(value))
        return tally


@dataclass
class UsageTotals(Tally):
    """A session's totals, and the same totals split by model."""

    by_model: dict[str, Tally] = field(default_factory=dict)

    def add(self, call: CallUsage) -> None:
        super().add(call)
        self.by_model.setdefault(call.model, Tally()).add(call)

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "by_model": {name: t.as_dict() for name, t in self.by_model.items()}}

    @classmethod
    def from_dict(cls, raw: Any) -> UsageTotals:
        base = Tally.from_dict(raw)
        totals = cls(**base.as_dict())
        by_model = raw.get("by_model") if isinstance(raw, dict) else None
        if isinstance(by_model, dict):
            totals.by_model = {str(name): Tally.from_dict(t) for name, t in by_model.items()}
        return totals


def _count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def call_cost(tokens: dict[str, int], tiers: Iterable[Any] | None) -> float | None:
    """Dollars for one call's `tokens` at the catalog `tiers` (dollars per
    million tokens, each from its `min_context` up), or None without tiers.
    Cache reads and writes are priced at their own rates, or the input rate
    when the catalog gives none."""
    tiers = list(tiers or ())
    if not tiers:
        return None
    prompt = tokens["input_tokens"]
    tier = tiers[0]
    for candidate in tiers:
        if (getattr(candidate, "min_context", 0) or 0) <= prompt:
            tier = candidate
    read, write = tokens["cache_read_tokens"], tokens["cache_write_tokens"]
    fresh = max(0, prompt - read - write)
    read_rate = tier.cache_read if tier.cache_read is not None else tier.input
    write_rate = tier.cache_write if tier.cache_write is not None else tier.input
    dollars = (fresh * tier.input + read * read_rate + write * write_rate
               + tokens["output_tokens"] * tier.output)
    return dollars / 1_000_000


def call_usage(usage: Any, *, model: str, provider_id: str | None) -> CallUsage:
    """The `CallUsage` of an SDK usage object (None when the provider sent
    none), priced from the catalog."""
    raw = getattr(usage, "raw", None) if usage is not None else None
    tokens = {
        "input_tokens": _count(getattr(usage, "input_tokens", 0)),
        "output_tokens": _count(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0)),
        "cache_read_tokens": _count(getattr(usage, "cache_read_tokens", 0)),
        "cache_write_tokens": _count(getattr(usage, "cache_write_tokens", 0)),
        "reasoning_tokens": _count(getattr(usage, "reasoning_tokens", 0)),
    }
    if isinstance(raw, dict) and "cache_creation_input_tokens" in raw:
        tokens["input_tokens"] += tokens["cache_write_tokens"]
    cost = None
    if usage is not None:
        from . import model_metadata

        try:
            tiers = model_metadata.model_prices(model, provider_id)
        except Exception:  # noqa: BLE001 - a catalog failure leaves the call unpriced
            tiers = None
        cost = call_cost(tokens, tiers)
    return CallUsage(model=model, provider=provider_id, cost=cost, **tokens)


# --- per-session totals ----------------------------------------------------------

_LOCK = threading.RLock()
_TOTALS: dict[str, UsageTotals] = {}


def _persisted(session_file: Path | None) -> bool:
    return session_file is not None and str(session_file) not in ("", os.devnull)


def _key(session_file: Path | None) -> str:
    if not _persisted(session_file):
        return os.devnull
    return str(Path(session_file).resolve(strict=False))


def load(session_file: Path) -> UsageTotals:
    """The totals of the last `usage` record in `session_file`, or zero."""
    try:
        with open(session_file, "rb") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return UsageTotals()
    for line in reversed(lines):
        if b'"kind":"usage"' not in line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict) and record.get("kind") == RECORD_KIND and record.get("version") == RECORD_VERSION:
            return UsageTotals.from_dict(record.get("totals"))
    return UsageTotals()


def totals(session_file: Path | None) -> UsageTotals:
    """The live totals of `session_file`, read from it the first time. A
    session that is not saved keeps its totals in memory only."""
    key = _key(session_file)
    with _LOCK:
        found = _TOTALS.get(key)
        if found is None:
            found = load(Path(session_file)) if _persisted(session_file) else UsageTotals()
            _TOTALS[key] = found
        return found


def forget(session_file: Path | None) -> None:
    """Drop the live totals of `session_file`; the next read loads the file."""
    with _LOCK:
        _TOTALS.pop(_key(session_file), None)


def charge(session_file: Path | None, call: CallUsage) -> UsageTotals:
    """Add `call` to the session's totals and append its `usage` record.
    The append happens under the lock, so the records of one file are in the
    order of their totals and the last one holds the largest."""
    with _LOCK:
        live = totals(session_file)
        live.add(call)
        if _persisted(session_file):
            try:
                session_store.append(Path(session_file), {
                    "kind": RECORD_KIND, "version": RECORD_VERSION, "ts": time.time(),
                    "call": call.as_dict(), "totals": live.as_dict(),
                })
            except OSError:
                pass  # accounting must never break the turn
    return live


# --- the meter: which sessions a model call is charged to ---------------------------

@dataclass(frozen=True)
class Meter:
    """The sessions a call is charged to, the first being the one running,
    and a callback that sees each call with that session's totals after it."""

    sessions: tuple[Path | None, ...]
    on_call: Callable[[CallUsage, UsageTotals], None] | None = None


_METER: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("js_usage_meter", default=None)


def start(meter: Meter) -> contextvars.Token:
    return _METER.set(meter)


def stop(token: contextvars.Token) -> None:
    _METER.reset(token)


def record(usage: Any, *, model: str, provider_id: str | None,
           session_file: Path | None = None) -> CallUsage | None:
    """Charge one model call to the running meter's sessions, or to
    `session_file` when no meter runs. Returns the call, or None when there
    was nowhere to charge it."""
    meter = _METER.get()
    sessions: tuple[Path | None, ...] = meter.sessions if meter is not None else (
        (session_file,) if session_file is not None else ())
    if not sessions:
        return None
    call = call_usage(usage, model=model, provider_id=provider_id)
    first: UsageTotals | None = None
    for index, session in enumerate(dict.fromkeys(sessions)):
        live = charge(session, call)
        if index == 0:
            first = live
    if meter is not None and meter.on_call is not None and first is not None:
        try:
            meter.on_call(call, first)
        except Exception:  # noqa: BLE001 - an observer never breaks the turn
            pass
    return call


# --- display -----------------------------------------------------------------------

def format_dollars(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def status_text(live: UsageTotals) -> str | None:
    """The status bar's session figure: the dollar total when any call was
    priced (with `+` when some were not), else the token total, else None
    before the first call."""
    if live.calls == 0:
        return None
    if live.priced:
        return format_dollars(live.cost) + ("+" if live.unpriced_calls else "")
    return f"{format_tokens(live.input_tokens + live.output_tokens)} tok"
