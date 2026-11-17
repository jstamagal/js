"""The REPL's session and conversation commands: /reset, /wipe, /turns,
/cost, /copy, /session, /name, /compact and /quit."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from . import (
    compaction,
    memory as M,
    messages as msgs,
    repl_state,
    resume_view,
    runtime,
    session_picker,
    session_store,
    settings,
    snippets,
    stream_transport,
    usage as usage_mod,
    usage_display,
)
from .config import Config
from .repl_state import Command, ReplState
from .session_catalog import append_title, session_title
from .toolkit import task_jobs


def _cmd_name(arg: str, state: ReplState | None, cfg: Config) -> str | None:
    if cfg.session_file == Path(os.devnull):
        msgs.say(msgs.SESSION_NOT_SAVED_TITLE)
        return None
    title = " ".join(arg.split())
    if title:
        append_title(cfg.session_file, title)
        msgs.say(msgs.SESSION_TITLED, title=title)
        return None
    current = session_title(cfg.session_file)
    if current:
        msgs.say(msgs.SESSION_TITLE, title=current)
    else:
        msgs.say(msgs.SESSION_UNTITLED)
    return None


def _cmd_session(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Open the session picker. A chosen session other than this one ends the
    REPL, and js starts again in that session's directory, agent and model."""
    try:
        choice = session_picker.pick_session(Path.cwd(), query=arg)
    except Exception as exc:  # noqa: BLE001
        return msgs.FAILED.said(error=f"{type(exc).__name__}: {exc}")
    if choice is None:
        return None
    if (choice.action == session_picker.RESUME and cfg.session_file != Path(os.devnull)
            and Path(choice.session.path) == cfg.session_file.resolve(strict=False)):
        msgs.say(msgs.SESSIONS_ALREADY_HERE)
        return None
    target = session_picker.session_target(choice)
    state.switch_session = target
    state.running = False
    msgs.say(msgs.SESSIONS_SWITCHING, name=session_store.display_name(target.path), dir=target.cwd or Path.cwd())
    return None


def _cmd_reset(arg: str, state: ReplState, cfg: Config) -> str | None:
    state.messages.clear()
    task_jobs.forget(runtime.T.STOCK_CONTEXT.task_owner)
    compaction.history_rewritten(runtime.T.STOCK_CONTEXT)
    M.append_mark(cfg.session_file, "session_reset")
    msgs.say(msgs.RESET_DONE)
    return None


def _cost_text(tally: usage_mod.Tally) -> str:
    if not tally.priced:
        return msgs.COST_UNPRICED.said()
    dollars = usage_display.format_dollars(tally.cost)
    if tally.unpriced_calls:
        return msgs.COST_PARTLY_PRICED.said(cost=dollars, calls=msgs.plural(tally.unpriced_calls, "call"))
    return dollars


def _cmd_cost(arg: str, state: ReplState | None, cfg: Config) -> str | None:
    live = usage_mod.totals(cfg.session_file)
    if live.calls == 0:
        msgs.say(msgs.COST_NONE)
        return None
    msgs.say(msgs.COST_TOTAL, calls=msgs.plural(live.calls, "call"), cost=_cost_text(live))
    msgs.say(msgs.COST_TOKENS, input=f"{live.input_tokens:,}", cache_read=f"{live.cache_read_tokens:,}",
             cache_write=f"{live.cache_write_tokens:,}", output=f"{live.output_tokens:,}",
             reasoning=f"{live.reasoning_tokens:,}")
    for model, tally in live.by_model.items():
        msgs.say(msgs.COST_MODEL, model=model, calls=msgs.plural(tally.calls, "call"),
                 tokens=f"{tally.input_tokens + tally.output_tokens:,}", cost=_cost_text(tally))
    return None


def _copy_command() -> list[str] | None:
    """COPY_COMMAND split into words, else wl-copy under Wayland, else xclip
    under X11; None when there is no clipboard."""
    if configured := settings.live_value("COPY_COMMAND"):
        return shlex.split(str(configured))
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-copy"]
    if os.environ.get("DISPLAY"):
        return ["xclip", "-selection", "clipboard"]
    return None


