Apply several exact replacements to one file in a single operation.

Prefer this over repeated `patch` calls when you need multiple changes in the
same file. Each edit follows the same exact-match rules as `patch`.

Required preparation:
- Read the target file with `read` before editing.
- Verify the path is the intended file.

Inputs:
- `file_path`: target file path.
- `edits`: ordered list of replacement operations.
- Each edit requires `old_string` and `new_string`.
- Each edit may include `replace_all`.

Execution rules:
- Edits are applied in the order provided.
- Each edit sees the result of previous edits.
- If any edit is missing, ambiguous, or cannot match, no updated file is written.
- `old_string` must match exactly, including whitespace and indentation.
- `old_string` and `new_string` must differ.
- Use `replace_all` only when every occurrence of that edit should change.

Planning guidance:
- Make all edits produce idiomatic, correct code.
- Avoid edits that conflict with later edits in the same list.
- Use enough surrounding context to make each `old_string` unique.
- Do not leave the code in a broken intermediate state.
- Do not add emojis unless the operator explicitly asked for them.
