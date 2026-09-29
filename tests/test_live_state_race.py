"""A turn's cfg comes from one consistent view of the REPL's live state.

Commands run on an executor thread and write the live state; the turn consumer
snapshots it on the loop. Each `pair<i>.jsrc` switches the base URL with /set
and the model with /model, so a turn built from one command's effects always
carries a matching `m<i>` and `http://h<i>.invalid`.
"""

from __future__ import annotations

import asyncio
import threading

from js import cli
from js.config import from_env
from repl_driver import run_async


def _setup(monkeypatch, tmp_path, pairs: int) -> list[tuple[str, str | None]]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    for i in range(pairs):
        (tmp_path / f"pair{i}.jsrc").write_text(
            f"set provider.base_url http://h{i}.invalid\n"
            + "set runtime.steer one\n" * 8  # widens the window between the two
            + f"model m{i}\n",
            encoding="utf-8",
        )
    seen: list[tuple[str, str | None]] = []

    async def run_turn_async_stub(cfg, system, messages, telemetry, **kwargs):
        seen.append((cfg.model, cfg.provider_base_url))
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_stub)
    return seen


def _assert_consistent(seen: list[tuple[str, str | None]]) -> None:
    mixed = [(model, base) for model, base in seen if base != f"http://h{model[1:]}.invalid"]
    assert not mixed, f"turns built from half-applied commands: {mixed}"


def test_turn_start_waits_for_a_command_that_is_half_applied(monkeypatch, tmp_path):
    seen = _setup(monkeypatch, tmp_path, pairs=2)
    halfway = threading.Event()
    turn_ran = threading.Event()
    real_set_model = cli._set_model_via_route

    def set_model_after_a_pause(state, cfg, model):
        if model == "m1":
            # /set already applied h1; hold here until a turn has run, or give up.
            halfway.set()
            turn_ran.wait(timeout=0.5)
        real_set_model(state, cfg, model)

    monkeypatch.setattr(cli, "_set_model_via_route", set_model_after_a_pause)

    async def stub_turn_marks(cfg, system, messages, telemetry, **kwargs):
        seen.append((cfg.model, cfg.provider_base_url))
        turn_ran.set()
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn_async", stub_turn_marks)

    async def script(on_line):
        loop = asyncio.get_running_loop()
        await on_line(f"/load {tmp_path / 'pair0.jsrc'}")
        command = asyncio.ensure_future(on_line(f"/load {tmp_path / 'pair1.jsrc'}"))
        assert await loop.run_in_executor(None, halfway.wait, 5)
        await on_line("go")
        await command

    run_async(monkeypatch, from_env(), script)
    assert seen == [("m1", "http://h1.invalid")]


def test_interleaved_set_and_model_commands_never_mix_into_a_turn(monkeypatch, tmp_path):
    rounds = 200
    seen = _setup(monkeypatch, tmp_path, pairs=rounds)

    async def script(on_line):
        await on_line("/set runtime.steer one")
        await on_line(f"/load {tmp_path / 'pair0.jsrc'}")
        lines = []
        for i in range(1, rounds):
            lines += [f"/load {tmp_path / f'pair{i}.jsrc'}", f"turn {i}"]
        await asyncio.gather(*(on_line(line) for line in lines))

    run_async(monkeypatch, from_env(), script)
    assert len(seen) == rounds - 1
    _assert_consistent(seen)
