# Hard pins hold openai and anthropic a major version behind

Category: bug
Filed: 2026-09-10
Component: `pyproject.toml` dependency block, `uv.lock`

## Measured 2026-09-10

Installed is the uv tool install on PATH (`~/.local/share/uv/tools/js/`), which
is the `js` used daily and for every benchmark. Latest is PyPI at time of filing.

```
ai           installed 0.4.2      latest 0.5.2      behind (minor)
openai       installed 2.44.0     latest 3.11.0     behind (MAJOR)
anthropic    installed 0.113.0    latest 1.4.0      behind (MAJOR)
httpx        installed 0.28.1     latest 0.28.1     current
```

Commands used:

```
~/.local/share/uv/tools/js/bin/python -c \
  "import importlib.metadata as m; print(m.version('openai'))"
curl -s https://pypi.org/pypi/openai/json | jq -r .info.version
```

## Not a drift bug — that one is already fixed

`.scratch/asyncgen-close-crash/issues/02-ai-sdk-version-drift.md` (2026-08-12)
reported `uv.lock` pinning `ai 0.2.1` while the installed tool ran `0.4.2`.
That is no longer true: `pyproject.toml` requires `ai[openai,anthropic]==0.4.2`,
`uv.lock` says `version = "0.4.2"`, and the installed tool reports `0.4.2`. The
lock, the manifest, and the install agree. **Close that report, do not act on it.**

The live problem is different: nothing drifts because everything is `==` pinned,
and the pins have gone stale.

## Why openai is pinned, and why that matters

`pyproject.toml` carries the reason in a comment beside the pin:

> `just install` resolves from these constraints rather than uv.lock. Keep the
> provider boundary aligned with the tested lock: openai 3 switched to
> httpx2/httpcore2 and exposed a broken async-generator shutdown path.

That broken shutdown path is
`.scratch/asyncgen-close-crash/issues/01-httpcore2-asyncgen-error-after-turn-ends.md`
— a `RuntimeError` traceback dumped after a clean `-p` run.

So the ordering is forced, and doing it out of order trades one bug for another:

1. Fix the asyncgen shutdown handling (report 01).
2. That removes the stated reason for `openai==2.44.0`.
3. Then move to openai 3.x, which brings httpx2/httpcore2 with it.

Bumping the pin first just reintroduces the traceback on every clean run.

## anthropic

`0.113.0 → 1.4.0` has no such note beside it. It is a major and needs the
changelog read and the provider boundary exercised, not a version bump.

## ai

`0.4.2 → 0.5.2` is a minor. Lowest risk of the three; do it first and confirm
the suite is still green against the branch baseline.

## Caution

Two benchmark runs were live in `/tmp/toolsweep-deepseek` and
`/tmp/toolsweep-q27` when this was filed, both executing through `~/js/.venv`
via the editable install. Changing installed packages mid-run swaps the
interpreter underneath them and makes their results meaningless. Confirm nothing
is running before touching the environment.
