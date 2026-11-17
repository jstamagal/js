"""The js command line: the parser that defines it, an argv read the way that
parser reads it, and the words that give an option a value.

Everything here that knows which options take a value asks the parser."""
from __future__ import annotations

import argparse
import functools
import sys
from dataclasses import dataclass, fields
from typing import Any

from . import messages as msgs, sampling as sampling_mod


class _ShortHelpAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser._print_message(msgs.SHORT_HELP.words(), sys.stdout)
        parser.exit()


def parser() -> argparse.ArgumentParser:
    """The parser that defines the js command line."""
    js = msgs.ArgumentParser(add_help=False, allow_abbrev=False)
    groups = {name: js.add_argument_group(name) for name in (
        'RUNNING',
        'PROMPT',
        'WHAT THE MODEL GETS',
        'SAMPLING',
        'SESSIONS',
        'CONFIG',
        'OUTPUT & DEBUG',
        'MODELS & LOGINS',
        'MODES',
    )}
    groups['RUNNING'].add_argument("-h", "--help", action=_ShortHelpAction, nargs=0, help=msgs.OPT_HELP.words())
    groups['RUNNING'].add_argument("--help-full", action="help", help=msgs.OPT_HELP_FULL.words())
    groups['MODELS & LOGINS'].add_argument("--login", metavar="PROVIDER", nargs="?", const="", help=msgs.OPT_LOGIN.words())
    groups['MODELS & LOGINS'].add_argument("--logout", metavar="PROVIDER", help=msgs.OPT_LOGOUT.words())
    groups['PROMPT'].add_argument("-p", "--prompt", nargs="?", const="-", help=msgs.OPT_PROMPT.words())
    groups['PROMPT'].add_argument("-lp", dest="last_prompt", nargs="?", const="-", metavar="PROMPT",
                                  help=msgs.OPT_LAST_PROMPT.words())
    groups['PROMPT'].add_argument("-f", "--file", dest="files", action="append", default=[], metavar="PATH", help=msgs.OPT_FILE.words())
    groups['RUNNING'].add_argument("-a", "--agent", help=msgs.OPT_AGENT.words())
    groups['RUNNING'].add_argument("-m", "--model", help=msgs.OPT_MODEL.words())
    groups['RUNNING'].add_argument("-u", "--url", dest="url", metavar="SPEC",
                        help=msgs.OPT_URL.words())
    groups['RUNNING'].add_argument("-C", dest="cd", metavar="DIR", help=msgs.OPT_CD.words())
    groups['OUTPUT & DEBUG'].add_argument("-d", "--debug", action="store_true", help=msgs.OPT_DEBUG.words())
    groups['OUTPUT & DEBUG'].add_argument("--debug-file", dest="debug_file", metavar="PATH", help=msgs.OPT_DEBUG_FILE.words())
    session_group = groups["SESSIONS"].add_mutually_exclusive_group()
    session_group.add_argument("-s", "--session", nargs="?", const="", metavar="NAME", help=msgs.OPT_SESSION.words())
    session_group.add_argument("--session-key", metavar="KEY", help=msgs.OPT_SESSION_KEY.words())
    groups['SESSIONS'].add_argument("-n", "--no-save", action="store_true", help=msgs.OPT_NO_SAVE.words())
    groups['OUTPUT & DEBUG'].add_argument("-q", "--quiet", action="store_true", help=msgs.OPT_QUIET.words())
    groups['SAMPLING'].add_argument("-r", "--reasoning", help=msgs.OPT_REASONING.words())
    groups['RUNNING'].add_argument("--max-out", dest="max_out", type=int, help=msgs.OPT_MAX_OUT.words())
    groups['RUNNING'].add_argument("--max-turns", dest="max_turns", metavar="N", help=msgs.OPT_MAX_TURNS.words())
    groups['SAMPLING'].add_argument("--template-kwargs", dest="template_kwargs", metavar="KEY=VALUE[,...]",
                        help=msgs.OPT_TEMPLATE_KWARGS.words())
    for field in sampling_mod.SAMPLING_FIELDS:
        groups['SAMPLING'].add_argument("--" + field.replace("_", "-"), dest=field, metavar="VALUE",
                            help=msgs.OPT_SETTING_FLAG.words(setting=field.upper()))
    prompt_group = groups["WHAT THE MODEL GETS"].add_mutually_exclusive_group()
    prompt_group.add_argument('--system-prompt-file', metavar='PATH',
                              help=msgs.OPT_SYSTEM_PROMPT_FILE.words())
    prompt_group.add_argument('--system-prompt', metavar='TEXT',
                              help=msgs.OPT_SYSTEM_PROMPT.words())
    prompt_group.add_argument('--you-are', nargs='?', const='', metavar='TEXT',
                              help=msgs.OPT_YOU_ARE.words())
    groups['WHAT THE MODEL GETS'].add_argument('--append-system-prompt', action='append', default=[], metavar='TEXT',
                        help=msgs.OPT_APPEND_SYSTEM_PROMPT.words())
    groups['WHAT THE MODEL GETS'].add_argument('--no-env', action='store_true', help=msgs.OPT_NO_ENV.words())
    groups['WHAT THE MODEL GETS'].add_argument('--chat', action='store_true', help=msgs.OPT_CHAT.words())
    tools_group = groups["WHAT THE MODEL GETS"].add_mutually_exclusive_group()
    tools_group.add_argument('--tools', metavar='LIST', help=msgs.OPT_TOOLS.words())
    tools_group.add_argument('--no-tools', action='store_true', help=msgs.OPT_NO_TOOLS.words())
    groups['WHAT THE MODEL GETS'].add_argument('--no-agents-md', action='store_true', help=msgs.OPT_NO_AGENTS_MD.words())
    groups['WHAT THE MODEL GETS'].add_argument('--no-skills', action='store_true', help=msgs.OPT_NO_SKILLS.words())
    groups['WHAT THE MODEL GETS'].add_argument('--no-mcp', action='store_true', help=msgs.OPT_NO_MCP.words())
    groups['WHAT THE MODEL GETS'].add_argument('--bare', action='store_true', help=msgs.OPT_BARE.words())
    groups['MODES'].add_argument('--verify', nargs='?', const='', metavar='AGENT', help=msgs.OPT_VERIFY.words())
    groups['MODES'].add_argument('--bench-models', action='store_true', help=msgs.OPT_BENCH_MODELS.words())
    groups['MODES'].add_argument("--bench", metavar="AGENT", help=msgs.OPT_BENCH.words())
    groups['MODES'].add_argument("--loop", nargs="?", const="", metavar="N|NM|NH", help=msgs.OPT_LOOP.words())
    groups['MODES'].add_argument("--lock", action="store_true", help=msgs.OPT_LOCK.words())
    groups['OUTPUT & DEBUG'].add_argument("--stats-json", dest="stats_json", metavar="PATH", help=msgs.OPT_STATS_JSON.words())
    groups['OUTPUT & DEBUG'].add_argument("--stats-csv", dest="stats_csv", metavar="PATH", help=msgs.OPT_STATS_CSV.words())
    groups['CONFIG'].add_argument("--home", metavar="DIR", help=msgs.OPT_HOME.words())
    groups['CONFIG'].add_argument("--extra", dest="extras", action="append", default=[], metavar="KEY=VALUE",
                        help=msgs.OPT_EXTRA.words())
    groups['CONFIG'].add_argument("--preset", dest="presets", action="append", default=[], metavar="NAME[,NAME...]",
                        help=msgs.OPT_PRESET.words())
    groups['CONFIG'].add_argument("--no-local-config", action="store_true", help=msgs.OPT_NO_LOCAL_CONFIG.words())
    groups['CONFIG'].add_argument("--no-global-config", action="store_true", help=msgs.OPT_NO_GLOBAL_CONFIG.words())
    groups['CONFIG'].add_argument("--migrate-config", action="store_true", help=msgs.OPT_MIGRATE_CONFIG.words())
    groups['SESSIONS'].add_argument("--list-sessions", action="store_true", help=msgs.OPT_LIST_SESSIONS.words())
    groups['RUNNING'].add_argument("--list-agents", action="store_true", help=msgs.OPT_LIST_AGENTS.words())
    groups['SESSIONS'].add_argument("--last", action="store_true", help=msgs.OPT_LAST.words())
    groups['OUTPUT & DEBUG'].add_argument("--json", action="store_true", help=msgs.OPT_JSON.words())
    groups['MODES'].add_argument("--swarm", metavar="ROOT/NAME", help=msgs.OPT_SWARM.words())
    groups['OUTPUT & DEBUG'].add_argument("--json-stream", action="store_true", help=msgs.OPT_JSON_STREAM.words())
    groups['MODELS & LOGINS'].add_argument("--list-providers", action="store_true", help=msgs.OPT_LIST_PROVIDERS.words())
    groups['MODELS & LOGINS'].add_argument("--list-logins", action="store_true", help=msgs.OPT_LIST_LOGINS.words())
    groups['MODELS & LOGINS'].add_argument("--list-models", nargs="?", const="", metavar="PROVIDER", help=msgs.OPT_LIST_MODELS.words())
    groups['MODELS & LOGINS'].add_argument("--refresh-model-catalog", action="store_true", help=msgs.OPT_REFRESH_MODEL_CATALOG.words())
    groups['MODES'].add_argument("--commit", action="store_true", help=msgs.OPT_COMMIT.words())
    groups['MODES'].add_argument("--compact", metavar="SESSION", help=msgs.OPT_COMPACT.words())
    groups['WHAT THE MODEL GETS'].add_argument("--im-a-pussy", dest="im_a_pussy", action="store_true",
                        help=msgs.OPT_IM_A_PUSSY.words())
    groups['WHAT THE MODEL GETS'].add_argument("-i", "--no-shell-history", dest="no_shell_history", action="store_true",
                        help=msgs.OPT_NO_SHELL_HISTORY.words())
    groups['MODES'].add_argument("target", nargs="?", help=msgs.OPT_TARGET.words())
    return js


