# Bug: patch tool rejects `edits` array (serialized as string)

Date: 2026-09-05
Agent: js (headless run, vader)
Tool: patch

## What happened
Calling `patch` with the batch form — `edits` as a JSON array of
{old_string, new_string} objects — fails schema validation. Error:

    ERROR: invalid arguments for patch: {'edits': '[{"old_string": "..."}]', 'file_path': '...'} is not valid under any of the given schemas

The `edits` value arrives at the validator as an escaped JSON *string*
(note the outer quotes and `\\n` in the echo), not as a parsed array.
Happened twice on two different calls (2-edit and 8-edit batches).
Single-replacement form (old_string/new_string at top level) and full
`write` overwrite both work fine.

## Expected
Batch `edits` array validated and applied per docs.

## Workaround
Use one single-replacement patch per call, or `write` full-file
overwrite.

## Repro
Any patch call using `edits`: [...]
