"""Read-only validation of configuration, agent prompts, and reachable skills."""
from __future__ import annotations

from pathlib import Path

from . import paths, persona, settings, skills
from .toolkit import policy


def verify(agent: str | None = None) -> list[str]:
    errors: list[str] = []
    home = paths.home()
    project = Path.cwd()
    repo = Path(__file__).resolve().parents[1]
    roots = (repo / 'prompts', paths.global_agents_dir(), project / '.js' / 'agents')

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
        configs = {repo / 'js' / 'jsrc', home / 'jsrc', project / '.js' / 'jsrc', project / '.js' / 'jsrc.local'}
        configs.update(home.rglob('jsrc'))
        configs.update(home.rglob('jsrc.*'))
        for path in sorted(configs):
            if path.is_file():
                def validate_config(path=path):
                    errors.extend(settings.load_jsrc_files([path], settings.seed_defaults()))
                check(path, validate_config)
        directories = set()
        for root in roots:
            if root.is_dir():
                directories.update(path for path in root.iterdir() if path.is_dir())
        directories.update(path.parent for path in home.rglob('agent.yaml'))
        directories.update(path.parent for path in home.rglob('agents.yaml'))
        directories = sorted(directories)

    for directory in directories:
        def validate_agent(directory=directory):
            spec = persona.load_prompt_spec(directory)
            persona.load_benchmarks(directory)
            if config is not None:
                policy.expand(spec.tool_selectors, config, str(directory))
        check(directory, validate_agent)

    skill_roots = (skills.BUILTIN_SKILLS_DIR, paths.global_skills_dir(), paths.shared_skills_dir(),
                   project / '.agents' / 'skills', project / '.js' / 'skills')
    for root in skill_roots:
        for path in skills._skill_paths(root):
            check(path, lambda path=path: skills._index_skill(path, 'verify'))
    return errors
