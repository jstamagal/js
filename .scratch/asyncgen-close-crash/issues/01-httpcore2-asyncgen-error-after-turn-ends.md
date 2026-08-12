# httpcore2 async generator error dumps a traceback after a clean turn ends

Status: needs-triage

Reported: 2026-08-12

## What happened

A one-shot `-p` run finished cleanly — `finish=stop`, telemetry line
printed, no tool calls — and then a `RuntimeError` traceback was dumped to
the terminal from asyncgen shutdown.

## Repro

Run from `~/.claude` (not the repo):

```
js --model deepseek/deepseek-v4-flash -d -p "Ape. wake up and fart around once or twice and then end turn"
```

Ran against the uv **tool** install (`~/.local/share/uv/tools/js`), not
`.venv`.

## Output

```
  ▸ run model=deepseek-v4-flash  provider=deepseek  base=https://api.deepseek.com  ctx=1000000  max_out=384000  effort=high  vision=off  tools=5
[... normal model output ...]
  ▸ 3675ms  finish=stop  tool_calls=0  139 tok  37.8 tok/s  ttft 2214ms  cache 50%
an error occurred during closing of asynchronous generator <async_generator object PoolByteStream.__aiter__ at 0x7ff6fc21fb50>
asyncgen: <async_generator object PoolByteStream.__aiter__ at 0x7ff6fc21fb50>
Traceback (most recent call last):
  File ".../httpcore2/_async/connection_pool.py", line 427, in __aiter__
    yield chunk
GeneratorExit

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File ".../httpcore2/_async/connection_pool.py", line 425, in __aiter__
    async with safe_async_iterate(self._stream) as iterator:
               ~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^
  File ".../contextlib.py", line 271, in __aexit__
    raise RuntimeError("generator didn't stop after athrow()")
RuntimeError: generator didn't stop after athrow()
```

Session: `20260812T225542067619Z-498e2cad8f084d4d`

## Expected

The run ends after the telemetry line. Nothing else on the terminal.

## Notes

- Not yet established: whether it reproduces every run, only with deepseek,
  only with `-d`, or only from the tool install.
- Nothing is reported as failing — the turn succeeded. This is noise on the
  shutdown path.
