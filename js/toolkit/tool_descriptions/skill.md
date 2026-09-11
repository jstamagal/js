Load local skill instructions by name.

Use this when a specialized local workflow is relevant to the operator's
request.

A skill is a directory holding a `SKILL.md` (the Agent Skills format):
`<root>/<name>/SKILL.md`. Roots, lowest layer to highest:
- package: `js/skills/`
- global: `~/.agents/skills/`, then `~/.config/js/skills/`
- project: `./.agents/skills/`, then `./.js/skills/`

Later layers override earlier ones by name; within a layer the js-native
directory wins.

Rules:
- Only load skills that are relevant to the current task.
- Do not call a skill that is already active.
- Follow the loaded skill instructions before taking task actions.
- If no local skill matches, the tool returns an error.
