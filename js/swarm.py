"""The swarm bus: one inbox per agent, a message is a file, the room log is the blackboard.

  js -p --json --swarm ROOT/NAME "opener"          # this process is agent NAME on the bus at ROOT
  python -m js.swarm send ROOT FROM TO "text"       # from the runner or the operator; TO is a name or *
  python -m js.swarm send ROOT steer kivu --kind stop   # end an agent after its current turn
  python -m js.swarm members ROOT                    # who is on the bus, asleep or working, what each holds
  python -m js.swarm quiet ROOT                      # exit 0 when every agent is asleep with an empty inbox
  python -m js.swarm cells ROOT                      # the work board
  python -m js.swarm post ROOT steer "title" "body"  # the operator puts work on the board
  python -m js.swarm run SPEC.json                   # every agent in the spec, one process (js.swarm_run)

ROOT/log.jsonl         every message ever sent, one JSON object per line, seq-numbered
ROOT/claims.json       what is claimed, by whom, until when
ROOT/cells.json        the work board: tasks posted, taken and finished
ROOT/<name>/inbox/     one file per message not yet delivered to <name>
ROOT/<name>/subs       the kinds <name> subscribed to, one per line
ROOT/<name>/asleep     present while <name> waits on its inbox
ROOT/<name>/spawned    present when another agent's `recruit` started <name>

An agent on the bus never spends a model call waiting:

- while a turn runs, its inbox is drained at every tool boundary and the messages
  ride in as a user message (``runtime.run_turn_async``'s ``steer``);
- when the model stops calling tools, the process sleeps on the inbox and wakes
  into a new turn with whatever landed. The sleep is a harness-side poll every
  ``POLL_S`` seconds; the model is not called. A burst that lands within
  ``COALESCE_S`` of the first message is one wake.

A message of kind ``stop`` ends the agent after the turn it lands in. ``retire``
is the agent ending itself: it posts a handoff and leaves the bus. A message
sent to a name that has not joined yet waits in that name's inbox.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

POLL_S = 0.25
COALESCE_S = 0.3
CLAIM_TTL_S = 600.0
SPAWN_CAP = 8
STOP = "stop"
TICK = "tick"
RETIRE = "retire"
TASK = "task"
DONE = "done"
CLOCK = "clock"
BROADCAST = "*"
_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def check_name(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise ValueError(f"swarm name must be one word of [A-Za-z0-9_.-]: {name!r}")
    return name


def check_kind(kind: str) -> str:
    kind = (kind or "").strip() or "say"
    if not _NAME.fullmatch(kind):
        raise ValueError(f"a message kind is one word of [A-Za-z0-9_.-]: {kind!r}")
    return kind


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


@dataclass(frozen=True)
class Cell:
    """One piece of work on the board. `taken` is a live claim on `task:<id>`;
    when that claim expires the cell is open again."""

    id: int
    title: str
    body: str
    by: str
    ts: float
    status: str = "open"      # open | taken | done
    holder: str = ""
    result: str = ""

    @property
    def key(self) -> str:
        return f"task:{self.id}"

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "body": self.body, "by": self.by, "ts": self.ts,
                "status": self.status, "holder": self.holder, "result": self.result}

    @classmethod
    def from_dict(cls, d: dict) -> Cell:
        return cls(int(d["id"]), str(d.get("title") or ""), str(d.get("body") or ""), str(d.get("by") or ""),
                   float(d.get("ts") or 0), str(d.get("status") or "open"), str(d.get("holder") or ""),
                   str(d.get("result") or ""))


class Room:
    """A bus root. Any process may send; an agent joins to get an inbox."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.log = self.root / "log.jsonl"

    def join(self, name: str) -> Inbox:
        inbox = Inbox(self, check_name(name))
        inbox.path.mkdir(parents=True, exist_ok=True)
        return inbox

    def leave(self, name: str) -> None:
        """Drop <name> from the bus: its inbox, subscriptions and sleep marker. Its claims expire."""
        home = self.root / name
        shutil.rmtree(home / "inbox", ignore_errors=True)
        for marker in ("subs", "asleep"):
            (home / marker).unlink(missing_ok=True)

    def members(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "inbox").is_dir())

    def asleep(self, name: str) -> bool:
        return (self.root / name / "asleep").is_file()

    def quiet(self) -> bool:
        """True when every member is asleep with nothing waiting: the swarm has nothing to do."""
        members = self.members()
        return bool(members) and all(self.asleep(m) and not Inbox(self, m).pending() for m in members)

    @contextlib.contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # messages

    def send(self, sender: str, to: str, body: str, kind: str = "say") -> Msg:
        check_name(sender)
        if to != BROADCAST:
            check_name(to)
        kind = check_kind(kind)
        with self._locked():
            msg = Msg(self._next_seq(), time.time(), sender, to, kind, body)
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(msg.dumps() + "\n")
            members = [m for m in self.members() if m != sender]
            named = members if to == BROADCAST else [to]
            subscribed = [m for m in members if kind in self.subs_of(m)]
            for name in dict.fromkeys(named + subscribed):
                self._drop(name, msg)
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

    # subscriptions: a message of a subscribed kind reaches the subscriber whoever it was sent to

    def subs_of(self, name: str) -> set[str]:
        try:
            return set((self.root / name / "subs").read_text(encoding="utf-8").split())
        except OSError:
            return set()

    def subscribe(self, name: str, kind: str, on: bool = True) -> set[str]:
        check_name(name)
        kind = check_kind(kind)
        kinds = self.subs_of(name)
        (kinds.add if on else kinds.discard)(kind)
        path = self.root / name / "subs"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(k + "\n" for k in sorted(kinds)), encoding="utf-8")
        return kinds

    # claims: one holder per key, for a while

    def claim(self, key: str, who: str, ttl: float = CLAIM_TTL_S) -> tuple[bool, str, float]:
        """Take `key` for `who` for `ttl` seconds, or renew it. Returns (yours, holder, expires)."""
        check_name(who)
        with self._locked():
            claims = self._claims()
            held = claims.get(key)
            if held is not None and held["holder"] != who:
                return False, held["holder"], held["expires"]
            claims[key] = {"holder": who, "expires": time.time() + max(1.0, float(ttl))}
            self._write_claims(claims)
            return True, who, claims[key]["expires"]

    def release(self, key: str, who: str) -> bool:
        with self._locked():
            claims = self._claims()
            if claims.get(key, {}).get("holder") != who:
                return False
            del claims[key]
            self._write_claims(claims)
            return True

    def claims(self) -> dict[str, dict]:
        """Live claims: key -> {holder, expires}."""
        with self._locked():
            return self._claims()

    def _claims(self) -> dict[str, dict]:
        try:
            raw = json.loads((self.root / "claims.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        now = time.time()
        return {k: v for k, v in raw.items() if isinstance(v, dict) and float(v.get("expires", 0)) > now}

    def _write_claims(self, claims: dict[str, dict]) -> None:
        tmp = self.root / ".claims.tmp"
        tmp.write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.root / "claims.json")

    # the work board: cells posted, taken, finished

    def post_cell(self, by: str, title: str, body: str = "") -> Cell:
        """Put work on the board and tell everyone with a `task` message."""
        check_name(by)
        with self._locked():
            cells = self._cells()
            cell = Cell(max((c.id for c in cells), default=0) + 1, title.strip(), body, by, time.time())
            self._write_cells([*cells, cell])
        self.send(by, BROADCAST, f"task #{cell.id}: {cell.title}" + (f"\n{body}" if body.strip() else ""), TASK)
        return cell

    def take_cell(self, cell_id: int, who: str, ttl: float = CLAIM_TTL_S) -> tuple[bool, str, float]:
        """Take cell `cell_id` for `who`: a claim on its key, renewed by taking again."""
        cell = self.cell(cell_id)
        if cell is None:
            raise KeyError(cell_id)
        if cell.status == "done":
            return False, cell.holder, 0.0
        yours, holder, expires = self.claim(cell.key, who, ttl)
        if yours:
            with self._locked():
                self._write_cells([replace(c, status="taken", holder=who) if c.id == cell_id else c
                                   for c in self._cells()])
        return yours, holder, expires

    def finish_cell(self, cell_id: int, who: str, result: str = "") -> Cell:
        """Mark the cell done with `result`, free its claim, tell everyone with a `done` message."""
        check_name(who)
        with self._locked():
            cells = self._cells()
            done = next((c for c in cells if c.id == cell_id), None)
            if done is None:
                raise KeyError(cell_id)
            done = replace(done, status="done", holder=who, result=result)
            self._write_cells([done if c.id == cell_id else c for c in cells])
            claims = self._claims()
            if done.key in claims:
                del claims[done.key]
                self._write_claims(claims)
        self.send(who, BROADCAST, f"done #{done.id}: {done.title}" + (f"\n{result}" if result.strip() else ""), DONE)
        return done

    def cell(self, cell_id: int) -> Cell | None:
        return next((c for c in self.cells() if c.id == cell_id), None)

    def cells(self) -> list[Cell]:
        """The board as it stands: open and taken cells first, done ones last.
        A taken cell whose claim expired is open again."""
        with self._locked():
            claims = self._claims()
            cells = self._cells()
        live = []
        for c in cells:
            if c.status == "done":
                live.append(c)
            elif c.key in claims:
                live.append(replace(c, status="taken", holder=claims[c.key]["holder"]))
            else:
                live.append(replace(c, status="open", holder=""))
        return sorted(live, key=lambda c: (c.status == "done", c.id))

    def _cells(self) -> list[Cell]:
        try:
            raw = json.loads((self.root / "cells.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        return [Cell.from_dict(d) for d in raw if isinstance(d, dict)]

    def _write_cells(self, cells: list[Cell]) -> None:
        tmp = self.root / ".cells.tmp"
        tmp.write_text(json.dumps([c.as_dict() for c in cells], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.root / "cells.json")


class Inbox:
    def __init__(self, room: Room, name: str) -> None:
        self.room, self.name = room, name
        self.path = room.root / name / "inbox"

    def pending(self) -> bool:
        try:
            return any(n.endswith(".json") and not n.startswith(".") for n in os.listdir(self.path))
        except FileNotFoundError:
            return False

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
        seq = f"#{m.seq}" if m.seq else ""
        stamp = time.strftime("%H:%M:%S", time.localtime(m.ts))
        lines.append(f"[{seq}{kind} {m.sender} -> {to} {stamp}]".replace("[ ", "["))
        lines.append(m.body.rstrip())
        lines.append("")
    return "\n".join(lines).rstrip()


def roster(room: Room, me: str | None = None) -> str:
    """One line per member: name, you, asleep or working, what it holds."""
    held: dict[str, list[str]] = {}
    for key, claim in room.claims().items():
        held.setdefault(claim["holder"], []).append(key)
    lines = []
    for name in room.members():
        bits = [name]
        if name == me:
            bits.append("(you)")
        bits.append("asleep" if room.asleep(name) else "working")
        if held.get(name):
            bits.append("holds " + ", ".join(sorted(held[name])))
        lines.append(" ".join(bits))
    return "\n".join(lines) or "(nobody yet)"


def board(room: Room) -> str:
    """The work board as the model and the operator read it."""
    lines = []
    for c in room.cells():
        state = {"open": "open", "taken": f"taken by {c.holder}", "done": f"done by {c.holder}"}[c.status]
        lines.append(f"#{c.id} [{state}] {c.title}" + (f" (from {c.by})" if c.by else ""))
        first = (c.result if c.status == "done" else c.body).strip().splitlines()
        if first:
            lines.append(f"    {first[0][:160]}")
    return "\n".join(lines) or "(no tasks on the board)"


def sibling_argv(argv: list[str], swarm: str, name: str) -> list[str]:
    """The command of a sibling of this process: `argv` run again as `python -m js`
    with the --swarm path, the session name and the prompt replaced. The sibling
    reads its opener from stdin."""
    rest = list(argv[1:])
    out: list[str] = [sys.executable, "-m", "js"]
    has_prompt = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else None
        has_value = nxt is not None and (nxt == "-" or not nxt.startswith("-"))
        if tok == "--swarm" and has_value:
            out += [tok, swarm]
            i += 2
        elif tok.startswith("--swarm="):
            out.append(f"--swarm={swarm}")
            i += 1
        elif tok in ("-s", "--session") and has_value:
            out += [tok, f"{nxt}-{name}"]
            i += 2
        elif tok.startswith("--session="):
            out.append(f"{tok}-{name}")
            i += 1
        elif tok in ("-p", "--prompt"):
            has_prompt = True
            out += [tok, "-"]
            i += 2 if has_value else 1
        elif tok == "--last":
            i += 1
        else:
            out.append(tok)
            i += 1
    if "--swarm" not in out and not any(t.startswith("--swarm=") for t in out):
        out += ["--swarm", swarm]
    if not has_prompt:
        out += ["-p", "-"]
    return out


class Agent:
    """This process as one agent on a bus: ``--swarm ROOT/NAME``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        p = Path(path).expanduser()
        self.name = check_name(p.name)
        self.room = Room(p.parent)
        self.inbox = self.room.join(self.name)
        self.stop_seen = False
        self.retired = False
        self.alarm: float | None = None
        # Set by the in-process runner: recruit adds a coroutine there, not a process.
        self.recruiter: Any = None

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
        """Block until something lands, or the alarm set by `wake_me` goes off.
        No model call is made while waiting. A burst is handed over as one wake."""
        marker = self._asleep()
        try:
            timeout = None if self.alarm is None else max(0.0, self.alarm - time.monotonic())
            landed = self.inbox.wait(timeout)
            if not landed:
                return [self._tick()]
            time.sleep(COALESCE_S)
            return self._take(landed + self.inbox.drain())
        finally:
            marker.unlink(missing_ok=True)

    async def sleep_async(self) -> list[Msg]:
        """`sleep` for an agent on an event loop: the same wait as an await."""
        import asyncio

        marker = self._asleep()
        try:
            while True:
                landed = self.inbox.drain()
                if landed:
                    await asyncio.sleep(COALESCE_S)
                    return self._take(landed + self.inbox.drain())
                if self.alarm is not None and time.monotonic() >= self.alarm:
                    return [self._tick()]
                await asyncio.sleep(POLL_S)
        finally:
            marker.unlink(missing_ok=True)

    def _asleep(self) -> Path:
        marker = self.room.root / self.name / "asleep"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return marker

    def _tick(self) -> Msg:
        self.alarm = None
        return Msg(0, time.time(), CLOCK, self.name, TICK, "The time you asked to be woken after has passed.")

    def wake_message(self, msgs: list[Msg]) -> dict:
        return {"role": "user", "content": render(msgs, self.name)}

    def send(self, to: str, body: str, kind: str = "say") -> Msg:
        return self.room.send(self.name, to, body, kind)

    def wake_me(self, seconds: float) -> None:
        """Wake after `seconds` even if nothing lands. Zero or less clears the alarm."""
        self.alarm = None if seconds <= 0 else time.monotonic() + seconds

    def retire(self, handoff: str) -> Msg:
        """Post the handoff to everyone, leave the bus, end after this turn."""
        msg = self.send(BROADCAST, handoff, RETIRE)
        self.room.leave(self.name)
        self.stop_seen = True
        self.retired = True
        return msg

    def spawn(self, name: str, opener: str, argv: list[str] | None = None) -> subprocess.Popen:
        """Start a sibling agent NAME on this bus: this process's own command with
        a new name and session, the opener as its first prompt. Its events land in
        ROOT/NAME/events.jsonl. At most SPAWN_CAP spawned agents per bus."""
        check_name(name)
        root = self.room.root
        home = root / name
        if name in self.room.members() or (home / "spawned").is_file():
            raise ValueError(f"{name} is already on the bus")
        spawned = sum(1 for p in root.iterdir() if (p / "spawned").is_file()) if root.is_dir() else 0
        if spawned >= SPAWN_CAP:
            raise ValueError(f"this bus already has {spawned} spawned agents; the cap is {SPAWN_CAP}")
        home.mkdir(parents=True, exist_ok=True)
        cmd = sibling_argv(sys.argv if argv is None else argv, str(home), name)
        with open(home / "events.jsonl", "ab") as out, open(home / "stderr", "ab") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
        proc.stdin.write(opener.encode("utf-8"))
        proc.stdin.close()
        (home / "spawned").write_text(json.dumps({"by": self.name, "pid": proc.pid, "ts": time.time(), "cmd": cmd}),
                                      encoding="utf-8")
        return proc


# ---------------------------------------------------------------- the tools --swarm puts on the surface

_OFF_BUS = "ERROR: this agent is not on a swarm bus (run js with --swarm ROOT/NAME)"


def _agent(context: Any) -> Agent | None:
    return getattr(context, "swarm", None)


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _send(to: str = "", text: str = "", kind: str = "say", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    to = str(to or "").strip()
    text = str(text or "")
    if not to:
        return "ERROR: to is required: an agent name, or * for everyone else"
    if not text.strip():
        return "ERROR: text is empty"
    try:
        msg = agent.send(to, text, str(kind or "say"))
    except ValueError as exc:
        return f"ERROR: {exc}"
    return f"sent #{msg.seq} to {'everyone' if to == BROADCAST else to}"


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
        return "ERROR: key is empty: name the file, area or task you are taking"
    try:
        seconds = CLAIM_TTL_S if ttl in (None, "") else float(ttl)
    except (TypeError, ValueError):
        return f"ERROR: ttl must be a number of seconds, not {ttl!r}"
    yours, holder, expires = agent.room.claim(key, agent.name, seconds)
    if yours:
        return f"{key} is yours until {_clock(expires)}; claim it again to keep it longer"
    return f"{key} is held by {holder} until {_clock(expires)}"


def _release(key: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    key = str(key or "").strip()
    return f"released {key}" if agent.room.release(key, agent.name) else f"you do not hold {key}"


def _subscribe(kind: str = "", off: Any = False, context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    try:
        kinds = agent.room.subscribe(agent.name, str(kind or ""), on=not _truthy(off))
    except ValueError as exc:
        return f"ERROR: {exc}"
    return "subscribed to: " + (", ".join(sorted(kinds)) if kinds else "nothing")


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _wake_me(seconds: Any = None, context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return f"ERROR: seconds must be a number, not {seconds!r}"
    agent.wake_me(s)
    if s <= 0:
        return "alarm cleared"
    return f"you will be woken in {s:g} s if nothing lands first; stop calling tools to sleep"


def _retire(handoff: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    handoff = str(handoff or "")
    if not handoff.strip():
        return "ERROR: handoff is empty: say what you did, what is left, and where things are"
    msg = agent.retire(handoff)
    return f"retired; your handoff is #{msg.seq}. This turn is your last: finish and stop calling tools"


def _recruit(name: str = "", opener: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    name, opener = str(name or "").strip(), str(opener or "")
    if not opener.strip():
        return "ERROR: opener is empty: the new agent needs to be told who it is and what to do"
    try:
        if agent.recruiter is not None:
            agent.recruiter(name, opener)
        else:
            agent.spawn(name, opener)
    except (ValueError, OSError) as exc:
        return f"ERROR: {exc}"
    return f"started {name}; it is on the bus as {name} and reads your opener first"


def _post_task(title: str = "", body: str = "", context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    title = str(title or "").strip()
    if not title:
        return "ERROR: title is empty: say in one line what needs doing"
    cell = agent.room.post_cell(agent.name, title, str(body or ""))
    return f"posted task #{cell.id}: {cell.title}; everyone was told"


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
        return f"ERROR: id must be a task number, not {id!r}"
    try:
        yours, holder, expires = agent.room.take_cell(cell_id, agent.name)
    except KeyError:
        return f"no task #{cell_id} on the board"
    cell = agent.room.cell(cell_id)
    if yours:
        return f"task #{cell_id} is yours until {_clock(expires)}: {cell.title}\n{cell.body}".rstrip()
    if cell.status == "done":
        return f"task #{cell_id} is already done by {holder}"
    return f"task #{cell_id} is held by {holder} until {_clock(expires)}"


def _finish_task(id: Any = None, result: str = "", context: Any = None) -> str:  # noqa: A002
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    cell_id = _cell_id(id)
    if cell_id is None:
        return f"ERROR: id must be a task number, not {id!r}"
    try:
        cell = agent.room.finish_cell(cell_id, agent.name, str(result or ""))
    except KeyError:
        return f"no task #{cell_id} on the board"
    return f"task #{cell.id} done; everyone was told"


def _tasks(context: Any = None) -> str:
    agent = _agent(context)
    if agent is None:
        return _OFF_BUS
    return board(agent.room)


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
        Tool("claim", load_description("claim"), _claim, {
            "key": {"type": "string", "description": "What you are taking: a file, a module, a task line."},
            "ttl": {"type": "number", "description": f"Seconds the claim lasts. Default {CLAIM_TTL_S:g}."},
        }, required=("key",), source="swarm"),
        Tool("release", load_description("release"), _release, {
            "key": {"type": "string", "description": "The key you claimed."},
        }, required=("key",), source="swarm"),
        Tool("subscribe", load_description("subscribe"), _subscribe, {
            "kind": {"type": "string", "description": "A message kind, as passed to send."},
            "off": {"type": "boolean", "default": False, "description": "True to unsubscribe."},
        }, required=("kind",), source="swarm"),
        Tool("wake_me", load_description("wake_me"), _wake_me, {
            "seconds": {"type": "number", "description": "How long to wait. 0 clears a pending alarm."},
        }, required=("seconds",), source="swarm"),
        Tool("retire", load_description("retire"), _retire, {
            "handoff": {"type": "string", "description": "What you did, what is left, where things are."},
        }, required=("handoff",), source="swarm"),
        Tool("recruit", load_description("recruit"), _recruit, {
            "name": {"type": "string", "description": "The new agent's name: one word, not yet on the bus."},
            "opener": {"type": "string", "description": "Its first prompt: who it is, the goal, what to do first."},
        }, required=("name", "opener"), source="swarm"),
        Tool("post_task", load_description("post_task"), _post_task, {
            "title": {"type": "string", "description": "One line: what needs doing."},
            "body": {"type": "string", "default": "", "description": "What the taker needs to know: where, how, done when."},
        }, required=("title",), source="swarm"),
        Tool("take_task", load_description("take_task"), _take_task, {
            "id": {"type": "integer", "description": "The task number from the board."},
        }, required=("id",), source="swarm"),
        Tool("finish_task", load_description("finish_task"), _finish_task, {
            "id": {"type": "integer", "description": "The task number from the board."},
            "result": {"type": "string", "default": "", "description": "What came of it: where the work is, what was found."},
        }, required=("id",), source="swarm"),
        Tool("tasks", load_description("tasks"), _tasks, {}, read_only=True, source="swarm"),
    )


def with_bus_tools(registry):
    """`registry` plus the bus tools: what --swarm puts on every agent's surface."""
    extra = tuple(t for t in tools() if t.name not in registry.by_name)
    aliases = {**registry.aliases, **{t.name.lower(): t.name for t in extra}}
    return replace(registry, tools=registry.tools + extra, aliases=aliases)


# ---------------------------------------------------------------- python -m js.swarm

def main(argv: list[str] | None = None) -> int:
    import argparse

    from . import messages as msgs

    ap = argparse.ArgumentParser(prog="python -m js.swarm", description=msgs.SWARM_CLI.text())
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help=msgs.SWARM_SEND.text())
    s.add_argument("root")
    s.add_argument("sender")
    s.add_argument("to", help=msgs.SWARM_SEND_TO.text())
    s.add_argument("text", nargs="?")
    s.add_argument("--kind", default="say", help=msgs.SWARM_SEND_KIND.text())
    m = sub.add_parser("members", help=msgs.SWARM_MEMBERS.text())
    m.add_argument("root")
    q = sub.add_parser("quiet", help=msgs.SWARM_QUIET.text())
    q.add_argument("root")
    c = sub.add_parser("cells", help=msgs.SWARM_CELLS.text())
    c.add_argument("root")
    p = sub.add_parser("post", help=msgs.SWARM_POST.text())
    p.add_argument("root")
    p.add_argument("by")
    p.add_argument("title")
    p.add_argument("body", nargs="?", default="")
    r = sub.add_parser("run", help=msgs.SWARM_RUN.text())
    r.add_argument("spec")
    a = ap.parse_args(argv)
    if a.cmd == "send":
        body = a.text if a.text not in (None, "-") else sys.stdin.read()
        msg = Room(a.root).send(a.sender, a.to, body, a.kind)
        print(f"sent #{msg.seq}")
    elif a.cmd == "members":
        print(roster(Room(a.root)))
    elif a.cmd == "quiet":
        return 0 if Room(a.root).quiet() else 1
    elif a.cmd == "cells":
        print(board(Room(a.root)))
    elif a.cmd == "post":
        body = a.body if a.body != "-" else sys.stdin.read()
        cell = Room(a.root).post_cell(a.by, a.title, body)
        print(f"posted #{cell.id}")
    else:
        from . import swarm_run

        return swarm_run.main(a.spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
