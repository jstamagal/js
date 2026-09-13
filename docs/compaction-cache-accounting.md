# Cached input accounting and repeated summary rewrites

The AI SDK normalizes `Usage.input_tokens` to include cached input.
`cache_read_tokens` and `cache_write_tokens` are breakdowns, not additional
context. Context accounting uses input plus output, then estimates newly
appended tool messages. Adding cache again can nearly double apparent fullness.

A deterministic replay against ffde6e3 uses a 128,000 window, 4,096 buffer,
no output reserve, and replies reporting 64,000 input / 63,000 cached / 500
output tokens. The incorrect total is 127,500 before tool deltas, above the
123,904 input limit. Correct total is 64,500.

With twelve model responses and eleven tool exchanges, historical runtime
summarizes eleven times. Changing only accounting produces zero summaries.
The replay preserves the active user task at index 1, so every unnecessary
summary replaces only the previous summary; all tool exchanges survive. This
explains why counting retained tool bytes does not count summary operations.

Regression coverage lives in `tests/test_context_budget.py` and
`tests/test_provider_boundary_recovery.py`. Exact historical trigger attribution
requires the usage record at each trigger; a deterministic reproduction proves
the mechanism but is not a substitute for those records.
