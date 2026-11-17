"""The REPL's agent commands: /persona, /tools, /skill and /skills, the
system prompt, tools and skills a turn carries."""

from __future__ import annotations

import json
from pathlib import Path

from . import (
    messages as msgs,
    repl_settings_commands,
    repl_state,
    run_assembly,
    skill_picker,
    skills,
)
from .config import Config
from .repl_state import Command, ReplState
from .toolkit import policy as tool_policy


def _cmd_persona(arg: str, state: ReplState, cfg: Config) -> str | None:
    text = state.system
    print(text[:2048])
    if len(text) > 2048:
        msgs.say(msgs.PERSONA_TRUNCATED, size=len(text))
    return None


def _print_tool_chain(state: ReplState, cfg: Config) -> None:
    """Every tool with its resolved state and the chain entry that decided it."""
    full = run_assembly.registry_for(state.config_for_turn(cfg))
    try:
        config = tool_policy.load_tools_config()
        rules = tool_policy.expand(state.tool_selectors, config, f"agent {cfg.agent_id!r}")
    except tool_policy.ToolPolicyError as exc:
        msgs.say(msgs.FAILED, error=exc)
        return
    for row in tool_policy.render_table(tool_policy.resolve(full.tools, rules), config.bans):
        print(row)


def _cmd_tools(arg: str, state: ReplState, cfg: Config) -> str | None:
    _print_tool_chain(state, cfg)
    return None


def _print_skill_catalog(state: ReplState) -> None:
    """`/skill` alone lists every enabled skill; user-only ones are marked."""
    catalog = skills.discover_skills(Path.cwd(), disabled=repl_state.disabled_skills(state))
    if not catalog.skills:
        msgs.say(msgs.NO_SKILLS)
        return
    for skill in catalog.skills:
        entry = msgs.SKILL_ROW_USER_ONLY if not skill.model_invocable else msgs.SKILL_ROW
        msgs.say(entry, name=skill.name, source=skill.source, description=skill.description)
    msgs.say(msgs.SKILL_HINT)


def _cmd_skill(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Bare `/skill` lists the skills. `/skill <name> [request]` typed at the
    input line never reaches the table: it is a turn (see
    repl_commands.handle_command)."""
    if arg:
        return msgs.SKILL_IS_A_TURN.said()
    _print_skill_catalog(state)
    return None


def _cmd_skills(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Open the skill picker. Enter there sets SKILLS_DISABLED to the skills
    left switched off."""
    current = repl_state.disabled_skills(state)
    try:
        chosen = skill_picker.pick_skills(Path.cwd(), current)
    except Exception as exc:  # noqa: BLE001
        return msgs.FAILED.said(error=f"{type(exc).__name__}: {exc}")
    if chosen is None or tuple(chosen) == current:
        return None
    return repl_settings_commands.cmd_set(f"SKILLS_DISABLED {json.dumps(list(chosen))}", state, cfg)


COMMANDS: dict[str, Command] = {
    "persona": Command(_cmd_persona, "persona", msgs.CMD_PERSONA),
    "tools": Command(_cmd_tools, "tools", msgs.CMD_TOOLS),
    "skill": Command(_cmd_skill, "skill [name [request]]",
                     msgs.CMD_SKILL),
    "skills": Command(_cmd_skills, "skills", msgs.CMD_SKILLS),
}
