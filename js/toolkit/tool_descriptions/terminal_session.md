Drive an interactive program through a persistent terminal (PTY).
For TUIs, REPLs, pagers, editors, installers, and prompts. Sessions persist
across tool calls for this run and output is continuously consumed between
calls; a running process is normal, and stop sessions when you are done.
{{#if shell}}
`shell` is for ordinary non-interactive commands.
{{/if}}

For `send`, `text` is typed verbatim, including commas and words like `enter`.
It is sent before `keys`, so use `text="echo a, b"`, `keys="enter"` to run a
command containing a comma.

`keys` is comma-separated: named keys are `enter`, `tab`, `shift-tab`, `esc`,
the arrows, `home`, `end`, `pgup`, `pgdn`, `backspace`, `delete`, `ctrl-a` to
`ctrl-z`, and `f1` to `f12`. Prefix a key with `alt-` for an Alt chord
(`alt-x`, `alt-enter`, `alt-left`); a single letter preserves its case.
Anything else is typed verbatim, and `comma` types a comma. Whitespace inside
a token is sent as typed. Use `text` for literal text containing commas.
`cols` and `rows` are accepted with `start` only.

Each result has the rendered screen, cursor position, and process state.
`lines_changed` and `screen_responded` compare against the previous
observation: after `send`, change during the wait window after the keys (not
proof the keys caused it); after `look`, passive change since last time.
{{#if terminal_snapshot}}
`terminal_snapshot` renders the screen as a PNG when text cannot show spacing,
colour, borders, or clipping, and also resets that comparison baseline.
{{/if}}
When js runs with `-C DIR`, the command runs in the same jail as `shell`.
