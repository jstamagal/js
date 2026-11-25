Run the `{{AGENT_ID}}` agent as a subagent. Provide one or more task strings; the agent loads its own persona and selected tool surface.

<!--parameters-->

### tasks

One or more clear, detailed task prompts to run in parallel.

### model

Model for the subagent(s), same string as --model. Set it when the operator names one; omit otherwise.

### reasoning

Reasoning effort for the subagent(s): minimal, low, medium, high, xhigh or max. Set it when the operator names one; omit otherwise.
