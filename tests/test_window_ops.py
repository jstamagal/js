"""ircII's /window on the REPL screen: the op chain, split screens, killing a
running subagent's window, windows opened with /window new, and subagent
windows that were never looked at closing when their subagent ends."""

from __future__ import annotations

import asyncio

from repl_driver import run_async
from test_subagent_windows import _settle, _text, _until, _worker

from js import cli, fset, messages as msgs, repl_window_commands, screen, screen_scrollback
from js.config import from_env
from js.toolkit import ToolContext, subagents


def _windows(count: int) -> screen.Windows:
    windows = screen.Windows()
    windows.set_main(screen_scrollback.Scrollback())
    for _ in range(count):
        windows.open()
    return windows


def test_switching_to_a_hidden_window_puts_it_in_the_current_ones_place():
    windows = _windows(2)

    windows.switch(3)

    assert windows.visible == [3]
    assert (windows.current, windows.previous) == (3, 1)


def test_show_splits_the_screen_and_hide_gives_the_rows_back():
    windows = _windows(2)

    windows.show(2)
    windows.show(3)

    assert windows.visible == [1, 2, 3]
    assert windows.current == 3
    assert windows.hide()
    assert windows.visible == [1, 2]
    assert windows.current == 2
    windows.hide_others()
    assert windows.visible == [2]
    assert not windows.hide()


def test_a_visible_window_has_no_activity_mark():
    windows = _windows(2)
    windows.show(2)
    windows.switch(1)

    for number in (2, 3):
        windows.view(number).scrollback.append("out\n")
        windows.view(number).scrollback.flush()

    assert windows.activity() == [3]


def test_closing_the_only_window_on_screen_hands_its_place_back():
    windows = _windows(2)
    windows.switch(2)
    windows.switch(3)

    windows.close(3)

    assert windows.visible == [2]
    assert windows.current == 2
    windows.close(2)
    assert windows.visible == [1]
    assert windows.current == 1


def test_swap_move_renumber_and_cycle():
    windows = _windows(3)
    windows.show(2)
    windows.swap(4)
    assert windows.visible == [1, 4]
    windows.swap(1)
    assert windows.visible == [4, 1]
    assert windows.current == 1
    windows.move(-1)
    assert windows.visible == [1, 4]

    windows.switch(4)
    assert windows.renumber(3)
    assert windows.current == 3
    assert windows.visible == [1, 3]
    assert windows.view(4) is not None and windows.view(4).number == 4
    assert not windows.renumber(1)
    windows.switch(1)
    assert not windows.renumber(5)

    assert windows.cycle(-1) == 4
    assert windows.cycle(1) == 1


def test_window_max_caps_the_windows_open():
    fset.bind_settings({"WINDOW_MAX": 3})
    try:
        windows = _windows(5)
        assert windows.numbers() == [1, 2, 3]
        windows.close(2)
        assert windows.open().number == 2
    finally:
        fset.bind_settings(None)


def test_op_prefixes_name_one_op_or_the_op_every_match_starts_with():
    assert repl_window_commands.match_op("k")[0] == "kill"
    assert repl_window_commands.match_op("kill_o")[0] == "kill_others"
    assert repl_window_commands.match_op("prev")[0] == "previous"
    assert repl_window_commands.match_op("ne")[0] is None
    assert repl_window_commands.match_op("ne")[1] == ["new", "next"]


def _repl(monkeypatch, tmp_path, script, child):
    """Run the REPL over ``script(on_line, windows)``. The main turn launches
    one worker per line of a prompt "launch a\\nb"; ``child(cfg, messages,
    kwargs)`` stands in for each worker's turn."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_DEFAULT_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    _worker(tmp_path)
    results: list[str] = []

    async def turn(cfg, system, messages, telemetry, **kwargs):
        if cfg.agent_id == "worker":
            await child(cfg, messages, kwargs)
            return
        text = str(messages[-1]["content"])
        if text.startswith("launch "):
            context = ToolContext(cwd=tmp_path)
            context.config = cfg
            results.append(await subagents.task_async(
                tasks=text.removeprefix("launch ").split(","), agent_id="worker", context=context))
        messages.append({"role": "assistant", "content": "main done"})

    async def driver(on_line):
        windows = subagents.window_opener.get().__self__
        await script(on_line, windows)

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn)
    run_async(monkeypatch, from_env(), driver)
    return results


def test_windows_nobody_looked_at_close_when_their_subagents_end(monkeypatch, tmp_path):
    async def child(cfg, messages, kwargs):
        print("worked")
        messages.append({"role": "assistant", "content": "ok"})

    async def script(on_line, windows):
        await on_line("launch a,b,c,d")
        await _until(lambda: windows.windows.numbers() == [1])

    results = _repl(monkeypatch, tmp_path, script, child)

    assert results and results[0].count("ok") == 4


def test_kill_stops_a_running_subagent_and_the_screen_goes_back(monkeypatch, tmp_path):
    release = asyncio.Event()

    async def child(cfg, messages, kwargs):
        print("working")
        if messages[-1]["content"] == "slow":
            await asyncio.Event().wait()
        await release.wait()
        messages.append({"role": "assistant", "content": "fast done"})

    async def script(on_line, windows):
        await on_line("launch slow,fast")
        await _until(lambda: windows.windows.numbers() == [1, 2, 3])
        await on_line("/window 2")
        assert windows.windows.current == 2
        await on_line("/window kill")
        await _until(lambda: windows.windows.numbers() == [1, 3])
        assert windows.windows.current == 1
        assert windows.windows.visible == [1]
        release.set()

    results = _repl(monkeypatch, tmp_path, script, child)

    assert "Cancelled" in results[0] or "cancel" in results[0].lower()
    assert "fast done" in results[0]


def test_window_new_launches_its_agent_on_the_first_line_and_resumes_after(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    async def child(cfg, messages, kwargs):
        seen.append([str(m["content"]) for m in messages if m.get("role") == "user"])
        print(f"child turn {len(seen)}")
        messages.append({"role": "assistant", "content": f"reply {len(seen)}"})

    async def script(on_line, windows):
        await on_line("/window new agent worker name scratch")
        view = windows.windows.view()
        assert view.number == 2 and view.name == "scratch"
        assert windows.windows.visible == [1, 2]
        await on_line("first")
        await _until(lambda: len(seen) == 1 and not view.owner.busy())
        await on_line("second")
        await _until(lambda: len(seen) == 2 and not view.owner.busy())
        await _settle()
        assert "child turn 2" in _text(view.scrollback)
        assert "child turn" not in _text(windows.windows.view(1).scrollback)
        await on_line("/window scratch hide")
        assert windows.windows.visible == [1]
        assert windows.windows.view(2) is view

    _repl(monkeypatch, tmp_path, script, child)

    assert seen == [["first"], ["first", "second"]]


def test_a_refused_op_stops_the_chain(monkeypatch, tmp_path):
    async def child(cfg, messages, kwargs):
        messages.append({"role": "assistant", "content": "ok"})

    async def script(on_line, windows):
        await on_line("/window new name a hide hide name b")
        await _settle()
        assert windows.windows.visible == [1]
        assert windows.windows.view(2).name == "a"
        await on_line("/window kill")
        assert windows.windows.numbers() == [1, 2]
        await on_line("/window 2 kill")
        assert windows.windows.numbers() == [1]
        main = _text(windows.windows.view(1).scrollback)
        assert msgs.WINDOW_LAST_ON_SCREEN.words(number=1) in main
        assert msgs.WINDOW_MAIN_STAYS.words() in main

    _repl(monkeypatch, tmp_path, script, child)
