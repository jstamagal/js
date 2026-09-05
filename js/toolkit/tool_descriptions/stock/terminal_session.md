Drive an interactive command through a real persistent terminal (PTY).

Use this for TUIs, REPLs, pagers, editors, installers, prompts, or any command
whose behaviour depends on terminal input and screen redraws. Sessions belong
to this agent run and persist across tool calls.

Parameters:
- `action` (required): `start`, `send`, `look`, `stop`, or `list`.
- `session` (default `main`): name of the terminal session.
- `command`: shell command for `start`.
- `keys`: comma-separated input for `send`. Named keys include `enter`, `tab`,
  `esc`, arrows, `home`, `end`, `pgup`, `pgdn`, `backspace`, `delete`,
  `ctrl-c`, `ctrl-d`, `ctrl-l`, and `f1` through `f12`. Any other token is
  typed literally; use `comma` to type a comma.
- `cwd`: working directory for `start`, relative to the task workspace unless
  absolute.
- `wait_ms` (default `700`, maximum `10000`): time to collect redraw output
  after the action. A zero wait still polls once for output already available.
- `cols` and `rows` (defaults `64` by `36`, maximum `400` by `200`): terminal
  screen dimensions for `start` only. Passing either with another action is an
  error.

The result includes the rendered terminal lines, cursor position, and process
state. `lines_changed` and `screen_responded` compare this screen with the most
recent observation. For `send`, they indicate change after the sent keys; for
`look`, they indicate passive change since the prior observation.
{{#if terminal_snapshot}}
`terminal_snapshot` also updates the comparison baseline before a later `send`.
{{/if}}
A running process is normal for a TUI; stop sessions when finished.

{{#if terminal_snapshot}}
Use `terminal_snapshot` when text is not enough to judge spacing, colour,
borders, clipping, or the actual visual state of the TUI.
{{/if}}

When not to use: use `shell` for ordinary non-interactive commands that only
need stdout, stderr, and an exit status.
