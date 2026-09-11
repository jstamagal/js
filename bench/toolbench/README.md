# Tool bench

Races js against itself (full vs shell-only surface) and against other CLI agents on bug-fix tasks mined from real
repositories, and reports how each one used its tools.

- **Tasks**: [RepoRacer](https://github.com/HabrielStark/RepoRacer) mines each
  suite repo's history for commits that changed source *and* tests, checks the
  parent commit out into a worktree, hides the commit's tests, and hands the
  agent the commit message as the task. Grading = the hidden tests pass on the
  agent's tree. Real code, real fixes, nothing authored for a benchmark.
- **Sandbox**: `sandbox.sh` runs the install, the agent, and the tests in one
  Docker image (`Dockerfile`: js, rg/fd, uv-managed Python, Node 22, Go),
  unprivileged, with the worktree at `/workspace` and toolchain caches on a
  named volume. The container reaches the model server over the LAN.
- **Tool telemetry**: after each run `js.toolstats` prints one `TOOLSTATS`
  line from the js session: calls per tool, error results, turns, bytes moved,
  and shell habits (cat/grep/find/sed -i/cd/rm/redirects when a dedicated tool
  may have existed). `run.py` joins it onto RepoRacer's result per task.

```
just toolbench-image                # build the sandbox (once, and after js changes)
just toolbench-mine                 # what tasks each repo yields; no model needed
just toolbench-smoke                # fake agents through the sandbox on one task
just toolbench                      # slim vs stock on every repo in the suite
just toolbench --agents js-shell --repos click --tasks 2
just toolbench --agents claude,js-full
```

Model server: `suite.toml` `[model]` or `TOOLBENCH_BASE_URL` / `TOOLBENCH_MODEL`.
Results land in `results/<stamp>/` with `summary.md`, `summary.json`, RepoRacer's
`results.jsonl` and HTML report per repo, and every js session under `telemetry/`.
