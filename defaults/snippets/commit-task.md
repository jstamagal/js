Commit all work in this target directory: {{DIRECTORY}}

The repository has already been initialized if it was missing. Use this deterministic staging helper for every commit unit:
`{{STAGE}}`
Use `all` for whole-file staging or comma-separated hunk numbers from the survey for tracked text files. To commit a staged unit, write the full commit message to a file with the write tool, then run:
`{{COMMIT}}`
If you need a fresh snapshot after making changes, run:
`{{SURVEY_COMMAND}}`

Initial deterministic commit_helper survey:
```text
{{SURVEY}}
```