@functools.cache
def _options() -> argparse.ArgumentParser:
    """The parser, built once, for what it says about its options. Its help
    is never shown."""
    return parser()


@dataclass(frozen=True)
class Arg:
    """One argument of a js command line: an option with the value it takes,
    or a positional word."""

    words: tuple[str, ...]
    # The option string it names; None for a positional word.
    option: str | None = None
    # The parser destination the option sets; None for a positional word or
    # an option the parser does not define.
    dest: str | None = None
    # The value the option takes, or the positional word; None for an option
    # that takes none.
    value: str | None = None
    # How a value inside the option's own word is joined to it: "=" for
    # `--flag=value`, "" for `-xVALUE`; None when the value is a word of its
    # own or there is none.
    sep: str | None = None

    def given(self, value: str) -> tuple[str, ...]:
        """This option taking `value`, written the way this argument is. A
        value the parser would read as an option when it is a word of its own
        is joined to the option with `=`."""
        if self.sep is not None:
            return (f"{self.option}{self.sep}{value}",)
        if _option(_options(), value) is not None:
            return (f"{self.option}={value}",)
        return (self.option, value)


def read(argv: list[str]) -> list[Arg]:
    """`argv` as the js parser reads it. Every word from `--` on is a
    positional. A cluster of switches (`-qn`) is one Arg named by its first."""
    js = _options()
    args: list[Arg] = []
    index = 0
    while index < len(argv):
        word = argv[index]
        if word == "--":
            args += [Arg((rest,), value=rest) for rest in argv[index:]]
            break
        found = _option(js, word)
        if found is None:
            args.append(Arg((word,), value=word))
            index += 1
            continue
        action, option, sep, inline = found
        if action is None:
            args.append(Arg((word,), option))
            index += 1
            continue
        if action.nargs not in (None, "?", 0):
            raise ValueError(f"{option} takes nargs={action.nargs!r}, which read() does not know")
        if action.nargs == 0:
            args.append(Arg((word,), option, action.dest))
            index += 1
        elif sep is not None:
            args.append(Arg((word,), option, action.dest, inline, sep))
            index += 1
        elif index + 1 < len(argv) and argv[index + 1] != "--" and _option(js, argv[index + 1]) is None:
            args.append(Arg((word, argv[index + 1]), option, action.dest, argv[index + 1]))
            index += 2
        else:
            args.append(Arg((word,), option, action.dest))
            index += 1
    return args


