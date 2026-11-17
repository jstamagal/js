"""Read-only validation of configuration, agent prompts, and reachable skills."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import listings, paths, persona, setting_layers, settings, skills
from .toolkit import policy


def run_verify(args: argparse.Namespace) -> int:
    """`js --verify [AGENT]`: each error on stderr and a count, or with --json one
    {ok, errors} object. Exits 1 when there is an error."""
    errors = verify(args.verify or None)
    if args.json:
        listings.print_json({'ok': not errors, 'errors': errors})
    else:
        for error in errors:
            print(error, file=sys.stderr)
        print(f'Verification: {len(errors)} error(s).')
    return int(bool(errors))


def verify(agent: str | None = None) -> list[str]:
    errors: list[str] = []
    home = paths.home()
    project = Path.cwd()
    roots = paths.agent_roots(project)

    def check(path, action):
        try:
            action()
        except (ValueError, OSError) as exc:
            errors.append(f'{path}: {exc}')

    config = None
    try:
        config = policy.load_tools_config()
        for name, entries in config.tags.items():
            policy.expand(entries, config, f'tag:{name}')
    except ValueError as exc:
        errors.append(str(exc))

    if agent:
        from .config import validate_agent_id
        validate_agent_id(agent)
        agent_ids = [agent]
    else:
        configs = {paths.defaults_dir() / 'jsrc', home / 'jsrc', paths.project_home(project) / 'jsrc', paths.project_home(project) / 'jsrc.local'}
        for directory in (home, paths.project_home(project)):
            configs.update(directory.glob('jsrc.*'))
        for path in sorted(configs):
            if path.is_file():
                def validate_config(path=path):
                    errors.extend(setting_layers.load_jsrc_files([path], settings.seed_defaults()))
                check(path, validate_config)
        agent_ids = paths.agent_names(roots)

    for agent_id in agent_ids:
        try:
            directories = paths.agent_dirs(agent_id, roots)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        location = ", ".join(map(str, directories))

        def validate_agent(agent_id=agent_id, directories=directories, location=location):
            spec = persona.load_agent_prompt_spec(agent_id, roots)
            persona.load_benchmarks(directories)
            if config is not None:
                policy.expand(spec.tool_selectors, config, location)
                for profile in spec.models:
                    policy.expand(profile.tools or (), config, f"{location} models")
        check(location, validate_agent)

    shared_project = paths.shared_skills_dir() if project.resolve() == paths.user_home().resolve() else project / '.agents' / 'skills'
    skill_roots = (skills.BUILTIN_SKILLS_DIR, paths.global_skills_dir(), paths.shared_skills_dir(),
                   shared_project, paths.project_home(project) / 'skills')
    for root in skill_roots:
        for path in skills._skill_paths(root):
            check(path, lambda path=path: skills._index_skill(path, 'verify'))
    return errors
