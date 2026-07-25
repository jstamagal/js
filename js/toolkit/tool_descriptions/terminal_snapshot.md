Render the current screen of a `terminal_session` as a PNG.

Use this after starting and driving a PTY when the visual layout matters: TUI
panes, borders, colours, cursor placement, wrapping, blank screens, or clipped
content. It captures the pyte-rendered screen rather than the desktop.

Parameters:
- `session` (default `main`): existing terminal session to capture.
- `output_path`: optional `.png` path. By default snapshots go under
  `terminal-snapshots/` in the session working directory.
- `wait_ms` (default `100`): time to collect pending terminal redraws first.

Start the session with `terminal_session` before calling this tool. The result
is a normal js image result when vision is enabled and a path/metadata stub
when vision is disabled.

When not to use: use `terminal_session` alone when rendered text and process
state fully answer the question. This tool does not send keys or start commands.
