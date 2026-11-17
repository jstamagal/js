"""Read-only validation of configuration, agent prompts, and reachable skills."""
from __future__ import annotations

from pathlib import Path

from . import paths, persona, settings, skills
from .toolkit import policy


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
                    errors.extend(settings.load_jsrc_files([path], settings.seed_defaults()))
                check(path, validate_config)
        agent_ids = sorted({directory.name for root in roots if root.is_dir()
                            for directory in root.iterdir() if directory.is_dir()})

    for agent_id in agent_ids:
        directories = persona._most_specific_prompt_dir(agent_id, *roots)
        location = ", ".join(map(str, directories))

        def validate_agent(agent_id=agent_id, directories=directories, location=location):
            spec = persona.load_agent_prompt_spec(
                agent_id, repo_prompts_root=roots[0], global_agents_root=roots[1],
                project_agents_root=roots[2])
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
