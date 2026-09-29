"""Session tags: a few subjects per session, judged by TypeSafe's Jev.

The tag list is the operator's, in `~/.js/tags.yaml` (`tags.file`), one line
per tag, its name and a short description:

    js: the js harness itself, its code, config, tools and sessions
    nfs / mounts: NFS exports, mounts, automount, stale handles

A session is tagged in one TypeSafe request. The state is the session's last
`tags.messages` operator and model messages: what the operator typed, what the
model answered, and the text the model wrote alongside its tool calls. Tool
output is not sent. Each tag is one Noul question. Tags scoring at least
`tags.threshold` are kept, highest first, at most `tags.max`.

The result is a `tags` record appended to the session file: the kept tags,
every tag's score, the digest of the tag list they were judged against
(`list`) and the session's message count at the time (`through`).
`js.session_text` shows the newest one in the `.txt` header and the catalog.

`start_sweep` runs when a js entry point releases its sessions and when the
picker opens. With a TYPESAFE_API_KEY and a tag list file it starts a
detached `python -m js.session_tags`, which tags every shown session (not
quick, empty, subagent or script-started; `js.session_query.kind`) that is not
open in another process and whose newest record is missing, judged against a
different list, or older than its last message. So a session is tagged when
it ends, and editing the list retags every session. One sweep runs at a time;
a failed request ends the sweep with one line in `~/.js/logs/tags.log`.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import messages as msgs
from . import paths
from . import settings as settings_mod

API_KEY_ENV = "TYPESAFE_API_KEY"
RECORD_KIND = "tags"
RECORD_VERSION = 1

OPERATOR = "operator"
MODEL = "model"

# The question each tag is asked. `tag` is the question's own data; the
# conversation is the request's state.
QUESTION = "Is `tag` one of the main subjects of `conversation`?"
CRITERIA = {
    "true": "The conversation is substantially about `tag`: it is one of the things the "
            "operator and the model spend the conversation on.",
    "false": "`tag` is not a subject of the conversation, or comes up only in passing.",
}


class TagListError(ValueError):
    """The tag list file cannot be read as one `name: description` per tag."""


@dataclass(frozen=True)
class Tag:
    name: str
    description: str


@dataclass(frozen=True)
class Options:
    """The `tags.*` settings a sweep runs with."""

    file: str
    threshold: float
    top: int
    messages: int
    message_chars: int
    model: str

    @classmethod
    def from_settings(cls, settings: dict | None) -> Options:
        def knob(key: str) -> Any:
            return settings_mod.knob(settings, key)

        file = knob("tags.file")
        return cls(
            file=str(Path(file).expanduser() if file else paths.tags_file()),
            threshold=float(knob("tags.threshold")),
            top=int(knob("tags.max")),
            messages=int(knob("tags.messages")),
            message_chars=int(knob("tags.message_chars")),
            model=str(knob("tags.model")),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Options:
        return cls(**json.loads(text))


# --- the tag list ----------------------------------------------------------------


def load_tags(path: Path) -> list[Tag]:
    """The tags in the list file at `path`, in file order; none when there is
    no file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TagListError(msgs.TAGS_FILE_UNREADABLE.text(path=path, error=exc.strerror or exc)) from exc
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise TagListError(msgs.TAGS_FILE_UNREADABLE.text(path=path, error=exc)) from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise TagListError(msgs.TAGS_FILE_SHAPE.text(path=path))
    tags = []
    for name, description in data.items():
        if name is None or isinstance(name, (dict, list)) or isinstance(description, (dict, list)):
            raise TagListError(msgs.TAGS_FILE_SHAPE.text(path=path))
        name = str(name).strip()
        if name:
            tags.append(Tag(name, "" if description is None else str(description).strip()))
    return tags


