Drive an interactive program through a persistent terminal (PTY).
For TUIs, REPLs, pagers, editors, installers, and prompts. Sessions persist
across tool calls for this run; a running process is normal, and stop sessions
when you are done.
{{#if shell}}
`shell` is for ordinary non-interactive commands.
{{/if}}

`keys` is comma-separated: named keys are `enter`, `tab`, `esc`, the arrows,
`home`, `end`, `pgup`, `pgdn`, `backspace`, `delete`, `ctrl-c`, `ctrl-d`,
`ctrl-l`, and `f1` to `f12`; anything else is typed verbatim, and `comma` types
a comma. The comma is the only separator, so whitespace inside a token is sent
as typed. `cols` and `rows` are accepted with `start` only.

Each result has the rendered screen, cursor position, and process state.
`lines_changed` and `screen_responded` compare against the previous
observation: after `send`, change caused by the keys; after `look`, passive
change since last time.
{{#if terminal_snapshot}}
`terminal_snapshot` renders the screen as a PNG when text cannot show spacing,
colour, borders, or clipping, and also resets that comparison baseline.
{{/if}}
