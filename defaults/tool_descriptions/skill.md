Load a skill's full instructions by name.

Skills are specialized instructions for specific tasks. When a task matches
an available skill's description, call this tool with the skill's name to
load its instructions, then follow them.

Rules:
- Only load skills that are relevant to the current task.
- Do not load a skill that is already loaded in this conversation.
- The result is a `<skill_content>` block: the skill's instructions as
  whoever wrote the skill wrote them, the skill's directory, and the files
  bundled with it (`<skill_resources>`). Relative paths in the instructions
  resolve against the skill directory; pass absolute paths to tools. Read a
  bundled file only when the instructions call for it.
- A skill whose frontmatter sets `disable-model-invocation: true` is user-only:
  it is not listed, and the user loads it with `/skill <name>`.
- A skill whose frontmatter lists `paths:` globs is also offered in a
  `<js-reminder>` the first time read, write or patch touches a matching file.

A skill is a directory holding a `SKILL.md` (the Agent Skills format):
`<root>/<name>/SKILL.md`, where `name` in its frontmatter matches the
directory. Roots, lowest layer to highest:
- built-in: `defaults/skills/` (shipped with js)
- global: `~/.agents/skills/`, then `~/.js/skills/`
- project: each directory from the repository root down to the working
  directory, its `.agents/skills/` then its `.js/skills/`

A nearer layer's skill replaces a farther one of the same name.

<!--parameters-->

### name

The name of an available skill.