def _cmd_copy(arg: str, state: ReplState, cfg: Config) -> str | None:
    """The answer ``arg`` back (1, the last, by default) onto the clipboard,
    as the model wrote it: no wrapping, no marks, no padding."""
    arg = arg.strip() or "1"
    if not arg.isdigit() or int(arg) < 1:
        msgs.say(msgs.COPY_USAGE)
        return None
    n = int(arg)
    answers = [text for m in state.messages if m.get("role") == "assistant"
               if (text := resume_view.content_text(m.get("content"))).strip()]
    if len(answers) < n:
        msgs.say(msgs.COPY_NOTHING, n=n)
        return None
    command = _copy_command()
    if command is None:
        msgs.say(msgs.COPY_NO_CLIPBOARD)
        return None
    text = answers[-n].strip("\n") + "\n"
    try:
        # wl-copy stays behind to serve the clipboard: no pipes it could hold open.
        subprocess.run(command, input=text.encode(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=True)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        msgs.say(msgs.FAILED, error=e)
        return None
    msgs.say(msgs.COPY_DONE, n=n, chars=f"{len(text):,}")
    return None


def _cmd_wipe(arg: str, state: ReplState, cfg: Config) -> str | None:
    bak = M.wipe(cfg.session_file)
    usage_mod.forget(cfg.session_file)
    state.messages = M.load_replay_messages(cfg.session_file)
    task_jobs.forget(runtime.T.STOCK_CONTEXT.task_owner)
    compaction.history_rewritten(runtime.T.STOCK_CONTEXT)
    if bak:
        msgs.say(msgs.WIPE_ROTATED, path=bak.name)
    else:
        msgs.say(msgs.WIPE_NOTHING)
    return None


def _split_compact_model(arg: str) -> tuple[str | None, str, bool]:
    """Pull a leading ``-m <model>`` (or ``--model``) off a /compact argument.

    Returns ``(model, focus, ok)``; ``ok`` is False when the flag carries no
    value. The flag is leading-only because focus is free text.
    """
    tokens = arg.split(maxsplit=2)
    if tokens and tokens[0] in ("-m", "--model"):
        if len(tokens) < 2:
            return None, "", False
        return tokens[1], tokens[2].strip() if len(tokens) > 2 else "", True
    return None, arg.strip(), True


def _cmd_compact(arg: str, state: ReplState, cfg: Config) -> str | None:
    model, focus, ok = _split_compact_model(arg)
    if not ok:
        return msgs.USAGE.said(usage="/compact [-m model] [focus]")
    forced = focus == repl_state.repl_assembly(state).compact_force_focus
    if forced:
        focus = ""
    try:
        compact_cfg = state.config_for_turn(cfg)
        hooks = compaction.compact_hooks(state, compact_cfg)
        hooks["emit"], skipped = compaction.track_compact_skips(hooks["emit"])
        with stream_transport.net_role(msgs.COMPACTING_ROLE.words()):
            result = compaction.compact_now_sync(compact_cfg, state.system, state.messages, model=model,
                                                 focus=focus, forced=forced, **hooks)
    except snippets.SnippetError:
        raise
    except Exception as e:  # noqa: BLE001
        return msgs.COMPACTION_FAILED.said(error=f"{type(e).__name__}: {e}")
    if result not in skipped:
        msgs.say(msgs.COMPACTION_DONE, result=result)
    return None


def _append_closing_note(cfg: Config, state: ReplState, reminder: str) -> None:
    """Fold a closing note into the conversation without breaking role alternation.

    After a completed turn the note is simply the next user message. When the last
    message is already a user turn it is rewritten to carry the note instead, since
    two user messages in a row are rejected outright by some providers and silently
    mangled by chat templates that assume alternation."""
    messages = state.messages
    if not messages or messages[-1].get("role") != "user":
        message = {"role": "user", "content": reminder}
        messages.append(message)
        M.append_message(cfg.session_file, message)
        return

    last_user = messages[-1]
    content = last_user.get("content")
    if isinstance(content, str):
        last_user["content"] = f"{content}\n\n{reminder}"
    elif isinstance(content, list):
        last_user["content"] = [*content, {"type": "text", "text": reminder}]
    else:
        last_user["content"] = reminder
    M.replace_last_message(cfg.session_file, messages)


def _cmd_quit(arg: str, state: ReplState, cfg: Config) -> str | None:
    if arg:
        # Recorded as a user message so the next turn actually sees it. It is
        # appended to the previous user message rather than added as a second
        # one: providers with strict role alternation reject back-to-back user
        # turns, and chat templates that tolerate it silently degrade.
        reminder = f'<js-reminder>User closed the client with the message: {arg}</js-reminder>'
        _append_closing_note(cfg, state, reminder)
    state.running = False
    return None


COMMANDS: dict[str, Command] = {
    "reset": Command(_cmd_reset, "reset", msgs.CMD_RESET, turn_state=True),
    "wipe": Command(_cmd_wipe, "wipe", msgs.CMD_WIPE, turn_state=True),
    "turns": Command(lambda arg, state, cfg: msgs.say(msgs.TURNS_COUNT, messages=msgs.plural(len(state.messages), "message")),
                     "turns", msgs.CMD_TURNS),
    "cost": Command(_cmd_cost, "cost", msgs.CMD_COST, stands_alone=True),
    "copy": Command(_cmd_copy, "copy [N]", msgs.CMD_COPY),
    "session": Command(_cmd_session, "session [query]", msgs.CMD_SESSION, turn_state=True),
    "name": Command(_cmd_name, "name [title]", msgs.CMD_NAME, stands_alone=True),
    "compact": Command(_cmd_compact, "compact [-m model] [focus]", msgs.CMD_COMPACT,
                       turn_state=True),
    "quit": Command(_cmd_quit, "quit [note]", msgs.CMD_QUIT),
}
