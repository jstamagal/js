# Proposed js --help: skill-shaped, one screen

Current help is ~40 flags with paragraph-length descriptions; an
agent reading it spends hundreds of tokens before it knows how to
run one prompt. Proposal: --help prints the short form below, and
--help-full prints today's full text unchanged.

    js — one agent, one terminal.

    RUN
      js                          interactive REPL in this directory
      js -p "task"                one prompt, print the answer
      echo task | js -p           same, from stdin
      js -C DIR ...               run as if launched from DIR

    PICK
      -a NAME     agent profile (~/.config/js/agents/NAME)
      -m MODEL    provider/model, e.g. openai-codex/gpt-5.6-sol
      -r EFFORT   off|minimal|low|medium|high|xhigh|max

    STATE (sessions are saved by default — that is what you want)
      -s ID       resume a session
      -n          do NOT save.  Costs more: no resume, so the next
                  run re-reads everything from scratch.  Use only
                  for throwaway one-liners.
      --debug-file PATH   full request trace, for debugging js

    SCRIPTING
      -q          no resume hint after the answer
      -f PATH     attach a file or image (repeatable)
      --max-out N max output tokens
      --extra K=V one-off config, e.g. --extra limits.task_max_depth=3

    MORE
      js --login [PROVIDER]   sign in      js --list-models
      js --commit             commit agent js --help-full

Rationale for the -n wording: every agent that drives js reaches
for -n by reflex, including the one that wrote this file, which
means every correction round re-reads the repository from zero and
no stderr or session survives for telemetry.  The flag is not free
and the help should say so where it is read.
