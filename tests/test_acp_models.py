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
