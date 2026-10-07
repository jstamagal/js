"""The swarm bus: one inbox per agent, a message is a file, the room log is the blackboard.

  js -p --json --swarm ROOT/NAME "opener"          # this process is agent NAME on the bus at ROOT
  python -m js.swarm send ROOT FROM TO "text"       # from the runner or the operator; TO is a name or *
  python -m js.swarm send ROOT steer kivu --kind stop   # end an agent after its current turn
  python -m js.swarm members ROOT

ROOT/log.jsonl         every message ever sent, one JSON object per line, seq-numbered
ROOT/<name>/inbox/     one file per message not yet delivered to <name>

An agent on the bus never spends a model call waiting:

- while a turn runs, its inbox is drained at every tool boundary and the messages
  ride in as a user message (``runtime.run_turn_async``'s ``steer``);
- when the model stops calling tools, the process sleeps on the inbox and wakes
  into a new turn with whatever landed. The sleep is a harness-side poll every
  ``POLL_S`` seconds; the model is not called.

A message of kind ``stop`` ends the agent after the turn it lands in. A message
sent to a name that has not joined yet waits in that name's inbox.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

POLL_S = 0.25
STOP = "stop"
BROADCAST = "*"
_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def check_name(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise ValueError(f"swarm name must be one word of [A-Za-z0-9_.-]: {name!r}")
    return name


@dataclass(frozen=True)
class Msg:
    seq: int
    ts: float
    sender: str
    to: str
    kind: str
    body: str

    def dumps(self) -> str:
        return json.dumps({"seq": self.seq, "ts": self.ts, "from": self.sender, "to": self.to,
                           "kind": self.kind, "body": self.body}, ensure_ascii=False)

    @classmethod
    def loads(cls, text: str) -> Msg:
        d = json.loads(text)
        return cls(int(d["seq"]), float(d["ts"]), str(d["from"]), str(d["to"]),
                   str(d.get("kind") or "say"), str(d.get("body") or ""))


class Room:
    """A bus root. Any process may send; an agent joins to get an inbox."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.log = self.root / "log.jsonl"

    def join(self, name: str) -> Inbox:
        inbox = Inbox(self, check_name(name))
        inbox.path.mkdir(parents=True, exist_ok=True)
        return inbox

    def members(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "inbox").is_dir())

    def send(self, sender: str, to: str, body: str, kind: str = "say") -> Msg:
        check_name(sender)
        if to != BROADCAST:
            check_name(to)
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                msg = Msg(self._next_seq(), time.time(), sender, to, kind or "say", body)
                with open(self.log, "a", encoding="utf-8") as f:
                    f.write(msg.dumps() + "\n")
                targets = [m for m in self.members() if m != sender] if to == BROADCAST else [to]
                for name in targets:
                    self._drop(name, msg)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return msg

    def _next_seq(self) -> int:
        """Under the lock. The counter file is the fast path; the log is the truth."""
        counter = self.root / ".seq"
        try:
            seq = int(counter.read_text()) + 1
        except (OSError, ValueError):
            seq = 1
            if self.log.is_file():
                with open(self.log, encoding="utf-8", errors="replace") as f:
                    seq = sum(1 for _ in f) + 1
        counter.write_text(str(seq))
        return seq

    def _drop(self, name: str, msg: Msg) -> None:
        inbox = self.root / name / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        tmp = inbox / f".{msg.seq:08d}.tmp"
        tmp.write_text(msg.dumps(), encoding="utf-8")
        os.replace(tmp, inbox / f"{msg.seq:08d}.json")