def _option(js: argparse.ArgumentParser, word: str) -> tuple[argparse.Action | None, str, str | None, str | None] | None:
    """(action, option string, sep, inline value) for a word argparse reads as
    an option, the action None when the parser has no such option; None for
    a word it reads as a value. The parser has allow_abbrev off."""
    actions = js._option_string_actions
    if not word.startswith("-"):
        return None
    if word in actions:
        return actions[word], word, None, None
    if len(word) == 1:
        return None
    head, sep, tail = word.partition("=")
    if sep and head in actions:
        return actions[head], head, sep, tail
    if word[1] != "-" and word[:2] in actions:
        return actions[word[:2]], word[:2], "", word[2:]
    if js._negative_number_matcher.match(word) and not js._has_negative_number_optionals:
        return None
    if " " in word:
        return None
    return None, word, None, None


def words(args: list[Arg]) -> list[str]:
    """The argv `args` were read from, or are rewritten into."""
    return [word for arg in args for word in arg.words]


def argv(*given: tuple[str, Any]) -> list[str]:
    """The words that give each (option, value) pair: nothing for None or
    False, the option alone for a switch, the option before each item for a
    repeated option, else the option and the value."""
    actions = _options()._option_string_actions
    out: list[str] = []
    for option, value in given:
        action = actions[option]
        if value is None or value is False:
            continue
        if action.nargs == 0:
            out.append(option)
        elif isinstance(action, argparse._AppendAction):
            for item in value:
                out += [option, item]
        else:
            out += [option, str(value)]
    return out


def flag(dest: str) -> str:
    """The option that sets parser destination `dest`: its long form when it
    has one."""
    for action in _options()._actions:
        if action.dest == dest and action.option_strings:
            return action.option_strings[-1]
    raise KeyError(dest)


def options_argv(options: Any) -> list[str]:
    """The options that give a run `options` (a `cli_options.RunOptions`),
    each field through the parser option of the same name, in field order.
    The agent is not among them: the command names the agent it resolved."""
    return argv(*((flag(field.name), getattr(options, field.name))
                  for field in fields(options) if field.name != "agent"))
