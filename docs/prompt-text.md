# Model text

Actual instructions to the model live in `defaults/snippets/<name>.md`.
Copy one to `<home>/snippets/<name>.md` to change it. The home is selected by
`--home`, `JS_HOME`, or `~/.js`; an empty file shadows stock too. A missing
snippet is an error naming it and the searched directories.

`js.snippets.render(name, **values)` resolves the file when used and substitutes
`{{NAME}}` fields once. Replacement values are inserted literally and never
rescanned. This keeps user text or a prior summary from becoming a directive.
Snippet placeholders are separate from the inline directives expanded in agent
prompt files.

The main snippets are `commit-task`, `compaction-summary`, `compaction-update`,
`compaction-rehydrate`, `output-limit-resume` and `flush-context`. Further model
instructions use the same directory. Tool parameter descriptions live with each
tool's contract in `defaults/tool_descriptions/`, overridable in the home.
Tool result and error text the model reads lives in its owning Python module;
text shown to the operator lives in `js/messages.py`.
