"""The swarm bus as tools a model calls: send, who, claim and release, subscribe, wake_me, retire, recruit, and the work board's tasks."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from ..swarm import BROADCAST, CLAIM_TTL_S, Agent, board, bus_view, clock, expiry, roster
from . import policy
from .core import Tool
from .descriptions import load_description, load_parameter_description
from .presentation import Presentation

_OFF_BUS = 'ERROR: this agent is not on a swarm bus (run it with python -m js.swarm run SPEC.json)'


def _agent(context: Any) -> Agent | None:
    return getattr(context, "swarm", None)


def _send(to: str = "", text: str = "", kind: str = "say", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    to = str(to or "").strip()
    text = str(text or "")
    if not to:
        return 'ERROR: to is required: an agent name, or * for everyone else'
    if not text.strip():
        return 'ERROR: text is empty'
    try:
        msg = agent.send(to, text, str(kind or "say"))
    except ValueError as exc:
        return f'ERROR: {exc}'
    bus_view(message={"seq": msg.seq, "ts": msg.ts, "from": msg.sender,
                   "to": msg.to, "kind": msg.kind, "body": msg.body})
    return 'sent #{msg_seq} to {everyone_if_to_BROADCAST_else_to}'.format(msg_seq=msg.seq, everyone_if_to_BROADCAST_else_to='everyone' if to == BROADCAST else to)


def _who(context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    return roster(agent.room, agent.name)


def _claim(key: str = "", ttl: Any = None, context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    key = str(key or "").strip()
    if not key:
        return 'ERROR: key is empty: name the file, area or task you are taking'
    try:
        seconds = CLAIM_TTL_S if ttl in (None, "") else float(ttl)
        yours, holder, expires = agent.room.claim(key, agent.name, seconds)
    except (TypeError, ValueError, OverflowError):
        return f'ERROR: ttl must be finite seconds with a representable expiry, not {ttl!r}'
    bus_view(key=key, claimed=yours, holder=holder, expires=expires)
    if yours:
        return f'{key} is yours until {clock(expires)}; claim it again to keep it longer'
    return f'{key} is held by {holder} until {clock(expires)}'


def _release(key: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    key = str(key or "").strip()
    released = agent.room.release(key, agent.name)
    bus_view(key=key, released=released)
    return f'released {key}' if released else f'you do not hold {key}'


def _subscribe(kind: str = "", off: Any = False, context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    try:
        kinds = agent.room.subscribe(agent.name, str(kind or ""), on=not _truthy(off))
    except ValueError as exc:
        return f'ERROR: {exc}'
    bus_view(subscriptions=sorted(kinds))
    return 'subscribed to: ' + (', '.join(sorted(kinds)) if kinds else 'nothing')


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _wake_me(seconds: Any = None, context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    try:
        s = float(seconds)
        expiry(s, time.monotonic())
    except (TypeError, ValueError, OverflowError):
        return f'ERROR: seconds must be finite with a representable deadline, not {seconds!r}'
    left = None if agent.alarm is None else agent.alarm - time.monotonic()
    if s > 0 and left is not None and left > 0:
        return (f'an alarm is already set, {left:.0f} s from now, and it stays set when a message wakes you; nothing changed. Stop calling tools to sleep. Messages wake you by themselves.')
    agent.wake_me(s)
    bus_view(seconds=s, alarm=agent.alarm)
    if s <= 0:
        return 'alarm cleared'
    return f'you will be woken in {s:g} s if nothing lands first; stop calling tools to sleep'


def _retire(handoff: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    handoff = str(handoff or "")
    if not handoff.strip():
        return 'ERROR: handoff is empty: say what you did, what is left, and where things are'
    msg = agent.retire(handoff)
    bus_view(retired=True, handoff=handoff, seq=msg.seq)
    return f'retired; your handoff is #{msg.seq}. This turn is your last: finish and stop calling tools'


def _recruit(name: str = "", opener: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    name, opener = str(name or "").strip(), str(opener or "")
    if not opener.strip():
        return 'ERROR: opener is empty: the new agent needs to be told who it is and what to do'
    try:
        agent.recruiter(name, opener)
    except (ValueError, OSError) as exc:
        return f'ERROR: {exc}'
    bus_view(recruited=name, opener=opener)
    return f'started {name}; it is on the bus as {name} and reads your opener first'


def _post_task(title: str = "", body: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    title = str(title or "").strip()
    if not title:
        return 'ERROR: title is empty: say in one line what needs doing'
    cell = agent.room.post_cell(agent.name, title, str(body or ""))
    bus_view(task=cell.as_dict())
    return f'posted task #{cell.id}: {cell.title}; everyone was told'


def _cell_id(raw: Any) -> int | None:
    try:
        return int(str(raw).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def _take_task(id: Any = None, context: Any = None) -> str:  # noqa: A002 - the tool's parameter is named id
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    cell_id = _cell_id(id)
    if cell_id is None:
        return f'ERROR: id must be a task number, not {id!r}'
    try:
        yours, holder, expires = agent.room.take_cell(cell_id, agent.name)
    except KeyError:
        return f'no task #{cell_id} on the board'
    cell = agent.room.cell(cell_id)
    bus_view(task=cell.as_dict(), taken=yours, holder=holder, expires=expires)
    if yours:
        return f'task #{cell_id} is yours until {clock(expires)}: {cell.title}\n{cell.body}'.rstrip()
    if cell.status == "done":
        return f'task #{cell_id} is already done by {holder}'
    return f'task #{cell_id} is held by {holder} until {clock(expires)}'


def _finish_task(id: Any = None, result: str = "", context: Any = None) -> str:  # noqa: A002
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    cell_id = _cell_id(id)
    if cell_id is None:
        return f'ERROR: id must be a task number, not {id!r}'
    try:
        cell = agent.room.finish_cell(cell_id, agent.name, str(result or ""))
    except KeyError:
        return f'no task #{cell_id} on the board'
    except PermissionError as exc:
        return str(exc)
    bus_view(task=cell.as_dict())
    return f'task #{cell.id} done; everyone was told'


def _tasks(context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    return board(agent.room)


def tools() -> tuple:
    return (
        Tool("send", load_description("send"), _send, {
            "to": {"type": "string", "description": load_parameter_description('send', 'to')},
            "text": {"type": "string", "description": load_parameter_description('send', 'text')},
            "kind": {"type": "string", "default": "say",
                     "description": load_parameter_description('send', 'kind')},
        }, required=("to", "text"), source="swarm", presentation=Presentation(key=("to",), said="text")),
        Tool("who", load_description("who"), _who, {}, read_only=True, source="swarm"),
        Tool("claim", load_description("claim"), _claim, {
            "key": {"type": "string", "description": load_parameter_description('claim', 'key')},
            "ttl": {"type": "number", "description": load_parameter_description('claim', 'ttl', SECONDS=f"{CLAIM_TTL_S:g}")},
        }, required=("key",), source="swarm", presentation=Presentation(key=("key",))),
        Tool("release", load_description("release"), _release, {
            "key": {"type": "string", "description": load_parameter_description('release', 'key')},
        }, required=("key",), source="swarm", presentation=Presentation(key=("key",))),
        Tool("subscribe", load_description("subscribe"), _subscribe, {
            "kind": {"type": "string", "description": load_parameter_description('subscribe', 'kind')},
            "off": {"type": "boolean", "default": False, "description": load_parameter_description('subscribe', 'off')},
        }, required=("kind",), source="swarm"),
        Tool("wake_me", load_description("wake_me"), _wake_me, {
            "seconds": {"type": "number", "description": load_parameter_description('wake_me', 'seconds')},
        }, required=("seconds",), source="swarm"),
        Tool("retire", load_description("retire"), _retire, {
            "handoff": {"type": "string", "description": load_parameter_description('retire', 'handoff')},
        }, required=("handoff",), source="swarm"),
        Tool("recruit", load_description("recruit"), _recruit, {
            "name": {"type": "string", "description": load_parameter_description('recruit', 'name')},
            "opener": {"type": "string", "description": load_parameter_description('recruit', 'opener')},
        }, required=("name", "opener"), source="swarm"),
        Tool("post_task", load_description("post_task"), _post_task, {
            "title": {"type": "string", "description": load_parameter_description('post_task', 'title')},
            "body": {"type": "string", "default": "", "description": load_parameter_description('post_task', 'body')},
        }, required=("title",), source="swarm", presentation=Presentation(key=("title",), said="body")),
        Tool("take_task", load_description("take_task"), _take_task, {
            "id": {"type": "integer", "description": load_parameter_description('take_task', 'id')},
        }, required=("id",), source="swarm"),
        Tool("finish_task", load_description("finish_task"), _finish_task, {
            "id": {"type": "integer", "description": load_parameter_description('finish_task', 'id')},
            "result": {"type": "string", "default": "", "description": load_parameter_description('finish_task', 'result')},
        }, required=("id",), source="swarm", presentation=Presentation(said="result")),
        Tool("tasks", load_description("tasks"), _tasks, {}, read_only=True, source="swarm"),
    )


def with_bus_tools(registry):
    """`registry` plus the bus tools: what the runner puts on every agent's surface."""
    extra = tuple(t for t in tools() if t.name not in registry.by_name)
    aliases = {**registry.aliases, **{t.name.lower(): t.name for t in extra}}
    for canonical, public in registry.public_aliases.items():
        owner = aliases.get(public.lower())
        if owner is not None and owner != canonical:
            raise policy.ToolPolicyError(f"tool alias {public!r} for {canonical!r} conflicts with {owner!r}")
    return replace(registry, tools=registry.tools + extra, aliases=aliases)
