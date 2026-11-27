"""The `js` command line: parse it, settle the run's setup, and run the mode
it names: a listing, --bench, --compact, --commit, a -p run
(`js.prompt_run`), or the REPL (`js.repl`)."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from . import (
    agent_lock,
    command_line,
    compaction,
    display as display_mod,
    dotenv,
    endpoint_uri,
    headless,
    home as _home,
    jail as _jail,
    listings,
    loop_controller,
    memory as M,
    messages as msgs,
    model_metadata,
    network_display,
    paths as _paths,
    persona as P,
    prompt_run,
    run_assembly,
    runtime,
    sampling as sampling_mod,
    session_catalog,
    session_store,
    setting_values,
    settings,
    snippets,
)
from .cli_options import RunOptions, current, invocation
from .config import (
    InvocationSources,
    _preset_config_paths,
    derive_session_name,
    resolve_agent_id,
    resolve_session_file,
    run_settings,
    set_invocation,
    validate_agent_id,
)
from .home_cli import configure_home
from .tool_binaries import resolve_binary


def _latest_session_name(agent_id: str) -> str | None:
    """The most recently started session file for *agent_id*, or None when there is none.

    A recorded session that no longer exists is treated as absent rather
    than resurrected, so --last never creates an empty session."""
    latest = session_store.read_latest(_paths.state_root() / agent_id)
    return str(latest) if latest is not None else None


def _exchanges_are_the_prompt(args: argparse.Namespace, agent: str | None,
                              sources: InvocationSources) -> bool:
    """Whether a run with no -p runs the agent's NN-user.md / NN-agent.md
    exchanges as a one-shot: the agent has them and the session is fresh,
    so they are what the run sends."""
    try:
        cfg = run_assembly.from_env(None, save_session=False, sources=sources, agent_id=agent)
        spec = P.load_configured_prompt_spec(cfg, expand=False)
    except (FileNotFoundError, ValueError):
        # The REPL path reports the error.
        return False
    if not spec.exchanges:
        return False
    name = args.session if args.session is not None else os.environ.get("JS_SESSION")
    if name is None:
        return True
    try:
        session_file = resolve_session_file(cfg.sessions_dir, name, start=cfg.project_dir)
    except ValueError:
        # No such session yet: the run starts it.
        return True
    return not M.load_replay_messages(session_file)


def _invocation_agent(args: argparse.Namespace, sources: InvocationSources) -> str:
    """The agent this invocation runs: --agent, --commit, then the `DEFAULT_AGENT`
    setting, resolved as `from_env` resolves it."""
    return resolve_agent_id(
        args.agent or ("commit" if args.commit else None), sources=sources)


def _invocation_sources(args: argparse.Namespace) -> InvocationSources:
    """What the parsed command line adds to settings: its --extra values, its
    --preset names split at commas, and its --no-*-config switches."""
    return InvocationSources(
        extras=tuple(args.extras),
        presets=tuple(name for spec in args.presets for name in spec.split(",") if name.strip()),
        ignore_local_config=args.no_local_config,
        ignore_global_config=args.no_global_config,
    )


def _read_stdin_if_piped() -> str:
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""

def _read_stdin_attachment_if_piped() -> bytes:
    if sys.stdin.isatty():
        return b""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read()
    return sys.stdin.read().encode("utf-8")


# The command-line flags that set a setting: (args attribute, setting). Each
# reaches every run path as an --extra, which also records it as a flag value
# that a saved login's defaults never replace (js.login_defaults).
SETTING_FLAGS: tuple[tuple[str, str], ...] = (
    ("reasoning", "EFFORT"),
    ("max_out", "OUTPUT_TOKENS_MAX"),
    ("max_turns", "TURN_ROUNDS_MAX"),
    ("template_kwargs", "TEMPLATE_KWARGS"),
    *((field, field.upper()) for field in sampling_mod.SAMPLING_FIELDS),
)


def _setting_flag_extras(args: argparse.Namespace) -> tuple[list[str], int | None]:
    """The --extra entries for the `SETTING_FLAGS` ``args`` sets, and an exit
    code when one of them is not a value its setting takes."""
    extras: list[str] = []
    for attr, key in SETTING_FLAGS:
        value = getattr(args, attr, None)
        if value is None:
            continue
        if key == "EFFORT":
            _seed, effort_error = setting_values.validate_cli_reasoning(str(value))
            if effort_error is not None:
                msgs.warn(msgs.BAD_REASONING, value=value, error=effort_error)
                return [], 2
        else:
            _value, error = setting_values.coerce_value(settings.SPEC_BY_KEY[key], str(value))
            if error is not None:
                msgs.warn(msgs.BAD_FLAG_VALUE, flag="--" + attr.replace("_", "-"), value=value, error=error)
                return [], 2
        extras.append(f"{key}={value}")
    return extras, None


def _run_compact_offline(session: str, *, agent: str | None = None, focus: str = "",
                         sources: InvocationSources = InvocationSources(),
                         model: str | None = None) -> int:
    try:
        cfg = run_assembly.from_env(session, save_session=False, sources=sources, agent_id=agent)
        if not cfg.session_file.is_file():
            raise ValueError(msgs.CONFIG_SESSION_MUST_IDENTIFY_AN_EXISTING_JSONL_FILE.format(session=session))
        prompt_spec = P.load_configured_prompt_spec(cfg)
        messages = M.load_replay_messages(cfg.session_file)
        emit, skipped = compaction.track_compact_skips(compaction.printing())
        result = compaction.compact_now_sync(cfg, prompt_spec.system, messages, focus=focus, forced=True,
                                             model=model, emit=emit,
                                             log_dir=compaction.flight_log_dir(cfg))
    except snippets.SnippetError:
        raise
    except Exception as e:  # noqa: BLE001
        msgs.warn(msgs.FAILED, error=network_display.error_text(e))
        return 1
    if result not in skipped:
        msgs.say(msgs.COMPACTION_DONE, result=result)
    return 0

def _compact(args: argparse.Namespace, setup: Setup) -> int:
    """`js --compact SESSION`: -p TEXT is the focus, `-p -` reads it from stdin."""
    focus = _read_stdin_if_piped() if args.prompt == "-" else args.prompt or ""
    return _run_compact_offline(args.compact, agent=setup.agent, focus=focus, sources=setup.sources,
                                model=args.model)


def _commit(args: argparse.Namespace, setup: Setup) -> int:
    """`js --commit [TARGET]`. It runs the commit agent, so --agent beside it
    exits 2. -p TEXT and piped stdin are extra context for the agent."""
    if args.agent:
        msgs.warn(msgs.COMMIT_WITH_AGENT)
        return 2
    extra_context = None
    if args.prompt is not None:
        if args.prompt == "-":
            extra_context = _read_stdin_if_piped()
        else:
            stdin_text = _read_stdin_if_piped()
            extra_context = args.prompt if not stdin_text.strip() else f"{args.prompt.rstrip()}\n\n{stdin_text.strip()}"
    session_catalog.session_leases.start["mode"] = "commit"
    from . import commit_cli

    return commit_cli.run_commit(args.target, model=args.model, debug=args.debug, debug_file=args.debug_file,
                       session=args.session, save=not args.no_save,
                       reasoning=args.reasoning, maxout=args.max_out,
                       extra_context=extra_context, sources=setup.sources)


def _agent_bench(args: argparse.Namespace, setup: Setup) -> int | None:
    """The run's agent runs its NN-benchmark.md cases when it has some and no
    system prompt flag or chat mode replaces its prompt; None otherwise."""
    try:
        agent = _invocation_agent(args, setup.sources)
        agent_dir = _paths.agent_dirs(agent)
    except (ValueError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    if not current().replaces_prompt and not current().chat and P.has_benchmarks(agent_dir):
        from . import bench

        return bench.run_agent_bench(agent, args, setup.sources)
    return None


def _warn_unmatched_presets(sources: InvocationSources) -> None:
    project_dir = Path.cwd()
    for name in sources.presets:
        candidates = _preset_config_paths(replace(sources, presets=(name,)), project_dir)
        if not any(path.exists() for path in candidates):
            if candidates:
                msgs.warn(msgs.PRESET_NOT_FOUND, name=name, looked=" or ".join(str(p) for p in candidates))
            else:
                msgs.warn(msgs.PRESET_NO_CONFIG_DIRS, name=name)


# External binaries js leans on. Each missing one is warned about once at
# startup, so a run without it has an obvious cause, and startup goes on
# without it. `just install` provisions them.
_EXPECTED_BINARIES: tuple[tuple[str, str], ...] = (
    ("rg", "fs_search"),
    ("fd", "file finding"),
    ("bat", "highlighted previews"),
    ("fzf", "the interactive pickers"),
)
_warned_binaries: set[str] = set()


def reset_warnings() -> None:
    """Forget which missing binaries were reported; each is reported again once."""
    _warned_binaries.clear()


def _warn_missing_binaries() -> None:
    for name, why in _EXPECTED_BINARIES:
        if name in _warned_binaries:
            continue
        binary = resolve_binary(name)
        if binary is None:
            _warned_binaries.add(name)
            msgs.warn(msgs.BINARY_MISSING, name=name, user=why)


def main(argv: list[str] | None = None) -> int:
    try:
        code = _main(argv)
    except snippets.SnippetError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        code = 2
    repl = sys.modules.get(f"{__package__}.repl")
    if repl is not None and repl.pending_switch:
        pending = dict(repl.pending_switch)
        repl.pending_switch.clear()
        repl.exec_session(pending["target"])
    return code


@session_catalog.session_scope
def _main(argv: list[str] | None = None) -> int:
    try:
        dispatch_argv = configure_home(list(argv if argv is not None else sys.argv[1:]))
    except ValueError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    loop_controller.claim_child()
    generate = dispatch_argv[:1] == ["generate"]
    options_end = dispatch_argv.index("--") if "--" in dispatch_argv else len(dispatch_argv)
    if not generate and "--acp" in dispatch_argv[:options_end]:
        return _run_acp(dispatch_argv)
    if (error := _startup()) is not None:
        return error
    if generate:
        from . import generate_cli
        dotenv.load()
        return generate_cli.main(dispatch_argv[1:])
    # Handle login/logout before argparse so they don't require a valid agent/config.
    # None of them take -C, so the cwd is already final and .env can load here;
    # otherwise `js --login x` and `js --login=x` (argparse path) would disagree.
    if dispatch_argv and dispatch_argv[0] in (
        "--login", "login", "--logout", "logout", "--models-edit", "models-edit"
    ):
        dotenv.load()
    if dispatch_argv and dispatch_argv[0] in ("--login", "login") and not any(
        flag in dispatch_argv for flag in ("--json", "--json-stream")
    ):
        from . import login_cli
        return login_cli.main(dispatch_argv[1:])
    if dispatch_argv and dispatch_argv[0] in ("--logout", "logout") and not any(
        flag in dispatch_argv for flag in ("--json", "--json-stream")
    ):
        from . import login_cli
        return login_cli.main(["logout"] + dispatch_argv[1:])
    if dispatch_argv and dispatch_argv[0] in ("--models-edit", "models-edit"):
        from . import login_cli
        return login_cli.main(["models-edit"] + dispatch_argv[1:])

    parser = command_line.parser()
    _refuse_glued_short_options(parser, dispatch_argv)
    _validate_output_argv(parser, dispatch_argv)
    args = parser.parse_args(dispatch_argv)
    _validate_output_flags(parser, args)
    _paths.configure_isolate(True if args.isolate else False if args.no_isolate else None)
    if args.last_prompt is not None:
        if args.prompt is not None:
            parser.error(msgs.LAST_PROMPT_WITH_PROMPT.text())
        args.last, args.prompt = True, args.last_prompt
    if args.target is not None and not args.commit:
        parser.error(msgs.UNEXPECTED_ARGUMENT.text(word=args.target))
    if args.bare and args.agent is not None:
        parser.error(msgs.BARE_WITH_AGENT.text())
    if args.loop is not None:
        try:
            loop_settings = run_settings(Path(args.cd).expanduser() if args.cd else None,
                                         sources=_invocation_sources(args))
            return loop_controller.run(dispatch_argv, args.loop, bench=args.bench, prompt=args.prompt,
                                       settings=loop_settings)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    if args.bare:
        args.agent = 'slim'
    options = RunOptions(
        system_prompt_file=args.system_prompt_file, system_prompt=args.system_prompt,
        append_system_prompt=tuple(args.append_system_prompt), you_are=args.you_are,
        no_env=args.no_env or args.bare, chat=args.chat, agent=args.agent,
        no_agents_md=args.no_agents_md, no_skills=args.no_skills or args.bare,
        no_mcp=args.no_mcp or args.bare, no_tools=args.no_tools, tools=args.tools,
        pi=args.pi, pi_strict=args.pi_strict,
    )
    def run() -> int:
        # The control word a run ended on is settled before a --json result
        # is printed, so the result is that of the whole run.
        code = _run_args(args, dispatch_argv)
        return loop_controller.after_run(
            code, dispatch_argv, bench=args.bench, prompt=args.prompt,
            settings=lambda: run_settings(Path(args.cd).expanduser() if args.cd else None,
                                          sources=_invocation_sources(args)),
            relay=None if headless.active is None else headless.active.relay)

    try:
        options.selectors(())
        with invocation(options):
            if _json_run_requested(args):
                return headless.json_run(run, stream=args.json_stream)
            return run()
    except (ValueError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2


def _startup() -> int | None:
    display_mod.blocking_stdio()
    # Before anything reads or writes ~/.js: move the old locations in, once,
    # then make sure every directory of the layout is there.
    _home.migrate_once()
    with contextlib.suppress(OSError):
        _paths.ensure_home()
    try:
        _home.sweep_tmp()
    except (OSError, ValueError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    return None


def _run_acp(dispatch_argv: list[str]) -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            parser = command_line.parser()
            _refuse_glued_short_options(parser, dispatch_argv)
            try:
                args = parser.parse_args(dispatch_argv)
            except SystemExit as exc:
                if exc.code == 0:
                    parser.error(msgs.ACP_HELP_UNSUPPORTED.text())
                raise
            for name in (
                "cd", "prompt", "last_prompt", "files", "session", "session_key", "last", "no_save",
                "json", "json_stream", "quiet", "debug", "debug_file", "stats_json", "stats_csv",
                "verify", "bench", "loop", "lock", "commit", "compact", "migrate_config",
                "login", "logout", "refresh_model_catalog", "list_sessions", "list_agents",
                "list_models", "list_providers", "list_logins", "target",
            ):
                value = getattr(args, name)
                if value not in (None, False, []):
                    option = command_line.flag(name) if name != "target" else value
                    parser.error(msgs.ACP_UNSUPPORTED.text(option=option))
            if args.bare and args.agent is not None:
                parser.error(msgs.BARE_WITH_AGENT.text())
            if (error := _startup()) is not None:
                return error
            from . import acp_agent

        options = command_line.words([arg for arg in command_line.read(dispatch_argv)
                                      if arg.dest != "acp" and arg.words != ("--",)])
        return acp_agent.run(options)
    except (ValueError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2


_NEGATIVE_NUMBER = re.compile(r"-\d+|-\d*\.\d+")


def _refuse_glued_short_options(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    """Exit 2 on a single-dash word that is not a defined option. argparse reads
    one as the short option its first letters name, with the rest glued on as
    the value: `-sysf FILE` is `-s ysf`, a session. Boolean short flags may
    cluster (`-qn`); a word with a value-taking letter in it is refused."""
    known = parser._option_string_actions
    for token in argv:
        if token == "--":
            break
        name = token.partition("=")[0]
        if (not token.startswith("-") or token.startswith("--") or len(token) < 2
                or " " in token or name in known or _NEGATIVE_NUMBER.fullmatch(token)):
            continue
        letters = [known.get("-" + letter) for letter in name[1:]]
        if letters and all(action is not None and action.nargs == 0 for action in letters):
            continue
        parser.error(msgs.UNKNOWN_OPTION.text(option=token))


def _validate_output_argv(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    flags = {word.partition("=")[0] for word in argv[:argv.index("--") if "--" in argv else len(argv)]}
    if {"--json", "--json-stream"} <= flags:
        parser.error(msgs.JSON_FLAGS_EXCLUSIVE.words())
    if flags & {"--json", "--json-stream"}:
        for command in ("--help", "-h", "--help-full"):
            if command in flags:
                error = msgs.JSON_UNSUPPORTED.words(mode="--json/--json-stream", command=command)
                parser.error(error)


def _validate_output_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.json and args.json_stream:
        parser.error(msgs.JSON_FLAGS_EXCLUSIVE.words())
    for name in ("list_sessions", "list_providers", "list_logins"):
        if getattr(args, name) and not args.json:
            error = msgs.LIST_REQUIRES_JSON.words(command="--" + name.replace("_", "-"))
            parser.error(error)
    listings = ("list_sessions", "list_providers", "list_logins", "list_agents", "list_models", "verify")
    for name in listings:
        value = getattr(args, name)
        if value is not None and value is not False and args.json_stream:
            error = msgs.JSON_UNSUPPORTED.words(mode="--json-stream", command="--" + name.replace("_", "-"))
            parser.error(error)
    if args.json or args.json_stream:
        for name in ("commit", "compact", "login", "logout", "migrate_config", "refresh_model_catalog"):
            value = getattr(args, name)
            if value is not None and value is not False:
                error = msgs.JSON_UNSUPPORTED.words(mode="--json-stream" if args.json_stream else "--json", command="--" + name.replace("_", "-"))
                parser.error(error)


def _json_run_requested(args: argparse.Namespace) -> bool:
    return (args.json or args.json_stream) and not any(
        getattr(args, name) is not None and getattr(args, name) is not False
        for name in ("list_sessions", "list_providers", "list_logins", "list_agents", "list_models", "verify")
    )


@dataclass(frozen=True)
class Setup:
    """What the command line settles before a mode runs: the setting sources
    the run reads, and the --agent it names once that is validated."""
    sources: InvocationSources
    agent: str | None = None


def _setup(args: argparse.Namespace, dispatch_argv: list[str]) -> Setup | int:
    """Before any mode: the setting sources with -u and the setting flags in
    them, the session a bare --session picks, .env, and the -C jail. An exit
    code when one of them stops the run."""
    session_catalog.session_leases.start = {"mode": None, "command": ["js", *dispatch_argv]}
    sources = _invocation_sources(args)
    if args.url:
        # Desugar before anything reads the extras. Prepended, not appended, so
        # an explicit --extra on the same command line still overrides a field
        # the spec filled in.
        try:
            endpoint_settings = run_settings(Path(args.cd).expanduser() if args.cd else None,
                                             sources=sources)
            endpoint = endpoint_uri.parse(args.url, settings=endpoint_settings, env=os.environ)
            sources = replace(sources, extras=(*endpoint.as_settings(), *sources.extras))
        except endpoint_uri.EndpointSpecError as exc:
            msgs.warn(msgs.BAD_URL_SPEC, error=exc)
            return 2
    else:
        # The env layer reads a JS_BASE_URL in the -u form; one it cannot
        # read, or one another JS_ variable contradicts, stops the run here.
        try:
            endpoint_uri.from_env(os.environ)
        except endpoint_uri.EndpointSpecError as exc:
            msgs.warn(msgs.BAD_BASE_URL, error=exc)
            return 2
    flag_extras, flag_error = _setting_flag_extras(args)
    if flag_error is not None:
        return flag_error
    # After --extra, so a flag beats an --extra naming the same setting.
    sources = replace(sources, extras=(*sources.extras, *flag_extras))
    set_invocation(sources)
    if args.session == "":
        # A bare --session opens the picker; the chosen session is resumed in
        # its own directory and agent, jailed there when it started under -C.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            msgs.warn(msgs.SESSIONS_NEEDS_TERMINAL)
            return 2
        try:
            from . import session_picker

            choice = session_picker.pick_session(Path.cwd())
            target = session_picker.session_target(choice) if choice is not None else None
        except Exception as exc:  # noqa: BLE001
            msgs.warn(msgs.FAILED, error=f"{type(exc).__name__}: {exc}")
            return 1
        if target is None:
            return 0
        session_picker.enter_session_dir(target)
        args.session = str(target.path)
        if target.agent:
            args.agent = target.agent
        if target.jailed and target.cwd and not args.cd:
            args.cd = target.cwd
    # Fill unset env names from .env, the run's directory (DIR under -C)
    # upward, then ~/.js/.env. The real environment always wins. `just run`
    # already did this via the justfile's dotenv-load; this gives a bare `js`
    # on PATH the same keys. Every setting source is loaded before the jail
    # below is checked and built.
    dotenv.load(Path(args.cd).expanduser() if args.cd else None)
    if args.cd:
        # -C is the jail: every tool that starts a process runs under
        # bubblewrap, and the file tools stay inside DIR and the bound paths.
        # The self-test builds the jail this run uses: its JAIL_BIND and JAIL_HIDE.
        try:
            jail_settings = run_settings(Path(args.cd).expanduser())
        except ValueError as exc:
            msgs.warn(msgs.FAILED, error=exc)
            return 2
        try:
            jailed = _jail.enter(Path(args.cd).expanduser(),
                                 setting=settings.knob(jail_settings, "JAIL_BIND") or (),
                                 hide=settings.knob(jail_settings, "JAIL_HIDE"))
        except _jail.JailError as exc:
            msgs.warn(msgs.JAIL_REFUSED, error=exc)
            return 2
        os.chdir(jailed.root)
        # STOCK_CONTEXT is built at import (before this chdir), so its cwd is
        # stale; rebind it so -p/REPL turns (which fall back to it) run in DIR.
        runtime.T.STOCK_CONTEXT.cwd = Path.cwd()
    for marker in _paths.tracked_markers():
        msgs.warn(msgs.ISOLATE_TRACKED, marker=marker)
    return Setup(sources)


def _settle_session(args: argparse.Namespace, setup: Setup) -> int | None:
    """--last and --session-key name the session; the run takes the chat mode
    a session was saved with and warns about a --preset nothing matches. An
    exit code when the session cannot be named."""
    sources = setup.sources
    if args.last:
        if args.session is not None:
            msgs.warn(msgs.LAST_WITH_SESSION)
            return 2
        try:
            last_agent = _invocation_agent(args, sources)
        except ValueError as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2
        resolved_last = _latest_session_name(last_agent)
        if resolved_last is None:
            msgs.warn(msgs.NO_PREVIOUS_SESSION, agent=last_agent)
            return 2
        args.session = resolved_last
    if args.session_key is not None:
        try:
            effective_agent = _invocation_agent(args, sources)
        except ValueError as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2
        args.session = derive_session_name(effective_agent, Path.cwd(), args.session_key)
    session_catalog.session_leases.caller_key = args.session_key
    run_assembly.inherit_session_chat(args.session)
    if sources.presets:
        # Resolve against the now-final cwd (after any -C). A name that matches no
        # file anywhere is almost certainly a typo — say so rather than no-op.
        _warn_unmatched_presets(sources)
    return None


def _prepare_run(args: argparse.Namespace, setup: Setup) -> Setup | int:
    """Past the listings, for a run: the flags that cannot go together, the
    environment the run's tools read, the missing helper binaries, --agent
    validated into the setup, and --lock. An exit code when one refuses."""
    sources = setup.sources
    if args.debug and args.debug_file:
        msgs.warn(msgs.DEBUG_FLAGS_EXCLUSIVE)
        return 2
    if args.im_a_pussy:
        # Inline code runs by default now; the opt-out flag turns it off for this
        # run via the same env knob the config layer reads.
        os.environ["JS_INLINE_CODE"] = "0"
    if args.no_shell_history:
        # envctx reads this; 0 drops its recent_shell block.
        os.environ["ENVCTX_HIST"] = "0"
    # Past the machine-consumed introspection early-exits: this is a real run, so
    # surface any missing helper binary once (never crash, never block startup).
    _warn_missing_binaries()
    cli_agent = None
    if args.agent:
        try:
            cli_agent = validate_agent_id(args.agent)
        except ValueError as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2

    if args.lock:
        try:
            agent_lock.acquire(args.bench and validate_agent_id(args.bench) or _invocation_agent(args, sources))
        except (ValueError, agent_lock.Busy, OSError) as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2

    selected_modes = [name for name, enabled in (("commit", args.commit), ("compact", args.compact)) if enabled]
    if len(selected_modes) > 1:
        msgs.warn(msgs.MODES_EXCLUSIVE)
        return 2
    if args.files and selected_modes:
        msgs.warn(msgs.FILE_NEEDS_PROMPT_MODE)
        return 2
    return replace(setup, agent=cli_agent)


def _run_args(args: argparse.Namespace, dispatch_argv: list[str]) -> int:
    """The mode picker: setup, then each mode in turn, the first named on the
    command line running, then a -p run or the REPL."""
    setup = _setup(args, dispatch_argv)
    if isinstance(setup, int):
        return setup
    if args.verify is not None:
        from . import verify

        return verify.run_verify(args)
    if args.list_sessions:
        return listings.run_list_sessions(args)
    if (code := _settle_session(args, setup)) is not None:
        return code
    if args.login is not None:
        from . import login_cli
        return login_cli.main([args.login] if args.login else [])
    if args.logout:
        from . import login_cli
        return login_cli.main(["logout", args.logout])
    if args.list_agents:
        return listings.run_list_agents(args)
    if args.list_providers:
        return listings.print_json(listings.providers_json())
    if args.list_logins:
        return listings.print_json(listings.logins_json())
    if args.list_models is not None:
        return listings.run_list_models(args, lambda: run_assembly.from_env(
            args.session, save_session=False, sources=setup.sources, agent_id=args.agent))
    if args.refresh_model_catalog and (code := model_metadata.run_refresh_model_catalog(args)) is not None:
        return code
    setup = _prepare_run(args, setup)
    if isinstance(setup, int):
        return setup
    if args.bench:
        from . import bench

        return bench.run_bench_mode(args, setup.sources)
    if args.compact:
        return _compact(args, setup)
    if args.migrate_config:
        from . import migrate_config

        return migrate_config.run_migrate_config()
    if args.commit:
        return _commit(args, setup)
    if (code := _agent_bench(args, setup)) is not None:
        return code
    exchanges_only = (args.prompt is None and not args.files and sys.stdin.isatty()
                      and _exchanges_are_the_prompt(args, setup.agent, setup.sources))
    if args.prompt is not None or args.files or not sys.stdin.isatty() or exchanges_only:
        return _run_headless(args, setup, exchanges_only=exchanges_only)
    if args.json or args.json_stream:
        msgs.warn(msgs.FAILED, error="--json/--json-stream requires -p, -f, a pipe, an agent run, or --bench")
        return 2
    from . import repl

    return repl.start_repl(args, setup.sources, setup.agent)


def _run_headless(args: argparse.Namespace, setup: Setup, *, exchanges_only: bool) -> int:
    """One turn with no REPL: -p PROMPT, piped stdin as the prompt or beside it,
    `-f -` attaching stdin, -f files alone as the prompt, or the agent's own
    exchanges when they are the prompt: no -p and nothing piped."""
    os.environ["JS_MODE"] = "headless"
    stdin_attachment = None
    if "-" in args.files:
        if args.prompt in {None, "-"}:
            msgs.warn(msgs.STDIN_TWICE)
            return 2
        if sys.stdin.isatty():
            msgs.warn(msgs.STDIN_ATTACHMENT_NOT_PIPED)
            return 2
        stdin_attachment = _read_stdin_attachment_if_piped()
        prompt = args.prompt or ""
    elif exchanges_only:
        prompt = None
    elif args.prompt in {None, "-"}:
        prompt = _read_stdin_if_piped()
        if (args.prompt is None and not prompt.strip() and not args.files
                and _exchanges_are_the_prompt(args, setup.agent, setup.sources)):
            # Nothing was piped (cron, nohup, </dev/null): the exchanges are the prompt.
            exchanges_only, prompt = True, None
    elif sys.stdin.isatty():
        prompt = args.prompt
    else:
        stdin_text = _read_stdin_if_piped()
        prompt = args.prompt if not stdin_text.strip() else f"{args.prompt.rstrip()}\n\n{stdin_text.strip()}"
    session_catalog.session_leases.start["mode"] = "-p" if args.prompt is not None or exchanges_only else "pipe"
    result = prompt_run.run(prompt, model=args.model, debug=args.debug, debug_file=args.debug_file,
                            agent=args.agent, session=args.session, save=not args.no_save,
                            caller_key=args.session_key,
                            reasoning=args.reasoning, maxout=args.max_out,
                            show_continue=not args.quiet,
                            sources=setup.sources,
                            files=args.files,
                            stdin_attachment=stdin_attachment,
                            stats_json=args.stats_json, stats_csv=args.stats_csv,
                            events=headless.active)
    if args.no_save:
        msgs.warn(msgs.NOT_SAVED_NO_RESUME)
    return result


if __name__ == "__main__":
    sys.exit(main())
