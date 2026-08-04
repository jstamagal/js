# Ten things that would make js a great tool for agents to drive

Written by an agent that drove js headless for about ten hours:
sixty-plus invocations, three concurrent worktrees, coder and
reviewer crews, through two provider outages. Every failure that
night was the provider or my orchestration. None were js. These are
the ten things I wanted while driving, filtered through your stated
north star: simple from a terminal, clean when piped into a screen
reader, adaptable to any task. Items 1 and 2 are already being
implemented on the fix/agent-driver-warts branch.

## 1. Machine-readable error taxonomy

Terminal failures print a red string and exit 1, so an orchestrator
greps stderr for phrases like "overloaded" to decide whether a
retry is worth it. One JSON line on stderr with a class
(provider_transient, provider_auth, config, tool_failure,
internal), a retryable flag, and a distinct exit code per class
would replace every one of those greps. I built string-matching
outage detection three separate times tonight.

## 2. Durable turn checkpoint on provider death

When the provider dies mid-turn, completed tool calls and partial
output vanish with the process. I lost eleven minutes of coder work
to one "no assistant response" and started committing
work-in-progress from the outside as insurance. The harness should
flush everything completed to the session file before exiting and
say so in the error JSON.

## 3. Structured output contract

My review gates work by demanding the same JSON in a file and on
stdout, then diffing them, because there is no way to make js
guarantee a schema. A flag that takes a JSON schema, validates the
final answer against it, re-prompts once or twice on mismatch, and
exits with a distinct code on failure would collapse about two
hundred lines of my gate plumbing into one argument.

## 4. Harness-enforced write scope

My reviewer profile says read-only, but the tools can write; the
prompt is a prayer. After every review I ran git status to check
the agent behaved. A write-root flag the toolkit enforces would
make the guarantee real, and a read-only mode with one writable
artifact path is exactly what a review gate needs.

## 5. Live event stream

A flag that appends JSONL lifecycle events (turn started, tool
called, tool finished, tokens so far) to a file or fd while the
agent runs. Tonight the only sign of life was buffered stdout;
I watched hour-long coder runs by polling git status in their
worktrees. This is also the robots-dashboard bridge for free, and
a narration mode for a screen reader falls out of the same stream.

## 6. Usage receipt on exit

One JSON line per invocation: tokens in and out, estimated cost
from the models metadata js already caches, provider, session id.
Your quota ledger and any burn-down scheduler need exactly this,
and today it exists nowhere outside the provider's dashboard.

## 7. Continue a session headless

Every correction round tonight started a fresh coder that re-read
the repository from zero. Being able to reopen a session and add
one instruction (fix these two findings, keep everything else)
would have cut most retry rounds roughly in half. The session
files already contain everything needed.

## 8. Named seats instead of raw model strings

The -m flag takes one model. An orchestrator wants a seat:
coder-bulk resolving to an ordered list (kimi k3, then glm 5.2,
then qwen) with the harness moving down the list on auth or quota
failure. This is the hotswap idea, and it belongs in the harness
where the provider errors are visible, not in every caller.

## 9. Budget flags with graceful exit

I enforced timeouts by SIGKILLing process groups from outside,
which is how work gets lost. A max-seconds or max-cost flag that
stops at a clean boundary, checkpoints per item 2, and exits with
a distinct code would let a scheduler be strict without being
destructive.

## 10. Clean channel discipline everywhere

Stdout should carry the answer and nothing else; progress and
errors belong on stderr; ANSI color should shut off automatically
when output is not a TTY. Tonight every log I collected has raw
escape codes in it, and anything piped to a screen reader would
read them aloud. One env var to force plain output would make js
the rare tool that sounds as good as it prints.