def list_digest(tags: list[Tag]) -> str:
    """What a `tags` record names the list it was judged against by: the
    digest of every tag's name and description, in order."""
    payload = json.dumps([[tag.name, tag.description] for tag in tags], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --- one session ------------------------------------------------------------------


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def conversation(rows: list[Any], *, count: int, chars: int) -> list[dict[str, str]]:
    """The last `count` operator and model messages of a session's rows
    (`js.session_text.Row`), each cut to `chars` characters. A tool row
    contributes the text the model wrote alongside the call, once per
    assistant message; tool output is never included."""
    messages: list[dict[str, str]] = []
    label = None
    for row in rows:
        if row.role == "tool":
            if not row.label or (row.label == label and not row.lines):
                continue
            label = row.label
            who, lines = MODEL, [row.label, *row.lines]
        elif row.role in ("user", "assistant"):
            label = None
            who, lines = (OPERATOR if row.role == "user" else MODEL), row.lines
        else:
            continue
        text = "\n".join(lines).strip()
        if text:
            messages.append({"from": who, "text": _cut(text, chars)})
    return messages[-count:] if count > 0 else []


def questions(tags: list[Tag]) -> dict[str, dict[str, Any]]:
    """One Noul question per tag, keyed `t0`, `t1`, … in list order."""
    return {
        f"t{index}": {
            "type": "noul",
            "instructions": {"tag": {"name": tag.name, "description": tag.description},
                             "question": QUESTION},
            "criteria": CRITERIA,
        }
        for index, tag in enumerate(tags)
    }


def judge(state: dict[str, Any], asked: dict[str, dict[str, Any]], *, model: str) -> dict[str, float]:
    """Each question's Noul answer, from one TypeSafe System One request."""
    from typesafe_sdk import TypeSafeClient

    with TypeSafeClient(model=model) as client:
        response = client.system_one(state, asked)
    return {key: float(answer.noul) for key, answer in response.nouls.items()}


def choose(scores: dict[str, float], *, threshold: float, top: int) -> list[str]:
    """The tags to keep: scoring at least `threshold`, highest first, at most `top`."""
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, score in ranked if score >= threshold][:max(0, top)]


def tag_session(session_file: Path, tags: list[Tag], options: Options) -> list[str]:
    """Judge `session_file` against `tags`, append the `tags` record and bring
    its `.txt` and catalog entry up to date. Returns the kept tags."""
    from . import session_store, session_text

    rows = session_text.rows(session_file)
    state = {"conversation": conversation(rows, count=options.messages, chars=options.message_chars)}
    scores: dict[str, float] = {}
    if state["conversation"] and tags:
        asked = questions(tags)
        answers = judge(state, asked, model=options.model)
        scores = {tag.name: round(answers[key], 4) for key, tag in zip(asked, tags, strict=True)
                  if key in answers}
    kept = choose(scores, threshold=options.threshold, top=options.top)
    session_store.append(session_file, {
        "kind": RECORD_KIND, "version": RECORD_VERSION, "ts": time.time(),
        "tags": kept, "scores": scores, "list": list_digest(tags), "through": len(rows),
    })
    session_text.refresh(session_file)
    return kept


# --- the sweep --------------------------------------------------------------------


def stale(summary: dict[str, Any], digest: str) -> bool:
    """Whether a catalogued session is shown and its newest `tags` record is
    missing, judged against another list, or older than its last message."""
    from . import session_query

    session = session_query.Session.from_summary(summary)
    if session_query.kind(session) != session_query.SHOWN:
        return False
    return summary.get("tags_list") != digest or summary.get("tags_through") != session.messages


def _log(session: str, error: BaseException) -> None:
    try:
        log = paths.logs_root() / "tags.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(msgs.TAGS_LOG_FAILED.text(
                when=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session=session,
                error=f"{type(error).__name__}: {error}") + "\n")
    except OSError:
        pass


def sweep(options: Options) -> int:
    """Tag every stale session, newest first, until none is left. Returns how
    many were tagged. Holds `~/.js/cache/tags.lock` throughout, so sweeps run
    one after another; the list is read again before each pass."""
    from . import session_catalog, session_index

    if not os.environ.get(API_KEY_ENV, "").strip():
        return 0
    lock_path = paths.cache_root() / "tags.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tagged = 0
    tried: set[str] = set()
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        while True:
            try:
                tags = load_tags(Path(options.file))
            except TagListError as exc:
                _log(options.file, exc)
                return tagged
            if not tags:
                return tagged
            digest = list_digest(tags)
            waiting = sorted((summary for summary in session_index.catalog()
                              if summary["path"] not in tried and stale(summary, digest)),
                             key=lambda summary: -(summary.get("last") or summary.get("mtime") or 0))
            if not waiting:
                return tagged
            for summary in waiting:
                path = Path(summary["path"])
                tried.add(summary["path"])
                if session_catalog.session_in_flight(path):
                    continue
                try:
                    tag_session(path, tags, options)
                except Exception as exc:  # noqa: BLE001 - detached; the log is where it goes
                    _log(str(path), exc)
                    return tagged
                tagged += 1


def start_sweep(settings: dict | None) -> None:
    """Start a detached sweep when there is a TypeSafe key and a tag list
    file; otherwise do nothing."""
    if not os.environ.get(API_KEY_ENV, "").strip():
        return
    options = Options.from_settings(settings)
    if not Path(options.file).is_file():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "js.session_tags", options.to_json()],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, cwd=str(paths.user_home()),
        )
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    options = Options.from_json(argv[0]) if argv else Options.from_settings(None)
    sweep(options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
