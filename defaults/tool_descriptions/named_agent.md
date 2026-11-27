Run the `{{AGENT_ID}}` agent as a subagent. Provide one or more task strings; the agent loads its own persona and selected tool surface. By default the call returns the subagents' handles at once and they run while you work; their result comes to you in a `<js-reminder>` when they finish. Watch, stop and talk to them with the `task` tool's list, poll, kill and send actions.

<!--parameters-->

### tasks

One or more clear, detailed task prompts to run in parallel.

### model

Model for the subagent(s), same string as --model. Set it when the operator names one; omit otherwise.

### reasoning

Reasoning effort for the subagent(s): minimal, low, medium, high, xhigh or max. Set it when the operator names one; omit otherwise.

### background

true (the default for the main agent): return the handles at once while the subagents run. false: wait for their result.
