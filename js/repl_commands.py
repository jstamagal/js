"""The REPL's slash commands: the command table, the dispatcher, aliases and
the startup run of the jsrc files."""

from __future__ import annotations

import contextlib
import copy
import io
import os
import re
import textwrap
from collections.abc import Mapping
from pathlib import Path

from . import (
    events,
    hookexec,
    messages as msgs,
    prompt_commands,
    repl_agent_commands,
    repl_job_commands,
    repl_model_commands,
    repl_session_commands,
    repl_settings_commands,
    repl_state,
    repl_workspace_commands,
    runtime,
    setcmd,
    setting_layers,
    settings,
    skills,
    supervisor,
    text_layout,
)
from .config import Config
from .repl_state import Command, ReplState


def _cmd_on(arg: str, state: ReplState, cfg: Config) -> str | None:
    if state.events is None:
        state.events = events.EventHooks()
    result = setcmd.on_command(state.events, arg)
    for out in result.lines:
        print(out)
    return result.error


def _cmd_exec(arg: str, state: ReplState | None, cfg: Config) -> str | None:
    """Run ``arg`` in the shell and queue its stdout for the next user message;
    with no ``state`` there is no next message and the stdout goes nowhere.
    Run by an `on` handler, the command reads the event as JSON on stdin with
    JS_EVENT naming it. Exit status 2 or a timeout under tool_call refuses the call."""
    command = arg.strip()
    if not command:
        return msgs.USAGE.said(usage="/exec <command>")
    call = events.current_call()
    live = state.settings if state is not None else cfg.settings
    timeout = int(settings.knob(live, "EXEC_TIMEOUT"))
    env = dict(os.environ)
    stdin_text = ""
    if call is not None:
        env["JS_EVENT"] = call.emission.event
        stdin_text = hookexec.event_json(call)
    try:
        outcome = hookexec.run(
            command,
            cwd=str(runtime.T.STOCK_CONTEXT.cwd),
            timeout=timeout,
            cap=int(settings.knob(live, "EXEC_OUTPUT_MAX") or 0),
            stdin_text=stdin_text,
            env=env,
        )
    except OSError as e:
        failure = msgs.EXEC_FAILED.said(command=command, error=e)
    else:
        if outcome.returncode is None:
            failure = msgs.EXEC_TIMED_OUT.said(command=command, seconds=timeout)
            hookexec.refuse_unfinished(call, str(failure))
        elif hookexec.apply_refusal(call, outcome):
            return None
        elif outcome.returncode != 0:
            detail = next((line.strip() for line in (outcome.stderr or outcome.stdout).splitlines()
                           if line.strip()), "")
            failure = msgs.EXEC_EXITED.said(command=command, status=outcome.returncode, detail=detail)
        else:
            note = hookexec.reminder(outcome.stdout)
            if note is not None and state is not None:
                repl_state.queue_note(state, note)
            if call is None:
                # Typed at the prompt: show what the model will read.
                if note is None:
                    msgs.say(msgs.EXEC_NO_OUTPUT)
                else:
                    print(outcome.stdout.rstrip("\n"))
                    msgs.say(msgs.EXEC_QUEUED, lines=msgs.plural(len(outcome.stdout.strip().splitlines()), "line"))
            return None
    if call is not None:
        # An `on` handler's errors otherwise reach only the debug log.
        msgs.say_said(failure)
    return failure


# --------------------------------------------------------------------------
# The command table. Every REPL command, rc line, /load line, `on` handler
# and ex command dispatches through COMMANDS; completion and /help read it.
# --------------------------------------------------------------------------

_MAX_ALIAS_DEPTH = 16


