Ask the operator a blocking question.

Use this only when required information cannot be obtained from local files,
tools, prior context, or a safe default.

Good uses:
- A choice materially changes implementation direction.
- Credentials, permissions, or external state are required.
- Requirements conflict and guessing would risk doing the wrong work.
- The operator explicitly needs to decide between alternatives.

Avoid using this for:
- Questions that can be answered by reading the repository.
- Preference questions where a conservative default is obvious.
- Status updates or conversational clarifications that are not blocking.

Options:
- Provide concise options when the operator must choose.
- Use at most five options.
- `multiple=true` allows more than one option.

Runtime behavior:
- The result starts with `FOLLOWUP_REQUIRED`.
- The tool loop stops after this result so the operator can answer.
