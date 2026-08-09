Load local skill instructions by name.

Use this when a specialized local workflow is relevant to the operator's
request.

Skills are layered last-match-wins: project overrides global, and global
overrides the package catalog. Within the project, `./skills/` overrides
`./.skills/`. Within every root, the layout precedence is:
- `<root>/<name>.md`
- `<root>/<name>/README.md`
- `<root>/<name>/SKILL.md`

The roots, from lowest to highest layer, are the package `js/skills/` directory,
the platform config directory (`~/.config/js/skills/` by default), then project
`./.skills/` and `./skills/`.

Rules:
- Only load skills that are relevant to the current task.
- Do not call a skill that is already active.
- Follow the loaded skill instructions before taking task actions.
- If no local skill matches, the tool returns an error.
