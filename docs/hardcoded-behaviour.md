# js — hard-coded behaviour (presentation and policy inside parts)

Audit for bead js-2lg.3, made on branch `js-2lg-cfg` (2026-10-20), after js-2lg.2 and js-2lg.4.
It covers every module in `js/`, `js/mcp/` and `js/toolkit/`. The earlier audit of hard-coded
values (paths, numbers, strings) is `~/.js/work/HARDCODED-darkstar.md` on darkstar; this one covers
behaviour: places where a part decides presentation or policy itself instead of taking it from the
program that assembles js.

Facts only. Every row: file and line, function, kind, what the part decides, and the bead that holds
the finding. One bead holds the findings of one file for one seam. Line numbers are those of the
branch the audit was made on; they move as the code changes.

## Seams

- **S1 loop-events**: A part writes to the screen itself. It should emit an event (js/output.py OutputEvent, the run_turn_async event_sink, the compaction emit callback) and a Sink renders it. Related: js-2lg.1.
- **S2 tool-results**: The screen reads tool results from their text. Tools should hand the screen structured results. Related: js-2lg.5.
- **S3 transcript**: The transcript is built from rendered text. It should record events. Related: js-2lg.6.
- **S4 approval**: Allow/deny of a tool call is decided in the part. It should go through the approval gate (events.Approve, run_turn_async approve=) or `on tool_call` hooks. Related: js-2lg.4.
- **S5 settings**: Behaviour is fixed in code. It should be a registered setting (js/settings.py REGISTRY + js/jsrc).
- **S6 tool-policy**: Tool policy is fixed in code. It belongs in tools.yaml / js/toolkit/policy.py rules.
- **S7 prompts**: Model-facing wording is built in code. It belongs in prompts/ or js/toolkit/tool_descriptions/*.md.
- **S8 messages**: User-facing wording is built outside the js/messages.py catalogue.
- **S9 commands-keymap**: A command table, keymap or banner is fixed in code. The assembly should supply it.
- **S10 injectable**: The part decides something its caller cannot pass in as a parameter or callback.

## S1 loop-events

27 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/turn_budget.py:238,264,294` | TurnBudget.recover_overflow/_clear/_summarize | presentation | Passes compaction.printing() as emit, so flight notices go to stderr and not to the turn's event emit. | js-2lg.8 |
| `js/turn_budget.py:268,298,301-302` | TurnBudget._clear/_summarize | presentation | Writes COMPACTION_FAILED and the breaker pause notice straight to stderr. | js-2lg.8 |
| `js/turn_stream.py:128-132,152-157` | StreamSink.reasoning/text | presentation | Without a factory it falls back to StderrReasoning on sys.stderr and a Display on sys.stdout, and always sets the agent speaker label. | js-2lg.12 |
| `js/runtime.py:327` | Telemetry.event | presentation | Writes FLIGHT_LOG_FAILED to stderr. | js-2lg.27 |
| `js/runtime.py:544-552,651-652,768` | _print_trace/ToolTrace.settle/ToolTrace.result | presentation | The loop writes tool exchanges straight to sys.stdout when no exchange factory is set. | js-2lg.27 |
| `js/runtime.py:1936-1970,2353-2354` | _trace_banner/_print_run_line | presentation | Prints the run line (model, provider, ctx, tools) to stdout. | js-2lg.27 |
| `js/runtime.py:2137-2138,2266-2267,2390,2405-2406,2431` | _send_resume_nudge/_take_steer/run_turn_async | presentation | Prints MAX_OUTPUT_RESUMING, STEERED, RETRY_BUDGET_EXHAUSTED, RESPONSE_INCOMPLETE and MAX_ITERATIONS straight to stdout/stderr. | js-2lg.27 |
| `js/cli.py:1234-1267` | _set_model_via_route | presentation | The /model routing and state change prints its own confirmation, so another assembly cannot reuse it without that output. | js-2lg.82 |
| `js/cli.py:1158-1184` | _maybe_auto_compact_async | presentation | The between-turn auto-compaction trigger (status flag, net role, window resolver) is combined with printing its notices. | js-2lg.82 |
| `js/cli.py:3558-3586,4254-4291` | _do_turn, _blocking_repl | policy | The rule to keep a partial turn or discard an unstarted one on ^C or error (marks, orphan healing, requeued notes) is copied twice and combined with print and msgs.say. | js-2lg.82 |
| `js/cli.py:2437` | _run_rc_commands | presentation | Output from the startup jsrc replay is dropped by redirecting stdout into a StringIO. | js-2lg.82 |
| `js/cli.py:2959-2994` | _json_run, _emit_failure | presentation | The headless error event is taken from the last printed stderr line, with ANSI codes and warning marks removed. | js-2lg.82 |
| `js/headless.py:170-180` | watch_output | presentation | The process-global sys.stdout and sys.stderr are replaced so progress can detect silence. | js-2lg.88 |
| `js/screen.py:977-989` | capture_stdio | presentation | Replaces the global sys.stdout and sys.stderr with one proxy, so stderr merges into the scrollback. | js-2lg.104 |
| `js/screen.py:671` | _ScreenStdout.isatty | presentation | Reports every captured stream as a terminal. | js-2lg.104 |
| `js/messages.py:200-218` | for_stream / line_for / say | presentation | Write to the global sys.stdout when no file is passed. | js-2lg.105 |
| `js/messages.py:248-250` | warn | presentation | Always writes to the global sys.stderr. | js-2lg.105 |
| `js/logins.py:567,573` | _parse_store | presentation | Parsing logins.toml prints warnings to stderr itself, and uses the ENV_SETTING_IGNORED message for a login section. | js-2lg.142 |
| `js/codex_auth.py:642,647` | save_refreshed_login | presentation | A background token refresh prints warnings to stderr itself. | js-2lg.151 |
| `js/xai_auth.py:357,362` | save_refreshed_login | presentation | A background token refresh prints warnings to stderr itself. | js-2lg.158 |
| `js/model_metadata.py:528,533` | ensure_fresh_catalog | presentation | Catalog progress and failure lines go through stream_transport.say_or_print, which prints to stderr when no sink is installed. | js-2lg.166 |
| `js/tool_binaries.py:173-179` | warn_urllib_fallback | presentation | The runtime download path prints the aria2 fallback warning to stderr itself. | js-2lg.174 |
| `js/memory.py:365-367` | load_replay_messages | presentation | Prints a skipped-records warning straight to stderr through msgs.warn instead of emitting an event. | js-2lg.180 |
| `js/persona.py:233` | _coerce_sampling | presentation | Prints an ignored-sampling warning to stderr through msgs.warn while loading a manifest. | js-2lg.187 |
| `js/skills.py:462-468` | _warn_once | presentation | Prints skill discovery warnings to stderr itself, once per process. | js-2lg.190 |
| `js/swarm_run.py:247-257` | Runner.compact | presentation | Passes compaction.printing(), so notices go to stderr and the other compaction events are dropped instead of reaching the JSON event stream; failures are swallowed. | js-2lg.197 |
| `js/rename.py:640,660,671,673,675` | migrate_jsrc_file / _migrate_jsrc_file | presentation | Prints the migration notices to stderr by default. | js-2lg.210 |

## S2 tool-results

11 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/runtime.py:597-802` | ToolTrace/_trace_call/_trace_result | presentation | The loop renders tool exchanges, quiet-call folds and live shell output (shell only, at line 680) itself instead of handing structured results to the screen. | js-2lg.28 |
| `js/toolkit/kernel.py:1046,1057,1102,1106,1114,1116,1126,1164-1169,1178,1188-1194` | _died / _busy / kernel | presentation | The tool handler calls the stderr renderers directly instead of handing back structured results. | js-2lg.68 |
| `js/toolkit/toolbox.py:537,544,554-555,587,592-593,599,602-603,620-621` | toolbox | presentation | The tool handler draws its listing and results on stderr through kernel.render_event. | js-2lg.72 |
| `js/headless.py:27-33,39` | _result_text, tool_result_summary | policy | A tool result counts as failed when its text starts with "ERROR". | js-2lg.87 |
| `js/display.py:938,947,1125,1537` | _BODY_ARG / _UNLISTED_ARGS / _KEY_ARGS / _SAID_ARG | presentation | Tables keyed by tool name decide which argument is the body, the key, the hidden one and the "said" text of each built-in tool. | js-2lg.97 |
| `js/display.py:956,958,960,962,963,1049,1052-1081` | tool_output | presentation | Recovers exit code, stdout/stderr, the retry count and the shell envelope by parsing the model-facing result text with regexes. | js-2lg.97 |
| `js/display.py:1039-1045` | result_text | policy | Takes a plain-string result as an error when its text starts with "ERROR". | js-2lg.97 |
| `js/display.py:1408-1425` | call_kind | presentation | Hard-codes shell/kernel/terminal_session/toolbox as "command" and patch/write/notebook_edit as "diff" for TOOLS_DISPLAY 1. | js-2lg.97 |
| `js/display.py:1237-1251` | _lexer | presentation | Picks the syntax highlighter from hard-coded tool names (kernel and toolbox are python, plan is markdown, shell is bash or python). | js-2lg.97 |
| `js/display.py:1483-1502,1505-1532` | _read_view / _diff_view | presentation | Builds the file and diff views by parsing the `N\|` gutter, the paging note and the "patched ... (..., hash ...)" line of the model text. | js-2lg.97 |
| `js/swarm.py:709-878` | bus tool handlers | presentation | Bus tools return only text strings, with no structured result for a screen. | js-2lg.194 |

## S3 transcript

7 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/turn_stream.py:145-149,163-168,191-195` | StreamSink.text/close/_muted_transcript_tee | presentation | Tees answer chunks into the transcript log and mutes the log's stdout tee. | js-2lg.13 |
| `js/runtime.py:538-541,724` | _write_transcript/ToolTrace.result | presentation | Writes rendered tool exchanges into the transcript log. | js-2lg.29 |
| `js/cli.py:677-712,755-768,2833-2870,5052-5053` | _StdoutTee, _enter_transcript_stdio, _transcript_stdio, _run_prompt, _run_args | presentation | The transcript and debug logs are filled by redirecting and teeing sys.stdout/sys.stderr, and one-shot output is hidden or shown the same way. | js-2lg.85 |
| `js/transcript.py:104-114,123` | TranscriptLogSink.write_user / write_assistant / _speaker_locked | presentation | The speaker tags "USER" and "APE" and the IRC "<speaker> " prefix are fixed in code. | js-2lg.133 |
| `js/transcript.py:60-61` | TranscriptLogSink._stamp | presentation | The "[%H:%M] " line timestamp is fixed in code. | js-2lg.133 |
| `js/transcript.py:198-235` | TranscriptTee | presentation | The transcript records by teeing a real stream's text instead of recording events. | js-2lg.133 |
| `js/transcript.py:63-79,143-150` | _write_locked / flush | policy | One failing file write silently disables every transcript file. | js-2lg.133 |

## S4 approval

2 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/toolkit/fs.py:50,101-108,665-686` | remove / _trash_target | policy | Sends a remove to the trash by default and refuses one over a fixed 512 MiB with "confirm with the operator", with no setting or approval gate. | js-2lg.43 |
| `js/cli.py:2083-2086` | _cmd_exec | policy | An `on tool_call` handler run through /exec refuses the tool call when it exits with status 2, so this approval rule lives in a cli command. | js-2lg.84 |

## S5 settings

77 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/turn_call.py:206-219` | ModelCaller.call | policy | Silent-overflow detection (prompt over the window, or 99% full) and history shedding always run; no setting turns them off. | js-2lg.11 |
| `js/turn_call.py:260-276` | ModelCaller.call | policy | On a signed-reasoning rejection it always restarts the thinking turn or drops signed reasoning, then resends. | js-2lg.11 |
| `js/retry.py:57-60` | backoff | policy | The backoff curve (1s doubling to 16s, plus jitter) is fixed in code. | js-2lg.15 |
| `js/compaction.py:245` | MAX_OVERFLOW_ROUNDS | policy | Three overflow recovery rounds per call, fixed in code (also sizes the attempt loop at turn_call.py:186). | js-2lg.17 |
| `js/compaction.py:369` | recover_overflow | policy | Overflow recovery keeps 20 // round tool results instead of COMPACTION_KEEP_TOOL_RESULTS. | js-2lg.17 |
| `js/compaction.py:700-709` | _run_pre_hook | default | COMPACTION_HINT_COMMAND always runs under $SHELL with a fixed 30 s timeout. | js-2lg.17 |
| `js/compaction.py:865` | summarize.summarize_chunk | default | COMPACTION_SUMMARY_TOKENS_MAX is clamped to 8192 in code. | js-2lg.17 |
| `js/compaction.py:1179-1182` | maybe_auto_compact_async | policy | Auto-compaction pauses after 2 consecutive between-turn compactions; the count is fixed in code. | js-2lg.17 |
| `js/model_client.py:1716-1717` | _stream_with_model | default | The DeepSeek wire always gets max_reasoning_tokens 32000 unless effort is none. | js-2lg.24 |
| `js/model_client.py:1724-1725` | _stream_with_model | default | include_reasoning is forced on for Claude models behind OpenAI-shaped proxies. | js-2lg.24 |
| `js/model_client.py:1753-1754` | _stream_with_model | policy | Anthropic prompt caching is always requested. | js-2lg.24 |
| `js/reasoning.py:171-173,178` | anthropic_thinking | default | Effort "none" quietly becomes "low" on models that always think, and adaptive thinking always asks for display "summarized". | js-2lg.25 |
| `js/runtime.py:623-647,2136,2191-2194,2003-2017` | ToolErrorTracker/run_turn_async/_record_tool_error_limit | policy | Three errors from one tool end the turn; the limit is fixed in code and is not a setting. | js-2lg.30 |
| `js/toolkit/core.py:636-661` | ToolContext.require_read | policy | Refuses any edit or overwrite of a file the model has not read, with no setting to turn the gate off. | js-2lg.34 |
| `js/toolkit/fs.py:612-647` | write | policy | Refuses to overwrite an existing file unless overwrite=true and a read or matching hash came first. | js-2lg.44 |
| `js/toolkit/fs.py:1017-1020,1097-1108` | _apply_edit | policy | Waives the read gate for an exact match of 24 characters or more, and applies fuzzy quote, dash and whitespace matches without a setting. | js-2lg.44 |
| `js/toolkit/fs.py:573,587-591` | fs_read | policy | Swaps a repeated read for an "unchanged since read call N" note instead of the lines, with no setting. | js-2lg.44 |
| `js/toolkit/fs.py:1537-1542,1971-1976` | fs_search / ast_search | policy | Returns a memoized result tagged "[deduplicated repeated search]" for a repeated search of a file root. | js-2lg.44 |
| `js/toolkit/fs.py:1565` | fs_search | policy | Always passes `--one-file-system`, so search never crosses a mount point. | js-2lg.44 |
| `js/toolkit/process_net.py:70,81-83` | _shell_argv | policy | Adds `-o pipefail` for bash and zsh with no setting. | js-2lg.47 |
| `js/toolkit/process_net.py:393-399` | shell | policy | Puts the managed tools directory first on the command's PATH. | js-2lg.47 |
| `js/toolkit/process_net.py:426-431` | shell | policy | Never kills a command that outlives SHELL_WAIT; it returns a handle instead. | js-2lg.47 |
| `js/toolkit/search.py:127-129,180-182,222-224,264-266,342` | _key and the search backends | default | API keys come only from fixed environment variables, not from registered settings. | js-2lg.52 |
| `js/toolkit/search.py:481-483,531,546-547` | _obscura_status / browse | policy | Always runs obscura with `--stealth`, and adds `--allow-private-network` by itself for a private IP literal. | js-2lg.52 |
| `js/toolkit/terminal.py:285-291` | terminal_session | policy | Runs `$SHELL` or /bin/sh instead of SHELL_PROGRAM, and passes the full environment when no jail is active. | js-2lg.54 |
| `js/toolkit/browser.py:24,202-210` | _launch_browser | policy | Falls back to a fixed Chrome-for-Testing path when the Playwright launch fails. | js-2lg.57 |
| `js/toolkit/meta.py:320-332` | _run_one_task_async | policy | Caps each child's reply to an equal share of TOOL_RESULT_MAX. | js-2lg.60 |
| `js/toolkit/jev.py:17,106-107` | jev | default | The API key comes only from the TYPESAFE_API_KEY environment variable, not from a registered setting. | js-2lg.65 |
| `js/toolkit/kernel.py:1081,1093,1173` | kernel | default | Interrupts a cell after a default of 120 s that exists only in code. | js-2lg.70 |
| `js/toolkit/toolbox.py:180-202,530-533` | seed_examples / toolbox | policy | Copies the shipped example tools into the global toolbox on list and load. | js-2lg.71 |
| `js/toolkit/toolbox.py:578-588` | toolbox | policy | Refuses to save a tool whose body has unbound names. | js-2lg.71 |
| `js/toolkit/lsp.py:564-570` | _server_env | policy | Gives a language server the full environment when no jail is active. | js-2lg.74 |
| `js/toolkit/lsp.py:121-145` | workspace_root | policy | Never lets the home directory, its parents or / be a workspace root. | js-2lg.74 |
| `js/toolkit/notebook.py:231-233` | notebook_edit | policy | Refuses to edit a cell unless the notebook was read first. | js-2lg.77 |
| `js/toolkit/notebook.py:273-276` | notebook_edit | policy | Clears a code cell's outputs and execution count whenever its source is replaced. | js-2lg.77 |
| `js/cli.py:1270-1294` | _pick_model_into_state | policy | A model picked in the picker is always written to the global jsrc as the default model. | js-2lg.83 |
| `js/cli.py:4489-4493` | _main | policy | Every start auto-migrates old home locations, creates the home layout and user jsrc, and cleans tmp. | js-2lg.83 |
| `js/cli.py:4760-4766,4861,4904` | _run_args | policy | The flags are applied, and the run mode is published, by writing os.environ (JS_INLINE_CODE, ENVCTX_HIST, JS_MODE) instead of settings. | js-2lg.83 |
| `js/cli.py:4836-4854` | _run_args | policy | A plain invocation runs the benchmark when the agent's directory has benchmark files. | js-2lg.83 |
| `js/cli.py:3181-3214,3238-3254` | _prune_commit_backups, _snapshot_worktree, _run_commit | policy | The commit run runs `git init` in the target when needed, snapshots the worktree, and keeps only 10 backups. | js-2lg.83 |
| `js/cli.py:3466-3482` | _warn_missing_binaries | policy | A fixed list of external binaries is checked and warned about at every real run. | js-2lg.83 |
| `js/loop_controller.py:112,115,135,138` | run | policy | The retry policy (3x attempts cap, stop after 5 consecutive failures, backoff capped at 8s) is fixed. | js-2lg.91 |
| `js/display.py:968,969,970,972` | MARK / OK / FAIL / RAIL | presentation | The exchange glyphs are fixed constants, not /fset or theme entries. | js-2lg.99 |
| `js/display.py:541,1178,1202,1186,1187,1205,1519` | _FoldingTable / key_argument / _single_line / _diff_view | presentation | The table rule, newline mark, ellipsis and hunk gap are fixed glyphs, not taken from the theme. | js-2lg.99 |
| `js/display.py:955,1539` | _ARG_VALUE_CHARS / LIVE_LINES | default | The argument cut at 40 columns and 6 live output rows of a running command exist only in code. | js-2lg.99 |
| `js/display.py:497-513,1300-1301,1309` | _markdown_styles / _Block.header | presentation | Text attributes (underline h2, bold h3, italic h4, bold header and key argument) are fixed in code; only the colours come from the theme. | js-2lg.99 |
| `js/display.py:603-608` | render_markdown | presentation | Pads inline code with a space on each side and turns every soft break into a hard break. | js-2lg.99 |
| `js/screen.py:42,114` | THROBBER / throbber_frame | presentation | The spinner glyphs are a fixed braille string, not a theme entry. | js-2lg.100 |
| `js/screen.py:41,316` | SCROLLBACK_LINES / Scrollback._set_text | default | The scrollback keeps 5000 lines, set only in code. | js-2lg.100 |
| `js/replcomplete.py:97-110` | hunspell_suggest | default | Spellcheck language en_US and a 2 s timeout exist only in code. | js-2lg.117 |
| `js/exline.py:48` | append_note | presentation | The note timestamp format is fixed in code. | js-2lg.126 |
| `js/clipimage.py:81-97` | clipboard_command | default | The fallback chain wl-paste, then xclip, then kitten when PASTE_IMAGE_COMMAND is unset is fixed in code. | js-2lg.127 |
| `js/clipimage.py:34` | READ_TIMEOUT_S | default | The clipboard read timeout of 5 s exists only in code. | js-2lg.127 |
| `js/config.py:565,585` | from_env | default | The instruction files a run loads (JS.md, JS.local.md, AGENTS.md, AGENTS.local.md) are fixed in code and are not a setting. | js-2lg.136 |
| `js/settings.py:909` | load_jsrc_files | policy | Every settings load rewrites old-name jsrc files in place (rename.migrate_jsrc_file), with a backup and a line on stderr; this auto-migration is not a setting and has no callback. | js-2lg.140 |
| `js/login_cli.py:441-443` | _collect_api_login | policy | A keyless local endpoint gets the placeholder key "x" automatically. | js-2lg.148 |
| `js/codex_provider.py:479,489` | _build_body_async | default | text.verbosity "low" and reasoning summary "detailed" are fixed in code. | js-2lg.154 |
| `js/codex_provider.py:59-60,108-109` | _codex_headers | default | The originator "pi" and the User-Agent "js/0.1.0" are fixed in code; HTTP_USER_AGENT does not apply here. | js-2lg.154 |
| `js/codex_provider.py:44,47-58` | _client_version | default | JS_CODEX_CLIENT_VERSION is an env-only setting, and otherwise the part runs `codex --version` as a subprocess. | js-2lg.154 |
| `js/providers.py:135` | _BUILTINS (deepseek) | default | DeepSeek's default reasoning effort "xhigh" is fixed in the registry and is not a setting. | js-2lg.161 |
| `js/model_metadata.py:33,517-539` | ensure_fresh_catalog | policy | The models.dev catalog refreshes itself over the network when older than 8 hours, with one try per process; neither the age nor the auto-refresh is a setting. | js-2lg.164 |
| `js/model_metadata.py:335-368` | probe_local_context_window | policy | Local servers get HTTP probes for their context window automatically, with no setting to turn it off. | js-2lg.164 |
| `js/endpoint_uri.py:34,135,141` | parse | default | A spec with no key gets "sk-dummy-key", and the default wire shape is openai-completions; both are fixed in code. | js-2lg.168 |
| `js/dotenv.py:52-63,80` | candidate_files / load | policy | .env files are read from the start directory up to the filesystem root and fill os.environ; the walk-up is fixed in code. | js-2lg.169 |
| `js/home.py:89,652-673` | sweep_tmp | default | The ~/.js/tmp auto-clean age (24 hours) is fixed in code and is not a setting. | js-2lg.171 |
| `js/tool_binaries.py:161-166` | resolve_binary | policy | It falls back to PATH when the managed binary is missing; the fallback is fixed in code. | js-2lg.175 |
| `js/tool_binaries.py:206-209` | aria2_argv | policy | The aria2c retry and split policy (--max-tries=5, --split=8) is fixed in code. | js-2lg.175 |
| `js/tool_binaries.py:441,445,707,710` | _download / install_all.download | default | The User-Agent "js-tool-installer/0.1" is fixed in code and ignores HTTP_USER_AGENT. | js-2lg.175 |
| `js/session_query.py:43-44,160-175,404-410` | kind / visible | policy | Decides which sessions the picker hides (quick, empty, script, subagent), with the thresholds fixed in code. | js-2lg.181 |
| `js/session_query.py:603-605,636-656` | preview | policy | Treats a line that starts with the fixed pin mark as the agent's status line for the preview. | js-2lg.181 |
| `js/session_text.py:511-555` | refresh | policy | Always rewrites a .txt transcript and the search index after every session write, with no setting to turn it off. | js-2lg.185 |
| `js/swarm.py:65,691-695` | Agent.spawn | policy | Caps spawned agents per bus at a fixed SPAWN_CAP of 8 that is not a setting. | js-2lg.192 |
| `js/hookexec.py:66` | run | default | Runs /exec and on-handler commands under $SHELL or /bin/sh and ignores the SHELL_PROGRAM setting. | js-2lg.201 |
| `js/jail.py:47-50,336-356` | hidden_roots | policy | Always hides /home, the operator's home, /run/user and every network filesystem, with no setting. | js-2lg.205 |
| `js/jail.py:879-893,910` | _clear_stale_private_dirs / enter | policy | Deletes the private directories of dead js processes automatically on every jail entry. | js-2lg.205 |
| `js/rename.py:633-676` | migrate_jsrc_file | policy | Rewrites and backs up each loaded jsrc automatically (called from settings.py:909), with no setting to turn it off. | js-2lg.211 |
| `js/tool_args.py:55-90,118-209` | repair_jsonish / coerce_to_schema | policy | Always repairs malformed tool-call JSON and coerces string arguments to the schema type, with no strict mode. | js-2lg.214 |

## S6 tool-policy

2 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/cli.py:131-143` | _registry_for | policy | cli decides that the task tool's model parameter exists only when the subagent model lock is off. | js-2lg.80 |
| `js/agent_migration.py:108` | _convert_entry | policy | A bare tools entry is rewritten as NAME:eager, so the modifier is fixed in code. | js-2lg.178 |

## S7 prompts

72 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/compaction.py:247,288` | microcompact | model-facing | The text that replaces a cleared tool result, "[old tool result cleared]", is in code. | js-2lg.18 |
| `js/compaction.py:548,556-563` | _post_compact_rehydration | model-facing | The post-compaction-files message and the "too large to re-attach" line are written in code. | js-2lg.18 |
| `js/compaction.py:680-687,737,747,771-791,809-836,917-918` | _summary_prompt/serialize_conversation/_clip/_part_text/_compaction_summary_message | model-facing | The summarizer prompt, its six headings, the transcript role labels, the clip marker and the summary wrapper are in code. | js-2lg.18 |
| `js/model_client.py:922,932,991,994,1002,1007,1010,1013,1031` | _image_result_payload/_visual_read_error/_mixed_result_parts/media_label | model-facing | Tool-result error lines and media/resource labels the model reads are built in code. | js-2lg.22 |
| `js/runtime.py:286-289,1897` | MAX_OUTPUT_RESUME_NUDGE/_send_resume_nudge | model-facing | The resume nudge sent after a cut-off reply is in code. | js-2lg.26 |
| `js/runtime.py:392,414,1158,1189,1291,1328,1404,1427,1443,1465` | _normalize_tool_call_batch/_dispatch/_dispatch_tool_calls/_dispatch_fan_out_async/_dispatch_async_tool | model-facing | Tool error lines the model reads (parse failure, invalid arguments, "ERROR running", sibling-load refusal) are built in code. | js-2lg.26 |
| `js/runtime.py:505-512,1771-1774` | _truncated_tool_call_notice/_record_assistant | model-facing | The truncated-tool-call notice is built in code and streamed to the screen as reply text. | js-2lg.26 |
| `js/runtime.py:526` | _history_tool_result_message | model-facing | The fallback stub "VISUAL_FILE (image omitted from history)" is in code. | js-2lg.26 |
| `js/runtime.py:643,2013` | ToolErrorTracker.record/_record_tool_error_limit | model-facing | The <retry>attempts_left note and the retry-limit failure message are in code. | js-2lg.26 |
| `js/runtime.py:740-743,801,810,813,853-854,888-891,953,971,1012-1022,1101` | _media_notice/_dehydrate_capped_result/_cap_media/_cap_image_marker/_cap_result/spill_oversized_result/_cap_batch_results | model-facing | Media-dropped notices, truncation markers and the spill notice are built in code. | js-2lg.26 |
| `js/runtime.py:1802-1809` | _record_assistant | model-facing | The REPLY_TOOL_CALLS_MAX batch-limit diagnostic is built in code. | js-2lg.26 |
| `js/toolkit/core.py:647,651-652,657-660` | ToolContext.require_read | policy | Builds the model-facing read-before-write refusal text in code. | js-2lg.35 |
| `js/toolkit/core.py:948,957,964,1082,1134-1137` | ToolContext._persist_snapshot / snapshot / call_tool | policy | Builds undo-snapshot WARNING text in code and appends it to the model's tool result. | js-2lg.35 |
| `js/toolkit/core.py:315-335` | ToolResult.dehydrated | policy | Fixes the history placeholder wording for omitted image, audio and resource blocks. | js-2lg.35 |
| `js/toolkit/registry.py:59-68` | ToolRegistry.unavailable_error | policy | Builds the model-facing unknown-tool and policy-denied error text, including the "Did you mean" hint and the tool list. | js-2lg.36 |
| `js/toolkit/registry.py:119-128` | ToolRegistry.with_kernel | policy | Fixes which tools the kernel gate removes, and the refusal text that names the `/set PYTHON_KERNEL` REPL command. | js-2lg.36 |
| `js/toolkit/registry.py:270-275,280-282` | TurnToolSurface._unavailable_errors | policy | Builds the model-facing "not in the published tool set, load it with tool_discovery" error text. | js-2lg.36 |
| `js/toolkit/registry.py:362-388` | TurnToolSurface.offer_path_skills | policy | Decides on its own to offer a skill once per session and writes the <js-reminder> text the model reads. | js-2lg.36 |
| `js/toolkit/registry.py:423,445,469,473,475` | TurnToolSurface.discover_async / discover / _load | policy | Builds the discovery error strings in code. | js-2lg.36 |
| `js/toolkit/policy.py:292-295` | argument_refusal | policy | Builds the model-facing ban-pattern refusal text in code. | js-2lg.40 |
| `js/toolkit/discovery.py:202,230` | catalog_result | policy | Builds the model-facing error and hint text in code. | js-2lg.42 |
| `js/toolkit/discovery.py:37-41` | discovery_tool | policy | Parameter descriptions the model reads are written in the code schema. | js-2lg.42 |
| `js/toolkit/fs.py:619-622,637-638` | write | policy | Builds the model-facing overwrite refusal text in code. | js-2lg.45 |
| `js/toolkit/fs.py:205-221` | _changed_since_read | policy | Builds the changed-on-disk refusal and retry guidance the model reads. | js-2lg.45 |
| `js/toolkit/fs.py:798-814,1069,1071,1093-1096` | _nearest_hint / _apply_edit | policy | Builds the model-facing miss, ambiguity and closest-line hint text in code. | js-2lg.45 |
| `js/toolkit/fs.py:531-538` | fs_read | policy | Builds the READ_BYTES_MAX refusal with an example call in code. | js-2lg.45 |
| `js/toolkit/fs.py:1288,1295-1297` | module constants | policy | The missing-binary messages tell the model to run `just install-tool-binaries`. | js-2lg.45 |
| `js/toolkit/fs.py:2136-2260` | tools | policy | Parameter descriptions the model reads are written in the code schemas. | js-2lg.45 |
| `js/toolkit/process_net.py:317-325,343-349` | _render_finished / _render_running | policy | Builds the filtered-environment note and the poll/wait/kill guidance the model reads. | js-2lg.48 |
| `js/toolkit/process_net.py:500-504,885,888` | _inline_read_limit_error / _fetch_file_url | policy | Builds the inline-limit refusal and "use save=" guidance in code. | js-2lg.48 |
| `js/toolkit/process_net.py:1053-1087` | tools | policy | Parameter descriptions the model reads are written in the code schemas. | js-2lg.48 |
| `js/toolkit/search.py:586-590,602` | browse | policy | Builds the screenshot and HTTP-error guidance the model reads. | js-2lg.53 |
| `js/toolkit/terminal.py:165-174` | _observe | policy | Builds the "reading" guidance paragraph the model reads. | js-2lg.55 |
| `js/toolkit/terminal.py:210-211,372` | _jail_refusal / _draw_screen | policy | Builds the restart and install-hint text (`uv sync --extra vision`) in code. | js-2lg.55 |
| `js/toolkit/browser.py:93-95,251-254` | _frame_metrics / browser_probe | policy | Builds the install-hint text in code. | js-2lg.58 |
| `js/toolkit/browser.py:412-422` | browser_probe | policy | Builds the "reading" guidance paragraph the model reads. | js-2lg.58 |
| `js/toolkit/meta.py:670-673` | named_agent_tool | policy | Writes the fallback model-facing description of an agent tool in code. | js-2lg.62 |
| `js/toolkit/meta.py:678,685-694,700-701` | named_agent_tool / _task_params / tools | policy | Parameter descriptions the model reads are written in the code schemas. | js-2lg.62 |
| `js/toolkit/task_jobs.py:220-230,237-255` | started_text / report | policy | Builds the background-task handle text and guidance the model reads. | js-2lg.63 |
| `js/toolkit/task_jobs.py:277-299` | completion_notes | policy | Builds the <js-reminder> completion notes the model reads. | js-2lg.63 |
| `js/toolkit/jev.py:113,127` | jev | policy | Builds the install hint (`just sync`) and the order-check verdict text in code. | js-2lg.66 |
| `js/toolkit/kernel.py:65-72,640,1091` | MISSING_DEPS / get_session / kernel | policy | Model-facing errors name `/set PYTHON_KERNEL on`, `uv sync` and `just` commands. | js-2lg.69 |
| `js/toolkit/kernel.py:1041-1045,1052-1056,1152-1153,1200-1201,1215-1216` | _died / _busy / kernel | policy | Builds the restart, busy, interrupted and poll guidance the model reads. | js-2lg.69 |
| `js/toolkit/toolbox.py:519,566-567,573-574,581-586` | toolbox | policy | Builds refusal and guidance text in code, including the `/set` command. | js-2lg.73 |
| `js/toolkit/lsp.py:608,779-780` | server_for / lsp | policy | Builds the jail hint and the "Call again" text the model reads. | js-2lg.76 |
| `js/cli.py:151,157-172,175-187,190-197,204-219,2693-2700,2726-2730,3656-3657,4201-4202,5000-5007,5037-5038` | _note_mode_switch, _note_model_switch, _run_prompt, _run_repl_turn, _blocking_repl, _run_args | policy | The prompt-changed, mode-switch, model-switch and cut-off reminders are worded in code, and the logic that adds them to the user message is inside cli turn paths. | js-2lg.78 |
| `js/cli.py:2113-2115,2198,2218,2239` | _cmd_cd, _cmd_add, _cmd_drop | policy | The working-directory and jail-bind reminders the model reads are worded in code and queued automatically. | js-2lg.78 |
| `js/cli.py:1450-1475,1975` | _append_closing_note, _cmd_quit | policy | The /quit closing reminder is worded in code, and the rule that merges it into the last user message is fixed in cli. | js-2lg.78 |
| `js/cli.py:3266-3286` | _run_commit | policy | The commit agent's whole task prompt and helper instructions are built in code. | js-2lg.78 |
| `js/loop_controller.py:14,107-109` | run | policy | The model-facing default prompt for loop children is fixed in code. | js-2lg.89 |
| `js/attach.py:311-315` | _prepare_bytes | presentation | Builds the model-facing "ATTACHED_BINARY_FILE ... (content not inlined)" text in code. | js-2lg.131 |
| `js/attach.py:318-322` | _prepare_image | presentation | Builds the model-facing "VISUAL_FILE ... (vision disabled; image bytes not sent)" text in code. | js-2lg.131 |
| `js/attach.py:364-369` | _format_text_attachment | presentation | Builds the model-facing "Attached file: ... [truncated to N bytes]" header and fence in code. | js-2lg.131 |
| `js/login_cli.py:39,519,533` | _run_secondary_test | presentation | The model-facing test prompt "1+1=" is fixed in code. | js-2lg.147 |
| `js/codex_provider.py:62,329` | _repair_tool_pairs | presentation | The model-facing text for an interrupted tool output is fixed in code. | js-2lg.152 |
| `js/codex_provider.py:319` | _repair_tool_pairs | presentation | The model-facing "[Previous tool result; call_id=...]" wrapper for an orphaned result is built in code. | js-2lg.152 |
| `js/codex_provider.py:283` | _file_part_to_input | presentation | The model-facing "[Unsupported file for Codex: ...]" text is built in code. | js-2lg.152 |
| `js/memory.py:195-200` | _heal_orphaned_tool_calls | policy | Backfills a missing tool result with a fixed English error text that the model reads. | js-2lg.179 |
| `js/memory.py:244-248` | restart_thinking_turn | policy | Builds a fixed "Continue from this previous assistant context..." user message for the model in code. | js-2lg.179 |
| `js/skills.py:86-96` | LoadedSkill.render | policy | Builds the "Skill tool requirements unavailable / policy-denied / unknown" text that the model reads. | js-2lg.188 |
| `js/skills.py:248-255` | expand_user_invocation | policy | Builds the model-facing skill frame text ("Base directory for this skill", "load them with tool_discovery") in code. | js-2lg.188 |
| `js/swarm.py:481-492,664,347,380` | render / Agent._tick / Room.post_cell / Room.finish_cell | policy | Builds the inbox frame, the wake-alarm text and the task/done broadcast text that the model reads. | js-2lg.193 |
| `js/swarm.py:495-525` | roster / board | presentation | Fixes the member-list and work-board wording and layout that both the model and the operator read. | js-2lg.193 |
| `js/swarm.py:709-878` | _send / _who / _claim / _release / _subscribe / _wake_me / _retire / _recruit / _post_task / _take_task / _finish_task / _tasks | policy | Bus tools return model-facing result, refusal and nudge text built in code, for example "Stop calling tools to sleep". | js-2lg.193 |
| `js/swarm.py:893-932` | tools | policy | Writes the bus tools' parameter descriptions in code, not in tool_descriptions/*.md. | js-2lg.193 |
| `js/events.py:286-287` | _failed | policy | Builds the model-facing refusal text for a failed approval in code. | js-2lg.200 |
| `js/hookexec.py:192` | refusal | policy | Builds the default model-facing refusal text "an on tool_call handler refused ..." in code. | js-2lg.202 |
| `js/jail.py:517,535-547` | Jail.confine | policy | Builds the refusal text the model reads ("outside the jail", "hidden in the jail", "read-only in the jail") in code. | js-2lg.204 |
| `js/capped_process.py:462-463` | truncation_marker | policy | Builds the model-facing "[truncated: ... reached]" text in code. | js-2lg.213 |
| `js/mcp/host.py:306-331` | MCPHost.CONTROL_TOOLS | policy | Writes the MCP control tool descriptions and usage nudges in code, not in tool_descriptions/*.md. | js-2lg.215 |
| `js/mcp/host.py:475,496,502,675-677,749,754-755,810` | MCPHost.discover / _index_server_tools / _regroup_tools / _make_tool | policy | Builds the catalog entry text the model reads (server error, connected server, collision notes, default "MCP tool") in code. | js-2lg.215 |
| `js/mcp/host.py:244,252,271,793` | _safe_content_block / resource_result / _make_tool | policy | Builds "[... content suppressed]" and "MCP tool ... is no longer available" text for the model in code. | js-2lg.215 |

## S8 messages

53 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/turn_budget.py:295` | TurnBudget._summarize | presentation | The network role label "Compacting" is English text in code, and stream_transport.py:238 matches on it to pick the wording. | js-2lg.9 |
| `js/compaction.py:378,380-381,400,411-412,1004` | recover_overflow/clear_for_budget/compact_now | user-facing | Flight notice and result text that reaches the user is English built in code. | js-2lg.20 |
| `js/compaction.py:898,901` | summarize.summarize_chunk | user-facing | Summary failure errors are worded in code. | js-2lg.20 |
| `js/model_client.py:79,474` | StreamIdleError/_CheckedResponsesStream | user-facing | Error texts are worded in code. | js-2lg.23 |
| `js/sampling.py:130,134,136,142,148,157,160` | _coerce_float/_coerce_int | user-facing | Sampling validation errors are worded in code. | js-2lg.32 |
| `js/cli.py:2399` | _event_dispatcher | presentation | The text "unsupported event handler command" is built in code and does not come from messages.py. | js-2lg.79 |
| `js/cli.py:913,918-922,928-930,944-948,996-997` | _live_settings_overlay, _live_settings_overlay_for_state | presentation | The source labels ("login X", "session", "provider default", "env", "agent sampling", "unset") are fixed strings in code. | js-2lg.79 |
| `js/loop_controller.py:25,116,119,136,139,144` | parse_limit, run | presentation | The loop prints its own banner and stop/retry lines with print(), worded in code. | js-2lg.90 |
| `js/promptexpand.py:180-184,254,279` | expand_prompt._resolve, _run_subsystem, _sub_file | presentation | Some directive error texts are built in code and do not come from messages.py. | js-2lg.93 |
| `js/setcmd.py:40-45,72-90,221,279-281` | _empty_display, render_value, apply_set, apply_unset | presentation | Value display words ("on", "off", "<unset>", "<none>", "<set>") are fixed in code. | js-2lg.94 |
| `js/setcmd.py:399` | on_command | presentation | The confirmation line for a registered handler is built in code. | js-2lg.94 |
| `js/display.py:1209-1234` | _other_arguments | presentation | Special-cases read/write/patch and builds "bytes A-B", "lines A-B", "N lines" and "N edits" in code. | js-2lg.98 |
| `js/display.py:1088-1119` | metrics / _size / _count | presentation | Builds the footer wording ("failed · L of N tries left", "exit N", "N lines", "N shown", B/KB/MB) and English plurals in code, not from js/messages.py. | js-2lg.98 |
| `js/display.py:1429-1440,1444-1459,1468` | _FOLD_NOUN / fold_label / render_fold | presentation | Fold rows use English nouns per tool in code ("files", "searches", "pages", "looks"), a special case for git commands and a 24-character label cut. | js-2lg.98 |
| `js/display.py:1587` | _finished_brief | presentation | Builds the "exit N" tail of a failed command in code. | js-2lg.98 |
| `js/screen.py:117-131` | turn_centre | presentation | Builds the bar's tool phase "name +N Ns" and the byte count "NB" in code, not through js/messages.py. | js-2lg.103 |
| `js/messages.py:276-278` | plural | presentation | English pluralising is code, not a catalogue entry. | js-2lg.108 |
| `js/keys.py:48-61` | ACTIONS | presentation | Each action's user-facing description is English in code, not in js/messages.py. | js-2lg.120 |
| `js/keys.py:181-188` | describe | presentation | Builds key names ("Ctrl-X", "Esc") in code. | js-2lg.120 |
| `js/pastes.py:31-35` | marker | presentation | The "[paste #N +X lines]" and "[paste #N X chars]" wording is built in code. | js-2lg.121 |
| `js/clipimage.py:54-55` | placeholder | presentation | The "[image #N]" placeholder wording is built in code. | js-2lg.128 |
| `js/config.py:44` | validate_agent_id | presentation | The agent id refusal text is built in code instead of coming from js/messages.py. | js-2lg.134 |
| `js/config.py:263,266,273,277,289,299,301,307` | resolve_session_file | presentation | The session path refusals are f-strings in code instead of js/messages.py entries. | js-2lg.134 |
| `js/config.py:321` | _select_prompt_dir | presentation | The text for an unreadable agent directory is built in code. | js-2lg.134 |
| `js/settings.py:351,420` | _coerce_value | presentation | The EFFORT refusal ("expected ...") is built in code instead of from js/messages.py. | js-2lg.137 |
| `js/settings.py:454,456` | _coerce_value | presentation | The PASTE_IMAGE_COMMAND refusals are literal strings in code. | js-2lg.137 |
| `js/settings.py:579,641` | parse_header_lines / _parse_dotted_key | presentation | The header and empty-key error texts are built in code. | js-2lg.137 |
| `js/login_defaults.py:57,60` | parse | presentation | The refusal for a bad NAME=value entry is built in code. | js-2lg.141 |
| `js/logins.py:177,213` | hold_login_lock / ahold_login_lock | presentation | The store-busy error text is built in code. | js-2lg.143 |
| `js/logins.py:443-444` | _refuse_if_corrupt | presentation | The corrupt-file refusal, including the instruction "Fix or remove the file by hand", is built in code. | js-2lg.143 |
| `js/logins.py:473,504,509,514,517,620,910` | _optional_str / _login_from_section / _read_store / _read_model_cache_records | presentation | The bad-section and parse errors the user sees are built in code. | js-2lg.143 |
| `js/login_cli.py:105-116,334,345,633,324` | _curses_picker / _select_provider / _select_api_shape / _provider_details / _login_provider_rows | presentation | The picker row layout ("> ", "[x] ", padded id column, "[saved]", "saved/env/registry" tags) is fixed in code. | js-2lg.145 |
| `js/login_cli.py:32-37,299` | _API_SHAPES / _menu | presentation | The API shape descriptions and the NoTerminalError text are user-facing strings in code. | js-2lg.145 |
| `js/codex_auth.py:318,320` | _CallbackHandler.do_GET | presentation | The HTML page shown after login is fixed in code. | js-2lg.150 |
| `js/codex_auth.py:113-125,173,176,363,427-431,447-493,593,602,654,683,686,716,809` | _token_from_response / _post_token_form / login_browser / login_device / refreshed_login / relogin_error / ensure_fresh_login / _locked_refresh | presentation | The OAuth errors, including "run js --login openai-codex again" and "fix it before ...", are built in code. | js-2lg.150 |
| `js/codex_provider.py:84,559` | _account_id_from_token / OpenAICodexProvider.__init__ | presentation | The errors, including "run js --login openai-codex", are built in code. | js-2lg.155 |
| `js/xai_auth.py:273-274` | login_browser | presentation | The HTML page shown after login is fixed in code. | js-2lg.157 |
| `js/xai_auth.py:87,92,104-110,174-183,243,368,445,452,457` | _validate_endpoint / discover / _post_token_form / exchange_code_for_token / relogin_error / ensure_fresh_login | presentation | The OAuth errors, including "run js --login xai-oauth again", are built in code. | js-2lg.157 |
| `js/oauth_loopback.py:119,121,157,159,161,163` | wait_for_code | presentation | The callback errors are built in code from the label. | js-2lg.159 |
| `js/providers.py:731-740` | assert_endpoint_configured | presentation | The refusal text, including "Run `js --login ...`", is built in code. | js-2lg.162 |
| `js/endpoint_uri.py:76,83-86,89,91-93,106,109,113,116-119,122,131,133` | parse | presentation | The -u spec errors are worded for a terminal in code. | js-2lg.167 |
| `js/tool_binaries.py:145,550-551,568,573-574,583-585,622,627,632,634,698-699,730` | aria2_release / release_plan / _provision_termux_tools / install_all | presentation | The missing-tool and refusal texts are built in code. | js-2lg.177 |
| `js/session_query.py:545-547` | header_line | presentation | Builds the picker column header words ("when mode agent dir turns length name") inline, not from messages.py. | js-2lg.182 |
| `js/persona.py:79,114,118,125,133,140,145,155,158,168,172,199,203,214,222,226-227,230,336-337,353-354,363` | _load_exchanges / _load_yaml_manifest / _refuse_legacy_manifest / _coerce_* / _split_frontmatter / load_agent_manifest / load_prompt_spec | presentation | Builds manifest and prompt-directory error text inline instead of taking it from messages.py. | js-2lg.186 |
| `js/skills.py:400,412,416,476,488,496,501,504,518,561` | _split_frontmatter / _*_field / _validate_name | presentation | Builds frontmatter error text inline, and that text is shown in skill-skipped warnings. | js-2lg.191 |
| `js/swarm.py:78,85,692,695` | check_name / check_kind / Agent.spawn | presentation | Builds validation and refusal error text inline. | js-2lg.195 |
| `js/swarm.py:975,977,981,985` | main | presentation | Prints the "sent #N", "posted #N", roster and board output directly, with the wording built inline. | js-2lg.195 |
| `js/swarm_run.py:165,168,268,271,274,289` | Runner.recruiter / load_spec / main | presentation | Builds error text inline and prints "swarm run: ..." to stderr. | js-2lg.198 |
| `js/events.py:131,134,149,203` | EventHooks.add / EventHooks.emit / RefusableOnly.emit | presentation | Builds "unknown event" and "on needs a handler" error text inline. | js-2lg.199 |
| `js/agent_lock.py:31` | acquire | presentation | Builds the Busy error text inline. | js-2lg.203 |
| `js/jail.py:87,90,97,107,114,904,906,909,917,923` | parse_bind / parse_binds / parse_hide / parse_hides / enter | presentation | Builds setting and -C error text inline instead of taking it from messages.py. | js-2lg.207 |
| `js/mcp_config.py:91,102,104,112,118,125-280` | _pairs_object / _json_object / _string_map / _string_list / parse_servers / parse_agents / _request_timeout | presentation | Builds every MCP configuration error message inline. | js-2lg.208 |
| `js/rename.py:544,583` | migrate_jsrc_text | presentation | Writes the fixed comment wording "# js: not migrated" into the operator's jsrc. | js-2lg.212 |

## S9 commands-keymap

24 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/toolkit/meta.py:70,74` | skill | policy | Model-facing refusal text names the `/skill` REPL command. | js-2lg.61 |
| `js/cli.py:1510-1521,2245-2295` | Command, COMMANDS | policy | The REPL slash-command table is a fixed module-level dict. | js-2lg.86 |
| `js/cli.py:2318-2319,3783-3789,3922-3924` | _run_command, _is_turn_state_command, _repl_main.on_line | policy | cli refuses turn-state commands while a turn runs. | js-2lg.86 |
| `js/cli.py:2338,2353-2368,2373-2381` | QUIT_WORDS, _handle_command, _is_unknown_command | policy | The quit words, and the rule that an unknown command-shaped slash word is refused instead of sent to the model, are fixed. | js-2lg.86 |
| `js/cli.py:1950` | _cmd_compact | policy | The focus text "up to here" is a fixed magic phrase that forces compaction. | js-2lg.86 |
| `js/cli.py:3727-3744` | _startup_banner | presentation | The startup banner layout (MOTD, title, agent, model, prompt, memory, hint, notices) is fixed. | js-2lg.86 |
| `js/cli.py:3915-3920,3939-3950` | _repl_main.on_line, _repl_main.on_interrupt | policy | "/flush" and "/cancel queued" are fixed input words, and ^C always cancels the turn and drops queued input. | js-2lg.86 |
| `js/cli.py:4000-4004,4109-4128` | _repl_main, _repl_prompt_session | policy | The clipimage and paste key bindings and the prompt-session behaviour (search case, completion style, suspend, erase) are fixed. | js-2lg.86 |
| `js/cli.py:4160-4169` | _blocking_repl | policy | A second ^C at an idle prompt ends the blocking REPL; this rule is fixed. | js-2lg.86 |
| `js/screen.py:47,811-855` | build_app (_arm, _arm_or_quit, _ctrl_c, _ctrl_d) | policy | Fixes what interrupt and eof do (clear the line, then quit on a second press within 2.0 s, Ctrl-D deletes inside a line). The window is not a setting. | js-2lg.101 |
| `js/screen.py:879-893` | build_app | policy | The action-to-handler table is fixed. A keymap can only move keys between js's 13 actions; an assembly cannot add an action or change one. | js-2lg.101 |
| `js/messages.py:419,700,755,805-808,1157,1172-1173,1181,1191,1203,1206` | TURN_RUNNING / PRESS_CTRL_C_AGAIN / STARTUP_HINT / PICKER_KEYS* / PICK_HELP / SKILLS_KEYS / SESSIONS_*KEYS | presentation | Key hints spell fixed keys (^C, Ctrl-D, arrows/jk, esc, q, v, b, i, a, f) and do not follow the keymap. | js-2lg.107 |
| `js/picker.py:246-294` | ModelPicker.application | policy | Fixed keymap (up/k, down/j, pageup/pagedown, tab/left/right, enter, escape/q/c-c, f). | js-2lg.109 |
| `js/session_picker.py:85-87,234-316,464-483` | SessionPicker.press / _list_key / _messages_key / _info_key / application | policy | Fixed keymap (j/k, g/G, v, /, b, i, a, r, q, escape, c-w, c-u). | js-2lg.111 |
| `js/skill_picker.py:32-35,109-154,196-215` | SkillPicker.press / _search_key / application | policy | Fixed keymap (j/k, g/G, space, enter, /, escape, q, c-u). | js-2lg.113 |
| `js/replcomplete.py:39` | CMDCHAR | policy | The command character "/" is fixed. | js-2lg.115 |
| `js/keys.py:47-62` | ACTIONS | policy | The set of REPL actions is fixed in code; an assembly cannot add one. | js-2lg.119 |
| `js/keys.py:202-208` | prompt_bindings | policy | The --blocking prompt binds only history_search from the keymap. | js-2lg.119 |
| `js/exline.py:78-116` | run_ex | policy | Fixed ex verb table (w, wq, x, q, e, r, n, and the ! forms). | js-2lg.123 |
| `js/attach.py:168` | split_repl_attachments | policy | The "@" prefix as attachment syntax is fixed. | js-2lg.132 |
| `js/login_cli.py:133-179,719-726` | _curses_picker / _curses_prompt | policy | The picker keymap (/, j, k, q, a, n, e, space, Esc, Enter) is fixed in code. | js-2lg.144 |
| `js/login_cli.py:425-429,493-512` | _collect_api_login / _secondary_test_choice | policy | The y/n env-key question and the test-choice answers (Enter, q, t, a number) are fixed prompts in the part. | js-2lg.144 |
| `js/install.py:148,154` | ask_model | policy | The answer letters "a" (add provider) and "p" (pick) are fixed in code. | js-2lg.173 |
| `js/skills.py:212,238-246` | expand_user_invocation | policy | Fixes the /skill command word and its syntax inside the skills part. | js-2lg.189 |

## S10 injectable

73 findings.

| file:line | function | kind | what the part decides | bead |
|---|---|---|---|---|
| `js/toolkit/__init__.py:7-8` | module level | default | Builds the full default registry and a bare ToolContext at import time as the stock surface for callers that pass none. | js-2lg.33 |
| `js/toolkit/registry.py:501-502,577-601` | _default_prompts_root / build_default_registry | policy | Fixes the list of tool suites that exist, and falls back to the repo's own prompts/ directory when no roots are given. | js-2lg.38 |
| `js/toolkit/fs.py:2107-2126` | _offering_path_skills | policy | Wraps read, write and patch so their results start with a skill-offer reminder on their own. | js-2lg.46 |
| `js/toolkit/process_net.py:932-1023` | fetch | policy | Prefers aria2c when present and falls back to urllib on its own. | js-2lg.49 |
| `js/toolkit/search.py:56-63,350,387` | _http_body / docs_search | policy | Retries a 401 or 403 once without the Authorization header. | js-2lg.51 |
| `js/toolkit/terminal.py:53-83` | module constants / _draw_screen | presentation | Fixes the font list and colour palette of terminal snapshots. | js-2lg.56 |
| `js/toolkit/meta.py:172-312` | _run_one_task_async | policy | Builds the whole subagent turn itself (persona, routing, run_turn_async), with suppress_output=True fixed. | js-2lg.59 |
| `js/toolkit/meta.py:472,477-478` | _prepare_fan_out | default | Falls back to from_env(save_session=True) when the context has no config, and builds the subagent registry with build_default_registry. | js-2lg.59 |
| `js/toolkit/meta.py:105,111` | _session_file_for_id / _reserve_worker_session | policy | A subagent run moves the agent's latest-session pointer to its own session. | js-2lg.59 |
| `js/toolkit/task_jobs.py:171,195-216` | start | policy | Finds the REPL supervisor through a global and chooses the run strategy itself. | js-2lg.64 |
| `js/toolkit/lsp.py:319-336` | LanguageServer._answer | policy | Answers every workspace/configuration request with null, so a server gets no settings. | js-2lg.75 |
| `js/cli.py:389-446,2609-2614,2660-2667,4929-4936,4962-4964` | _resume_model_spec, _stamp_without_login, _resumed_layer, _resume_reasoning, _run_prompt, _run_args | policy | The rule that a resumed session goes back to its stamped model and reasoning, or falls back when the stamped provider has no login, is copied into two cli entry paths. | js-2lg.81 |
| `js/cli.py:2162-2182,2701,4985` | _restore_workspace | policy | On resume, cli restores the jail binds and cwd from the workspace mark by itself. | js-2lg.81 |
| `js/cli.py:2462-2503` | _agent_model_applies, _apply_agent_model, _apply_agent_reasoning | policy | The precedence that decides when agent.yaml model and reasoning apply over flags, env, jsrc or a resumed stamp lives only in cli. | js-2lg.81 |
| `js/cli.py:2668-2674,2693-2700,4943-4951,4997-5007` | _run_prompt, _run_args | policy | Reusing the recorded system prompt and reporting prompt-file changes is copied into two cli paths. | js-2lg.81 |
| `js/cli.py:1524-1548` | _apply_settings_result | policy | The side effects of a settings change (model-switch record, provider sync, registry rebuild, banner, context-window overrides) are combined with printing the result lines. | js-2lg.81 |
| `js/cli.py:2791-2822` | _run_prompt.turns | policy | The swarm sleep/wake loop, with auto-compaction before each sleep, is built into the cli one-shot path. | js-2lg.81 |
| `js/promptexpand.py:217-222,318-323` | expand_prompt._resolve, _run_capture | presentation | Directive failures and nonzero-exit warnings are written to stderr through msgs.warn inside the part. | js-2lg.92 |
| `js/promptexpand.py:266-273,318-323` | _sub_file, _run_capture | policy | A missing file embeds nothing, and a nonzero exit that printed output counts as success. | js-2lg.92 |
| `js/promptexpand.py:399-410` | _SUBSYSTEMS | policy | The set of subsystems, and which ones count as code, is a fixed module dict the caller cannot change. | js-2lg.92 |
| `js/setcmd.py:191-193,268-269` | apply_set, apply_unset | policy | Setting or unsetting BANNER changes the process-global message banner as a side effect. | js-2lg.95 |
| `js/screen.py:920-948` | build_app | presentation | Fixes the layout order (scrollback, status bar, input, exit hint, ex line, search toolbar), the ":" ex prefix, the multi-column completion menu and an input box of up to half the screen. | js-2lg.102 |
| `js/screen.py:955-966` | build_app | presentation | Fixes full_screen=True, mouse_support=False and 24-bit colour depth. | js-2lg.102 |
| `js/screen.py:768,866-873` | build_app (_tab) | policy | No completion while typing; Tab fills the common part, then cycles. | js-2lg.102 |
| `js/screen.py:780-782` | build_app.submit | presentation | The scrollback echo of a sent line is always display.user_line. Only whether to echo is injectable. | js-2lg.102 |
| `js/screen.py:145-238` | status_line | presentation | Fixes the bar's three groups, their fields, the order fields give way and a model-name cut at 12 characters. | js-2lg.102 |
| `js/messages.py:265-268` | ArgumentParser.error | policy | Prints usage to sys.stderr and exits the process with status 2. | js-2lg.106 |
| `js/messages.py:37,148-151` | BANNER / set_banner | default | The banner slot is one mutable module global per process, so two assemblies in one process share it. | js-2lg.106 |
| `js/picker.py:225,296-309` | ModelPicker._rows_text / application | presentation | Fixed selection glyph and a fixed two-pane layout with a 38-column provider pane. | js-2lg.110 |
| `js/picker.py:336-337` | pick_model | presentation | Binds the picker to sys.__stdin__ and sys.__stdout__. | js-2lg.110 |
| `js/session_picker.py:346,379,390,416,425,444,458` | subtitle / _list_body / _message_body / info_rows / _info_body | presentation | Fixed glyphs and layout (an 8-space preview indent, a 12-column label). | js-2lg.112 |
| `js/session_picker.py:551` | pick_session | presentation | Binds the picker to sys.__stdin__ and sys.__stdout__. | js-2lg.112 |
| `js/skill_picker.py:167,177-188` | subtitle / body | presentation | Fixed glyphs and a fixed column layout. | js-2lg.114 |
| `js/skill_picker.py:239` | pick_skills | presentation | Binds the picker to sys.__stdin__ and sys.__stdout__. | js-2lg.114 |
| `js/replcomplete.py:152-159` | JsCompleter.candidates | policy | A token that looks like a path always gets file completion, and an argument of any word that is not a command goes to the spellchecker. | js-2lg.116 |
| `js/replcomplete.py:44-46` | _VALUE_ENUM_KNOBS | default | Only EFFORT gets value completion, set by a table in code. | js-2lg.116 |
| `js/prompt_history.py:148-168,188-191` | import_legacy / PromptHistory.load_history_strings | policy | When the caller passes state_root, folds old per-agent history files into the new file and deletes them on load. | js-2lg.118 |
| `js/pastes.py:23,44-48` | _pastes / keep | default | Kept pastes live in one module global for the process. | js-2lg.122 |
| `js/exline.py:112-113` | run_ex | policy | Any word naming a program on PATH runs that program on the buffer, with no confirmation. | js-2lg.124 |
| `js/clipimage.py:41,58-62` | _images / keep | default | Pasted images live in one module global for the process. | js-2lg.129 |
| `js/config.py:311-322,581,584` | _select_prompt_dir / from_env | policy | The agent prompt roots and their order (project > global > repo) are fixed in code. | js-2lg.135 |
| `js/config.py:486,566` | from_env | presentation | Building a Config sets the global message banner and the fset bound settings, so loading config changes presentation for the whole process. | js-2lg.135 |
| `js/config.py:507-520` | from_env | policy | When the default model names a provider with no login, the run goes on with no provider instead of failing; the fallback is fixed in code. | js-2lg.135 |
| `js/settings.py:787,796,806` | _package_settings | policy | A missing or bad js/jsrc raises SystemExit and ends the process, with the text formatted for sys.stderr. | js-2lg.139 |
| `js/settings.py:934` | collect_settings | policy | Every settings collect clears all operator /fset overrides held in process-global state. | js-2lg.139 |
| `js/login_cli.py:358,360,496,498` | _input / _secondary_test_choice | presentation | It reads with input()/getpass and writes with print() directly. | js-2lg.146 |
| `js/login_cli.py:526` | _run_secondary_test | policy | The test call reads the process-global invocation settings (config.run_settings) instead of settings from its caller. | js-2lg.146 |
| `js/login_cli.py:258-259` | _select_models_to_cache | policy | With no terminal it keeps every fetched model without asking. | js-2lg.146 |
| `js/login_cli.py:551-554` | _post_fetch_confirmation | policy | The test step is skipped when the provider's model listing counts as proof of auth. | js-2lg.146 |
| `js/login_cli.py:871-885` | _run_login | policy | A login whose server cannot be reached is saved anyway, and a 404 adds a guessed "/v1" hint. | js-2lg.146 |
| `js/login_cli.py:318-321` | _login_provider_rows | policy | The openai-completions and openai-responses rows are hidden from the provider list in code. | js-2lg.146 |
| `js/login_cli.py:843-904` | _run_login | policy | The login flow (collect, fetch, confirm, keep-list, save) is tied to the terminal prompts, so another assembly cannot run a login without this UI. | js-2lg.146 |
| `js/codex_auth.py:368-369,407,465` | login_browser / login_device | presentation | The OAuth flows print the URL, the paste hint and the device code to stdout themselves. | js-2lg.149 |
| `js/codex_auth.py:378-389` | login_browser | policy | The browser flow reads a pasted callback from sys.stdin itself. | js-2lg.149 |
| `js/codex_auth.py:381` | login_browser | policy | The flow opens a web browser on its own, and unlike login_device has no open_browser parameter. | js-2lg.149 |
| `js/codex_provider.py:306-332` | _repair_tool_pairs | policy | Unmatched tool calls and outputs are repaired automatically with no way to turn it off. | js-2lg.153 |
| `js/xai_auth.py:263-264` | login_browser | presentation | The flow prints the URL itself and always opens a web browser. | js-2lg.156 |
| `js/providers.py:123-476,561-566` | _BUILTINS / all_providers / login_providers | policy | The provider registry is a fixed tuple with no way for an assembly to add or replace a provider. | js-2lg.160 |
| `js/routing.py:81-82` | _saved_login | policy | Reading a route refreshes the xAI OAuth token automatically (network POST and file write). | js-2lg.163 |
| `js/routing.py:144-164` | resolve_model_route | policy | The rule that env keys alone never create a route, and the refusal when a known provider has no login, are fixed in code. | js-2lg.163 |
| `js/model_metadata.py:192,666,672` | accepts_image_input / context_window / max_output_tokens | policy | Plain capability lookups can start a network catalog refresh. | js-2lg.165 |
| `js/home.py:629-649` | migrate_once | policy | The startup migration of old locations (agent conversion and session refiling included) runs automatically and prints each step as text to a stream instead of handing Steps to a callback. | js-2lg.170 |
| `js/install.py:28-34` | KEYS | default | The tool API keys the installer asks for are a fixed list in code. | js-2lg.172 |
| `js/install.py:89,120,122,141,159,168` | ask_keys / _say_unrouted / ask_model / _asker | presentation | It prints with msgs.say/print() itself, although the questions go through an injected ask. | js-2lg.172 |
| `js/tool_binaries.py:600,625` | _provision_aria2_via_brew / _provision_termux_tools | policy | It runs `brew install` and `pkg install -y` automatically. | js-2lg.176 |
| `js/tool_binaries.py:694,715,722,725,727,736` | install_all / main | presentation | Install progress is printed by the part itself. | js-2lg.176 |
| `js/session_query.py:542,550-599` | session_line / hit_line / message_lines | presentation | Fixes the picker row layout, column widths, tree glyphs and #NNNN numbering in the part. | js-2lg.183 |
| `js/session_text.py:47-48` | module constants | presentation | Fixes the speaker labels "you" and "ape" for every transcript row and search entry. | js-2lg.184 |
| `js/session_text.py:394-407` | _Transcript.header | presentation | Fixes the .txt transcript header layout and its English field labels in code. | js-2lg.184 |
| `js/swarm_run.py:95,101-102,107,116` | _configure | policy | Reaches into cli private helpers (_apply_agent_model, _resolve_cli_model_override, _validate_cli_reasoning, _registry_for) for the model and tool surface. | js-2lg.196 |
| `js/jail.py:807-811,924-927` | enter / active | policy | Keeps the jail as one process-wide global (ACTIVE plus the JS_JAIL env var) instead of taking it from the caller. | js-2lg.206 |
| `js/commit_helper.py:150-159,284,304,326,345,370,421,424` | _print / _out / _err / cmd_survey / cmd_stage / cmd_commit | presentation | The command functions print to stdout/stderr themselves and return only exit codes, not results to the caller. | js-2lg.209 |
| `js/mcp/host.py:555-567` | MCPHost._ensure_server | default | Never passes a client name or version, so every MCP server sees the client default "js" "0.1.0" (client.py:52-53). | js-2lg.216 |

## Modules with no finding

`js/__init__.py`, `js/__main__.py`, `js/activity.py`, `js/agent_messages.py`, `js/context_budget.py`, `js/fset.py`, `js/mcp/__init__.py`, `js/mcp/client.py`, `js/mcp/protocol.py`, `js/mcp/transports.py`, `js/mcp/types.py`, `js/model_matching.py`, `js/motd.py`, `js/output.py`, `js/paranoid.py`, `js/paths.py`, `js/prompt_commands.py`, `js/reasoning_display.py`, `js/session_catalog.py`, `js/session_index.py`, `js/session_store.py`, `js/stats.py`, `js/supervisor.py`, `js/text_bytes.py`, `js/tooldiag.py`, `js/toolkit/kernel_bridge.py`, `js/toolkit/kernel_client.py`, `js/toolkit/sanitize.py`, `js/toolstats.py`, `js/turn_settings.py`, `js/turn_surface.py`