def _cmd_alias(arg: str, state: ReplState, cfg: Config) -> str | None:
    """`alias` lists, `alias name` shows, `alias name command` defines,
    `alias -name` removes. `$*` in the body is the alias's arguments; a body
    without `$*` gets them appended."""
    aliases = state.aliases
    parts = arg.split(maxsplit=1)
    if not parts:
        for name, body in aliases.items():
            msgs.say(msgs.ALIAS_ROW, name=name, body=body)
        if not aliases:
            msgs.say(msgs.NO_ALIASES)
        return None
    name = parts[0].lower().lstrip("/")
    if name.startswith("-") and len(name) > 1:
        if aliases.pop(name[1:], None) is None:
            return msgs.NO_ALIAS.said(name=name[1:])
        return None
    if len(parts) == 1:
        if name not in aliases:
            return msgs.NO_ALIAS.said(name=name)
        msgs.say(msgs.ALIAS_ROW, name=name, body=aliases[name])
        return None
    if name in commands(state):
        return msgs.ALIAS_IS_COMMAND.said(name=name)
    aliases[name] = parts[1].strip()
    msgs.say(msgs.ALIAS_ROW, name=name, body=aliases[name])
    return None


def _cmd_load(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Run each line of a file through the command table, in order. The first
    error stops the file and names the line."""
    stack = state.load_stack
    base = stack[-1].parent if stack else Path(getattr(cfg, "project_dir", None) or Path.cwd())
    path, error = setcmd.load_path(arg, base)
    if error:
        return error
    if len(stack) >= setcmd.MAX_LOAD_DEPTH:
        return msgs.LOAD_TOO_DEEP.said()
    if path in stack:
        return msgs.LOAD_CYCLE.said(path=path)
    if not path.is_file():
        return msgs.LOAD_NOT_FOUND.said(path=path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as e:
        return msgs.LOAD_UNREADABLE.said(path=path, error=f"{type(e).__name__}: {e}")
    stack.append(path)
    try:
        for lineno, raw in enumerate(lines, 1):
            error = _run_script_line(raw, state, cfg)
            if error:
                return msgs.SCRIPT_LINE_FAILED.said(path=path, lineno=lineno, error=error)
    finally:
        stack.pop()
    msgs.say(msgs.LOADED, path=path)
    return None


def _help_set(name: str) -> str | None:
    """`/help set NAME`: the setting's help text, from `js/help/set.txt`."""
    if not name:
        return msgs.USAGE.said(usage="/help set NAME")
    spec = settings.spec_for(name)
    if spec is None:
        return msgs.UNKNOWN_SETTING.said(key=name)
    msgs.say(msgs.HELP_SETTING, key=spec.key.upper())
    for line in textwrap.wrap(spec.doc, 72):
        msgs.say(msgs.HELP_SETTING_LINE, text=line)
    return None


def _help_fset(name: str) -> str | None:
    """`/help fset NAME`: the /fset entry's help text, from `js/help/fset.txt`."""
    if not name:
        return msgs.USAGE.said(usage="/help fset NAME")
    key = name.upper()
    doc = settings.read_help(settings.FSET_HELP_FILE).get(key)
    if doc is None:
        return msgs.FSET_UNKNOWN.said(name=name)
    msgs.say(msgs.HELP_SETTING, key=key)
    for line in textwrap.wrap(doc, 72):
        msgs.say(msgs.HELP_SETTING_LINE, text=line)
    return None


def _cmd_help(arg: str, state: ReplState, cfg: Config) -> str | None:
    topic, _, rest = arg.strip().partition(" ")
    if topic.lower() == "set":
        return _help_set(rest.strip())
    if topic.lower() == "fset":
        return _help_fset(rest.strip())
    rows = [(f"/{c.usage}", c.doc.said()) for c in dict.fromkeys(commands(state).values())]
    rows += [(f"/{name}", msgs.HELP_ALIAS.said(body=body)) for name, body in state.aliases.items()]
    rows += [(f"/{name}", msgs.HELP_PROMPT_COMMAND.said(path=command.path, description=command.description))
             for name, command in prompt_commands.discover(Path.cwd()).items()
             if name not in commands(state) and name not in state.aliases]
    rows += [("@path/to/file", msgs.HELP_ATTACH.said()), ("exit", msgs.HELP_EXIT.said())]
    columns = max(7, text_layout.terminal_width() - 1)
    width = min(max(text_layout.cells(usage) for usage, _doc in rows), (columns - 3) // 2)
    msgs.say(msgs.HELP_HEADING)
    for usage, doc in rows:
        usages = text_layout.wrap_words(usage, width).splitlines()
        docs = text_layout.wrap_words(msgs.words_of(doc), columns - width - 3).splitlines()
        for index in range(max(len(usages), len(docs))):
            label = usages[index] if index < len(usages) else ""
            text = docs[index] if index < len(docs) else ""
            msgs.say(msgs.HELP_ROW, usage=label + " " * (width - text_layout.cells(label)), doc=text)
    return None


_LOAD = Command(_cmd_load, "load <file>", msgs.CMD_LOAD, complete="path")


_ROWS: dict[str, Command] = {
    "help": Command(_cmd_help, "help [set NAME]", msgs.CMD_HELP),
    **{verb: _LOAD for verb in setcmd.LOAD_VERBS},
    "on": Command(_cmd_on, "on [[^]event command]", msgs.CMD_ON, complete="events"),
    "alias": Command(_cmd_alias, "alias [name [command]]", msgs.CMD_ALIAS),
    "exec": Command(_cmd_exec, "exec <command>", msgs.CMD_EXEC, complete="path", stands_alone=True),
    **repl_model_commands.COMMANDS,
    **repl_agent_commands.COMMANDS,
    **repl_settings_commands.COMMANDS,
    **repl_session_commands.COMMANDS,
    **repl_job_commands.COMMANDS,
    **repl_workspace_commands.COMMANDS,
}

# /help lists the commands in this order.
COMMANDS: dict[str, Command] = {verb: _ROWS[verb] for verb in (
    "help", "set", "fset", "show", "save", *setcmd.LOAD_VERBS, "on", "alias",
    "model", "pick-model", "provider", "baseurl", "apikey", "login", "logout", "models",
    "reset", "wipe", "persona", "tools", "skill", "skills",
    "turns", "cost", "copy", "session", "name",
    "jobs", "cancel", "window", "flush",
    "compact", "compact-auto", "refresh-model-catalog", "quit",
    "cd", "add", "drop", "exec",
)}


def standalone(line: str) -> Command | None:
    """The command ``line`` runs when it stands alone (`Command.stands_alone`);
    None for any other line."""
    parsed = setcmd.split_command(line)
    command = COMMANDS.get(parsed[0]) if parsed is not None else None
    return command if command is not None and command.stands_alone else None


def run_command(line: str, state: ReplState, cfg: Config, depth: int = 0) -> tuple[bool, str | None]:
    """Dispatch one command line (leading `/` optional) through COMMANDS and
    aliases. Returns ``(handled, error)``; ``handled`` is False for a verb that
    is neither."""
    parsed = setcmd.split_command(line)
    if parsed is None:
        return True, None
    verb, arg = parsed
    command = commands(state).get(verb)
    if command is None:
        body = state.aliases.get(verb)
        if body is None:
            return False, None
        if depth >= _MAX_ALIAS_DEPTH:
            return True, msgs.ALIAS_TOO_DEEP.said(name=verb)
        # The body's $NAMEs expand once; the typed arguments never do.
        body = setcmd.expand_line(body, state.settings)
        expanded = body.replace("$*", arg) if "$*" in body else f"{body} {arg}".strip()
        handled, error = run_command(expanded, state, cfg, depth + 1)
        return True, error if handled else msgs.ALIAS_UNKNOWN_COMMAND.said(name=verb, verb=expanded.split()[0])
    if repl_state.repl_assembly(state).guard_turn_state and command.turn_state and (sup := supervisor.get_current()) is not None and sup.turn_active():
        return True, repl_state.repl_hint(msgs.TURN_RUNNING, state).said(command=f"/{verb}")
    return True, command.run(arg, state, cfg)


def _run_script_line(line: str, state: ReplState, cfg: Config) -> str | None:
    """One line from a file of commands, its $NAMEs expanded once: an
    unknown verb is an error. In the startup run of the jsrc files a
    setting's short name is that setting, as config load read it: `model X`
    is `set MODEL X`, not the /model command."""
    line = setcmd.expand_line(line, state.settings)
    parsed = setcmd.split_command(line)
    if state.rc_replay and parsed is not None and parsed[0] in settings.SPEC_BY_ALIAS:
        line = f"set {parsed[0]} {parsed[1]}"
    handled, error = run_command(line, state, cfg)
    if not handled:
        return msgs.UNKNOWN_COMMAND.said(verb=setcmd.split_command(line)[0])
    return error


def commands(state: ReplState | None) -> Mapping[str, Command]:
    supplied = repl_state.repl_assembly(state).commands
    return COMMANDS if supplied is None else supplied


def handle_command(line: str, state: ReplState, cfg: Config) -> bool:
    """Return True if `line` was a command (already handled), False otherwise."""
    if line in repl_state.repl_assembly(state).quit_words:
        state.running = False
        return True
    if not line.startswith("/") or is_skill_invocation(line, state):
        return False
    with repl_state.LIVE_STATE_LOCK:
        handled, error = run_command(line, state, cfg)
    if not handled and repl_state.repl_assembly(state).refuse_unknown and is_unknown_command(line):
        handled, error = True, msgs.UNKNOWN_SLASH_COMMAND.said()
    if isinstance(error, msgs.Said):
        msgs.say_said(error)
    elif error:
        msgs.say(msgs.COMMAND_REFUSED, error=error)
    return handled


# A slash word shaped like a command name. Anything else after the `/`
# (`/usr/bin/python is broken`, `/README.md`, `//text`) is prose for the model.
_COMMAND_WORD = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*")


def is_unknown_command(line: str) -> bool:
    """`line` names a command that is not in the table, not an alias and not
    a markdown command."""
    word = line.split(maxsplit=1)[0]
    return (_COMMAND_WORD.fullmatch(word) is not None
            and prompt_commands.expand(line, prompt_commands.discover(Path.cwd())) is None)


def is_skill_invocation(line: str, state: ReplState | None = None) -> bool:
    """An invocation with a skill name is a turn carrying that skill."""
    assembly = repl_state.repl_assembly(state)
    match = skills.match_user_invocation(line, command=assembly.skill_command, pattern=assembly.skill_pattern)
    return match is not None and match.group(1) is not None


def run_rc_commands(state: ReplState, cfg: Config, paths: list[Path]) -> list[str]:
    """Run each jsrc file a line at a time, in order, through the same line
    runner `/load` uses (`_run_script_line`), its settings and its commands
    together: a `load` in it is `/load`, so a loaded file stops at its first
    error and so does every loaded file containing it; the jsrc itself goes
    on past an error. A relative `load` resolves against the jsrc's
    directory. The run starts from the stock settings and no /fset entries,
    as config load did; each setting it leaves different from what config
    load found takes its value in the live settings, under the env and CLI
    layers, and what the session put on top of the jsrc layer stays. Output
    is dropped; the errors come back for the banner."""
    from . import fset as _fset

    errors: list[str] = []
    stack = state.load_stack
    live = state.settings
    found = setting_layers.collect_again(paths)
    layer = settings.seed_defaults()
    _fset.reset()
    state.settings = layer
    state.rc_replay = True
    try:
        for path in paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            stack.append(path.resolve(strict=False))
            try:
                for lineno, raw in enumerate(lines, 1):
                    with contextlib.redirect_stdout(io.StringIO()):
                        error = _run_script_line(raw, state, cfg)
                    if error:
                        errors.append(msgs.SCRIPT_LINE_FAILED.said(path=path, lineno=lineno, error=error))
            finally:
                stack.pop()
    finally:
        state.settings = live
        state.rc_replay = False
    if isinstance(live, dict):
        ran = setting_layers.over_jsrc_layer(paths, layer)
        changed: list[str] = []
        for spec in settings.REGISTRY:
            value = settings.get_dotted(ran, spec.path)
            if settings.get_dotted(found, spec.path) != value:
                settings.set_dotted(live, spec.path, copy.deepcopy(value))
                changed.append(spec.key)
        # The model, provider and the rest of the state follow only what the
        # run changed: the flags and the resumed session stay over the jsrc.
        repl_settings_commands.apply_settings_result(setcmd.CommandResult(handled=True, changed=bool(changed),
                                                                          changed_keys=changed), state, cfg)
        msgs.set_banner(str(settings.knob(live, "BANNER")))
    return errors
