Render the current screen of a `terminal_session` as a PNG.
It captures the pyte-rendered terminal, not the desktop, for judging layout,
borders, colours, cursor placement, wrapping, or clipping that text cannot show.
The session must already be started. The result is an image when the model has
vision, otherwise the file path.