class Inbox:
    def __init__(self, room: Room, name: str) -> None:
        self.room, self.name = room, name
        self.path = room.root / name / "inbox"

    def drain(self) -> list[Msg]:
        """Every waiting message in seq order, each handed over once."""
        try:
            names = sorted(n for n in os.listdir(self.path) if n.endswith(".json") and not n.startswith("."))
        except FileNotFoundError:
            return []
        out = []
        for n in names:
            p = self.path / n
            try:
                out.append(Msg.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            p.unlink(missing_ok=True)
        return out

    def wait(self, timeout: float | None = None) -> list[Msg]:
        """Block until something lands (or `timeout` seconds pass: then [])."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            msgs = self.drain()
            if msgs:
                return msgs
            if deadline is not None and time.monotonic() >= deadline:
                return []
            time.sleep(POLL_S)


def render(msgs: list[Msg], me: str) -> str:
    """The messages as the model reads them."""
    lines = ["--- messages ---"]
    for m in msgs:
        to = "you" if m.to == me else m.to
        kind = "" if m.kind == "say" else f" {m.kind}"
        stamp = time.strftime("%H:%M:%S", time.localtime(m.ts))
        lines.append(f"[#{m.seq}{kind} {m.sender} -> {to} {stamp}]")
        lines.append(m.body.rstrip())
        lines.append("")
    return "\n".join(lines).rstrip()


class Agent:
    """This process as one agent on a bus: ``--swarm ROOT/NAME``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        p = Path(path).expanduser()
        self.name = check_name(p.name)
        self.room = Room(p.parent)
        self.inbox = self.room.join(self.name)
        self.stop_seen = False

    def _take(self, msgs: list[Msg]) -> list[Msg]:
        if any(m.kind == STOP for m in msgs):
            self.stop_seen = True
        return msgs

    def steer(self) -> dict | None:
        """``run_turn_async``'s steer: the inbox as a user message at a tool boundary."""
        msgs = self._take(self.inbox.drain())
        if not msgs:
            return None
        return {"role": "user", "content": render(msgs, self.name), "steered": True}

    def sleep(self) -> list[Msg]:
        """Block until something lands. No model call is made while waiting."""
        return self._take(self.inbox.wait())

    def wake_message(self, msgs: list[Msg]) -> dict:
        return {"role": "user", "content": render(msgs, self.name)}

    def send(self, to: str, body: str, kind: str = "say") -> Msg:
        return self.room.send(self.name, to, body, kind)


# ---------------------------------------------------------------- the tools --swarm puts on the surface

def _send(to: str = "", text: str = "", kind: str = "say", context: Any = None) -> str:
    agent = getattr(context, "swarm", None)
    if agent is None:
        return "ERROR: this agent is not on a swarm bus (run js with --swarm ROOT/NAME)"
    to = str(to or "").strip()
    text = str(text or "")
    if not to:
        return "ERROR: to is required: an agent name, or * for everyone else"
    if not text.strip():
        return "ERROR: text is empty"
    try:
        msg = agent.send(to, text, str(kind or "say").strip() or "say")
    except ValueError as exc:
        return f"ERROR: {exc}"
    return f"sent #{msg.seq} to {'everyone' if to == BROADCAST else to}"


def _who(context: Any = None) -> str:
    agent = getattr(context, "swarm", None)
    if agent is None:
        return "ERROR: this agent is not on a swarm bus (run js with --swarm ROOT/NAME)"
    return "\n".join(f"{n} (you)" if n == agent.name else n for n in agent.room.members()) or "(nobody yet)"


def tools() -> tuple:
    from .toolkit.core import Tool
    from .toolkit.descriptions import load_description

    return (
        Tool("send", load_description("send"), _send, {
            "to": {"type": "string", "description": "An agent's name, or * for everyone else on the bus."},
            "text": {"type": "string", "description": "The message."},
            "kind": {"type": "string", "default": "say",
                     "description": "say (default), ask, reply, done, or a short word the troop agrees on."},
        }, required=("to", "text"), source="swarm"),
        Tool("who", load_description("who"), _who, {}, read_only=True, source="swarm"),
    )


def with_bus_tools(registry):
    """`registry` plus the bus tools: what --swarm puts on every agent's surface."""
    extra = tuple(t for t in tools() if t.name not in registry.by_name)
    aliases = {**registry.aliases, **{t.name.lower(): t.name for t in extra}}
    return replace(registry, tools=registry.tools + extra, aliases=aliases)


# ---------------------------------------------------------------- python -m js.swarm

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m js.swarm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="post a message; text '-' or absent reads stdin")
    s.add_argument("root")
    s.add_argument("sender")
    s.add_argument("to", help="an agent name or *")
    s.add_argument("text", nargs="?")
    s.add_argument("--kind", default="say")
    m = sub.add_parser("members", help="who has an inbox")
    m.add_argument("root")
    a = ap.parse_args(argv)
    if a.cmd == "send":
        body = a.text if a.text not in (None, "-") else sys.stdin.read()
        msg = Room(a.root).send(a.sender, a.to, body, a.kind)
        print(f"sent #{msg.seq}")
    else:
        print("\n".join(Room(a.root).members()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
