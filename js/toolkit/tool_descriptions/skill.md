Load local skill instructions by name.

Use this when a specialized local workflow is relevant to the operator's
request.

Lookup order:
- `js/skills/<name>.md`
- `js/skills/<name>/SKILL.md`
- `./skills/<name>.md`
- `./skills/<name>/README.md`
- `./.skills/<name>.md`

Rules:
- Only load skills that are relevant to the current task.
- Do not call a skill that is already active.
- Follow the loaded skill instructions before taking task actions.
- If no local skill matches, the tool returns an error.
