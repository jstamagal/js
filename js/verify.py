"""Read-only validation of configuration, agent prompts, and reachable skills."""
from __future__ import annotations

from pathlib import Path

from . import paths, persona, settings, skills
from .toolkit import policy


def verify(agent: str | None = None) -> list[str]:
    errors: list[str] = []
    home = paths.home()
    project = Path.cwd()
    roots = (paths.defaults_dir() / 'agents', paths.global_agents_dir(), paths.project_home(project) / 'agents')

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
        directories = [persona._most_specific_prompt_dir(agent, *roots)]
    else:
        configs = {paths.defaults_dir() / 'jsrc', home / 'jsrc', paths.project_home(project) / 'jsrc', paths.project_home(project) / 'jsrc.local'}
        for directory in (home, paths.project_home(project)):
            configs.update(directory.glob('jsrc.*'))
        for path in sorted(configs):
            if path.is_file():
                def validate_config(path=path):
                    errors.extend(settings.load_jsrc_files([path], settings.seed_defaults()))
                check(path, validate_config)
        directories = set()
        for root in roots:
            if root.is_dir():
                directories.update(path for path in root.iterdir() if path.is_dir())
        directories = sorted(directories)

    for directory in directories:
        def validate_agent(directory=directory):
            spec = persona.load_prompt_spec(directory)
            persona.load_benchmarks(directory)
            if config is not None:
                policy.expand(spec.tool_selectors, config, str(directory))
                for profile in spec.models:
                    policy.expand(profile.tools or (), config, f"{directory} models")
        check(directory, validate_agent)

    shared_project = paths.shared_skills_dir() if project.resolve() == paths.user_home().resolve() else project / '.agents' / 'skills'
    skill_roots = (skills.BUILTIN_SKILLS_DIR, paths.global_skills_dir(), paths.shared_skills_dir(),
                   shared_project, paths.project_home(project) / 'skills')
    for root in skill_roots:
        for path in skills._skill_paths(root):
            check(path, lambda path=path: skills._index_skill(path, 'verify'))
    return errors
