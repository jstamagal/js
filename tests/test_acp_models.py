import asyncio
from types import SimpleNamespace

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

from js import acp_agent, acp_turn, logins, paths


@pytest.fixture
def cached_models(tmp_path, monkeypatch):
    monkeypatch.setenv("JS_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(logins, "_CONFIG_DIR_OVERRIDE", None)
    logins.save_model_cache({"example": ["old", "new"]})
    cwd = tmp_path / "project"
    cwd.mkdir()
    return cwd


def model_option(response):
    return response.config_options[0]


def model_values(response):
    return {option.value for option in model_option(response).options}


def test_model_choice_survives_cold_resume_and_switches(cached_models, monkeypatch):
    calls = []

    async def run_turn(**kwargs):
        calls.append(kwargs["options"])

    monkeypatch.setattr(acp_turn, "run_turn", run_turn)

    async def scenario():
        cwd = str(cached_models)
        first = acp_agent.Agent([])
        created = await first.new_session(cwd)
        session_id = created.session_id
        assert model_values(created) == {"configured", "example/old", "example/new"}
        with pytest.raises(RequestError):
            await first.set_config_option("model", session_id, "unlisted")
        await first.set_config_option("model", session_id, "example/old")

        second = acp_agent.Agent([])
        second.on_connect(SimpleNamespace())
        resumed = await second.resume_session(session_id, cwd)
        assert model_option(resumed).current_value == "example/old"
        prompt = [TextContentBlock(text="Hello")]
        await second.prompt(session_id, prompt)
        await second.set_config_option("model", session_id, "example/new")
        await second.prompt(session_id, prompt)
        await second.set_config_option("model", session_id, "configured")
        await second.prompt(session_id, prompt)
        assert calls == [["--model=example/old"], ["--model=example/new"], []]

        pinned = acp_agent.Agent(["-m", "example/old"])
        pinned.on_connect(SimpleNamespace())
        resumed = await pinned.resume_session(session_id, cwd)
        assert model_values(resumed) == {"example/old"}
        assert model_option(resumed).current_value == "example/old"
        with pytest.raises(RequestError):
            await pinned.set_config_option("model", session_id, "example/new")
        await pinned.prompt(session_id, prompt)
        assert calls[-1] == ["-m", "example/old"]

    asyncio.run(scenario())


def test_cache_discovery_obeys_session_isolation(cached_models, monkeypatch):
    async def scenario():
        (cached_models / ".js").mkdir()
        cwd = str(cached_models)
        assert model_values(await acp_agent.Agent(["--isolate"]).new_session(cwd)) == {"configured"}
        (cached_models / ".isolate").touch()
        assert model_values(await acp_agent.Agent([]).new_session(cwd)) == {"configured"}
        assert "example/new" in model_values(
            await acp_agent.Agent(["--no-isolate"]).new_session(cwd)
        )

    monkeypatch.setattr(paths, "_ISOLATE", None)
    asyncio.run(scenario())


def test_resumed_prompt_lock_is_held_until_cancellation_cleanup(cached_models, monkeypatch):
    monkeypatch.setenv("JS_ACP_STEER_WAIT", "0")

    async def scenario():
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def run_turn(**kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()

        monkeypatch.setattr(acp_turn, "run_turn", run_turn)
        first, second = acp_agent.Agent([]), acp_agent.Agent([])
        first.on_connect(SimpleNamespace())
        second.on_connect(SimpleNamespace())
        cwd = str(cached_models)
        session_id = (await first.new_session(cwd)).session_id
        await second.resume_session(session_id, cwd)
        prompt = [TextContentBlock(text="Hello")]
        running = asyncio.create_task(first.prompt(session_id, prompt))
        try:
            await started.wait()
            with pytest.raises(RequestError):
                await second.prompt(session_id, prompt)
            await first.cancel(session_id)
            await cleaning.wait()
            with pytest.raises(RequestError):
                await second.prompt(session_id, prompt)
        finally:
            release.set()
            await first.cancel(session_id)
            await running
        assert running.result().stop_reason == "cancelled"

        async def complete(**kwargs):
            pass

        monkeypatch.setattr(acp_turn, "run_turn", complete)
        assert (await second.prompt(session_id, prompt)).stop_reason == "end_turn"

    asyncio.run(scenario())


def _steering(cached_models, monkeypatch, run_turn):
    """Two prompts to one session with a cancel between them, as a client
    steers: the response to each, and whether the first came while the turn ran."""
    async def scenario():
        monkeypatch.setattr(acp_turn, "run_turn", run_turn)
        agent = acp_agent.Agent([])
        sent = []

        async def session_update(session_id, update):
            sent.append(update)

        agent.on_connect(SimpleNamespace(session_update=session_update))
        session_id = (await agent.new_session(str(cached_models))).session_id
        first = asyncio.create_task(agent.prompt(session_id, [TextContentBlock(text="first")]))
        await asyncio.sleep(0.05)
        await agent.cancel(session_id)
        cancelled = (await first).stop_reason
        running = not agent.sessions[session_id].turn.done()
        second = await agent.prompt(session_id, [TextContentBlock(text="second")])
        return cancelled, running, second.stop_reason, sent

    return asyncio.run(scenario())


def test_a_prompt_after_a_cancel_steers_the_running_turn(cached_models, monkeypatch):
    turns = []

    async def run_turn(*, text, steers, emit, **kwargs):
        turns.append(text)
        while not steers.waiting:
            await asyncio.sleep(0.01)
            await emit({"type": "text", "delta": "working "})
        turns.append([steer["steer"] for steer in steers.waiting])
        steers._taken(1)
        await emit({"type": "text", "delta": "steered"})

    cancelled, running, second, sent = _steering(cached_models, monkeypatch, run_turn)
    assert (cancelled, running, second) == ("cancelled", True, "end_turn")
    assert turns == ["first", ["second"]]
    # What the turn said between the two requests reaches the client with the second.
    assert sent[-1].content.text == "steered"
    assert {update.content.text for update in sent[:-1]} == {"working "}


def test_a_steer_the_turn_did_not_take_is_the_next_turn(cached_models, monkeypatch):
    turns = []

    async def run_turn(*, text, steers, **kwargs):
        turns.append(text)
        if text == "first":
            while not steers.waiting:
                await asyncio.sleep(0.01)

    assert _steering(cached_models, monkeypatch, run_turn)[1:3] == (True, "end_turn")
    assert turns == ["first", "second"]


def test_a_cancel_no_prompt_follows_ends_the_turn(cached_models, monkeypatch):
    monkeypatch.setenv("JS_ACP_STEER_WAIT", "1")
    ended = []

    async def run_turn(**kwargs):
        try:
            await asyncio.Future()
        finally:
            ended.append(True)

    async def scenario():
        monkeypatch.setattr(acp_turn, "run_turn", run_turn)
        agent = acp_agent.Agent([])
        agent.on_connect(SimpleNamespace())
        session_id = (await agent.new_session(str(cached_models))).session_id
        first = asyncio.create_task(agent.prompt(session_id, [TextContentBlock(text="first")]))
        await asyncio.sleep(0.05)
        await agent.cancel(session_id)
        assert (await first).stop_reason == "cancelled"
        assert not ended
        await asyncio.sleep(1.2)
        assert ended

    asyncio.run(scenario())


def test_a_cancel_whose_turn_ended_does_not_end_the_next_turn(cached_models, monkeypatch):
    monkeypatch.setenv("JS_ACP_STEER_WAIT", "1")
    finish_first = asyncio.Event()
    finished = []

    async def run_turn(*, text, **kwargs):
        if text == "first":
            await finish_first.wait()
        else:
            # Past the first turn's cancel timer.
            await asyncio.sleep(1.3)
        finished.append(text)

    async def scenario():
        monkeypatch.setattr(acp_turn, "run_turn", run_turn)
        agent = acp_agent.Agent([])
        agent.on_connect(SimpleNamespace())
        session_id = (await agent.new_session(str(cached_models))).session_id
        first = asyncio.create_task(agent.prompt(session_id, [TextContentBlock(text="first")]))
        await asyncio.sleep(0.05)
        await agent.cancel(session_id)
        assert (await first).stop_reason == "cancelled"
        turn = agent.sessions[session_id].turn
        finish_first.set()
        await asyncio.wait_for(asyncio.shield(turn), 10)
        second = await agent.prompt(session_id, [TextContentBlock(text="second")])
        assert second.stop_reason == "end_turn"

    asyncio.run(scenario())
    assert finished == ["first", "second"]


def test_an_image_in_a_prompt_is_a_file_the_turn_attaches(cached_models, monkeypatch):
    from acp.schema import ImageContentBlock

    seen = []

    async def run_turn(*, text, files, **kwargs):
        seen.append((text, [open(path, "rb").read() for path in files]))

    async def scenario():
        monkeypatch.setattr(acp_turn, "run_turn", run_turn)
        agent = acp_agent.Agent([])
        agent.on_connect(SimpleNamespace())
        assert (await agent.initialize(1)).agent_capabilities.prompt_capabilities.image
        session_id = (await agent.new_session(str(cached_models))).session_id
        image = ImageContentBlock(type="image", data="aW1hZ2U=", mime_type="image/png")
        await agent.prompt(session_id, [TextContentBlock(text="look"), image])
        await agent.prompt(session_id, [image])

    asyncio.run(scenario())
    assert seen == [("look", [b"image"]), ("", [b"image"])]

