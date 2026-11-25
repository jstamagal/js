import json
from pathlib import Path

import ai
import ai.types.usage
import pytest
from repl_driver import run_async
from turn_driver import js

from js import cli, memory, persona, runtime, screen, session_store
from js.config import from_env
from js.model_client import ModelStreamResult
from js.session_catalog import last_stamp
from js.toolkit import ToolContext
from js.toolkit.subagents import task


def _agent(tmp_path, monkeypatch, files):
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JS_DEFAULT_AGENT", "voice")
    monkeypatch.setenv("JS_MODEL", "offline-test-model")
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    directory = tmp_path / ".js" / "agents" / "voice"
    directory.mkdir(parents=True)
    (directory / "00-prompt.md").write_text("SYSTEM", encoding="utf-8")
    for name, content in files.items():
        (directory / name).write_bytes(content.encode("utf-8"))
    return directory


def _model(monkeypatch, answers):
    answers = iter(answers)
    requests = []

    def stream(**kwargs):
        requests.append([
            (message.role, "".join(part.text for part in message.parts if part.kind == "text"))
            for message in kwargs["messages"]
        ])
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        kwargs["on_text"](answer)
        return ModelStreamResult(
            text=answer, tool_calls=[], reasoning="",
            usage=ai.types.usage.Usage(input_tokens=0, output_tokens=1),
            finish_reason="stop", assistant_message=ai.assistant_message(answer),
        )

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    return requests


def _saved(tmp_path):
    return session_store.folder_for(tmp_path) / "seeded.jsonl"


def test_only_keyword_files_load_and_other_markdown_is_not_read(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {
        "1-user.md": "USER",
        "1-agent.md": "ASSISTANT",
        "2-benchmark.md": "BENCHMARK",
        "02-env.md": "ENV",
        "README.md": "README",
        "CHANGELOG.md": "CHANGELOG",
        "03-notes.md": "NOTES",
    })
    source = tmp_path / "troop.md"
    source.write_text("TROOP", encoding="utf-8")
    (directory / "01-prompt.md").symlink_to(source)
    spec = persona.load_prompt_spec(directory)
    assert spec.system == "SYSTEM\n\nTROOP\n\nENV\n"


def test_cli_selects_parent_and_child_prompts_independently(tmp_path, monkeypatch):
    parent = _agent(tmp_path, monkeypatch, {})
    child = parent / "workflow-creator"
    child.mkdir()
    (child / "01-prompt.md").write_text("CHILD %%CURRENT_SESSION_AGENT%%")
    requests = _model(monkeypatch, ["first", "second"])

    for agent in ("voice", "voice:workflow-creator"):
        assert js(monkeypatch, "-a", agent, "--no-save", "-p", "hello") == 0
    assert [request[0] for request in requests] == [
        ("system", "SYSTEM\n"), ("system", "CHILD voice:workflow-creator\n"),
    ]


def test_env_files_can_be_left_out_and_prompt_files_replaced(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {"01-prompt.md": "MORE", "02-env.md": "ENV"})
    assert persona.load_prompt_spec(directory, env=False).system == "SYSTEM\n\nMORE\n"
    assert persona.load_prompt_spec(directory, prompt="OTHER").system == "OTHER\n\nENV\n"


def test_pairs_seed_exact_contents_and_agent_only_files_seed_assistant(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {
        "01-user.md": "  user\r\n\r\n",
        "01-agent.md": "---\nexample: literal\n---\n  assistant\n\n",
        "02-agent.md": "arr matey\n",
    })
    requests = _model(monkeypatch, ["done"])
    assert js(monkeypatch, "--session", "seeded", "-p", "now") == 0
    assert len(requests) == 1
    assert requests[0][1:4] == [
        ("user", "  user\r\n\r\n"),
        ("assistant", "---\nexample: literal\n---\n  assistant\n\n"),
        ("assistant", "arr matey\n"),
    ]
    saved = memory.load_replay_messages(_saved(tmp_path))
    assert [(message["role"], message["content"]) for message in saved[:3]] == requests[0][1:4]


def test_user_files_run_numerically_and_expand_after_previous_reply_is_saved(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {
        "1-user.md": "I want you to talk like a pirate",
        "2-user.md": "!{sh grep -qw 'arr' '%%CURRENT_SESSION_FULLPATH%%' && printf ahoy || printf 'are ye not a pirate matey'}",
        "10-user.md": "third",
    })
    requests = _model(monkeypatch, ["arr matey", "aye", "third reply", "done"])
    assert js(monkeypatch, "--session", "seeded", "-p", "now") == 0
    assert [request[-1] for request in requests] == [
        ("user", "I want you to talk like a pirate"),
        ("user", "ahoy"),
        ("user", "third"),
        ("user", "now"),
    ]
    assert [message["content"] for message in memory.load_replay_messages(_saved(tmp_path))] == [
        "I want you to talk like a pirate", "arr matey", "ahoy", "aye", "third", "third reply", "now", "done",
    ]


def test_resume_keeps_seeded_history_without_duplicating_it(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-user.md": "example", "01-agent.md": "example reply"})
    requests = _model(monkeypatch, ["first reply", "second reply"])
    assert js(monkeypatch, "--session", "seeded", "-p", "first") == 0
    assert js(monkeypatch, "--session", "seeded", "-p", "second") == 0
    assert len(requests) == 2
    saved = memory.load_replay_messages(_saved(tmp_path))
    assert [message["content"] for message in saved].count("example") == 1
    assert ("user", "example") in requests[1]
    assert ("assistant", "example reply") in requests[1]


def test_headless_resume_keeps_the_original_expanded_system_prompt(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    prompt = directory / "00-prompt.md"
    prompt.write_text("SYSTEM {{SESSION_CLOCK}}", encoding="utf-8")
    requests = _model(monkeypatch, ["opening reply", "first reply", "second reply"])
    monkeypatch.setenv("SESSION_CLOCK", "first")
    assert js(monkeypatch, "--session", "seeded", "-p", "first") == 0
    monkeypatch.setenv("SESSION_CLOCK", "second")
    prompt.write_text("CHANGED SYSTEM {{SESSION_CLOCK}}", encoding="utf-8")
    assert js(monkeypatch, "--session", "seeded", "-p", "second") == 0

    assert [request[0] for request in requests] == [("system", "SYSTEM first\n")] * 3
    assert memory.load_system_prompt(_saved(tmp_path)) == "SYSTEM first\n"


def test_headless_no_save_resume_leaves_the_saved_session_untouched(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})
    requests = _model(monkeypatch, ["saved reply", "temporary reply"])
    assert js(monkeypatch, "--session", "seeded", "-p", "saved question") == 0
    session = _saved(tmp_path)
    before = session.read_bytes()

    assert js(monkeypatch, "--session", "seeded", "--no-save", "-p", "temporary question") == 0

    assert ("assistant", "saved reply") in requests[1]
    assert session.read_bytes() == before


def test_empty_headless_reply_does_not_reprint_a_previous_answer(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {})
    _model(monkeypatch, ["previous answer", ""])
    assert js(monkeypatch, "--session", "seeded", "-p", "first question") == 0
    capsys.readouterr()

    rc = js(monkeypatch, "--session", "seeded", "-p", "second question")

    assert rc == 1
    assert "previous answer" not in capsys.readouterr().out


def test_repl_initializes_before_the_first_typed_message(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    cfg = from_env(session="seeded")
    requests = _model(monkeypatch, ["opening reply", "typed reply"])
    run_async(monkeypatch, cfg, ["typed"])
    assert [request[-1] for request in requests] == [("user", "opening"), ("user", "typed")]
    assert [message["content"] for message in memory.load_replay_messages(cfg.session_file)] == [
        "opening", "opening reply", "typed", "typed reply",
    ]


def test_a_project_agent_replaces_the_lower_one_manifest_and_all(tmp_path):
    repo, global_root, project = (tmp_path / name for name in ("repo", "global", "project"))
    for root in (repo, project):
        (root / "voice").mkdir(parents=True)
    (repo / "voice" / "agent.yaml").write_text("model: lower-model\n", encoding="utf-8")
    (repo / "voice" / "01-user.md").write_text("LOWER", encoding="utf-8")
    (project / "voice" / "01-user.md").write_text("PROJECT", encoding="utf-8")
    spec = persona.load_agent_prompt_spec("voice", (repo, global_root, project))
    assert spec.model == ""
    assert [exchange.user for exchange in spec.exchanges] == ["PROJECT"]


def test_duplicate_numeric_role_is_an_error(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {"1-user.md": "one", "01-user.md": "another"})
    with pytest.raises(ValueError):
        persona.load_prompt_spec(directory)


def test_failed_startup_persists_its_prompt_without_sending_the_cli_prompt(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    requests = _model(monkeypatch, [RuntimeError("offline failure")])
    assert js(monkeypatch, "--session", "seeded", "-p", "now") == 1
    assert requests[0][-1] == ("user", "opening")
    assert [message["content"] for message in memory.load_replay_messages(_saved(tmp_path))] == ["opening"]


@pytest.mark.parametrize("agent_id", ["worker", "team:worker"])
def test_task_agents_initialize_their_own_session(tmp_path, monkeypatch, agent_id):
    directory = _agent(tmp_path, monkeypatch, {})
    worker = directory.parent.joinpath(*agent_id.split(":"))
    worker.mkdir(parents=True)
    (worker / "01-user.md").write_text("%%CURRENT_SESSION_AGENT%%", encoding="utf-8")
    (worker / "01-agent.md").write_text("%%CURRENT_SESSION%%", encoding="utf-8")
    cfg = from_env(session="seeded")
    context = ToolContext(cwd=tmp_path)
    context.config = cfg
    requests = _model(monkeypatch, ["done"])
    result = task(["work"], agent_id=agent_id, context=context)
    assert "done" in result
    [child_session] = list(cfg.session_file.with_suffix("").glob("*.jsonl"))
    assert requests[0][1:3] == [("user", agent_id), ("assistant", child_session.stem)]
    assert [message["content"] for message in memory.load_replay_messages(child_session)][:2] == [agent_id, child_session.stem]


def test_benchmarks_use_examples_in_each_fresh_context(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {
        "01-user.md": "example", "01-agent.md": "example reply",
        "02-benchmark.md": "benchmark one", "03-benchmark.md": "benchmark two",
    })
    requests = _model(monkeypatch, ["first", "second"])
    assert cli.main(["--bench", "voice", "-q"]) == 0
    assert requests == [
        [("system", "SYSTEM\n"), ("user", "example"), ("assistant", "example reply"), ("user", "benchmark one")],
        [("system", "SYSTEM\n"), ("user", "example"), ("assistant", "example reply"), ("user", "benchmark two")],
    ]
    assert not list(session_store.folder_for(tmp_path).glob("*.jsonl"))


@pytest.mark.parametrize("args", [[], ["--agent", "voice"]])
def test_benchmark_files_select_bench_mode_without_a_flag_or_prompt(tmp_path, monkeypatch, args):
    _agent(tmp_path, monkeypatch, {"01-benchmark.md": "measure me"})
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    requests = _model(monkeypatch, ["measured"])
    assert cli.main([*args, "-q"]) == 0
    assert requests[0][-1] == ("user", "measure me")
    assert not list(session_store.folder_for(tmp_path).glob("*.jsonl"))


def test_even_an_empty_benchmark_file_prevents_entering_repl(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-benchmark.md": ""})
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(screen, "build_app", lambda **kwargs: pytest.fail("entered REPL"))
    assert cli.main([]) == 2


def test_exchange_files_run_as_a_one_shot_without_a_prompt(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {"01-user.md": "first step", "02-user.md": "last step"})
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(screen, "build_app", lambda **kwargs: pytest.fail("entered REPL"))
    requests = _model(monkeypatch, ["first reply", "last reply"])
    assert cli.main(["--session", "seeded", "-q"]) == 0
    assert [request[-1] for request in requests] == [("user", "first step"), ("user", "last step")]
    assert capsys.readouterr().out.strip() == "last reply"
    assert [message["content"] for message in memory.load_replay_messages(_saved(tmp_path))] == [
        "first step", "first reply", "last step", "last reply",
    ]


def test_exchange_files_with_a_prompt_send_it_after_them_and_exit(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {"01-user.md": "the workflow"})
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(screen, "build_app", lambda **kwargs: pytest.fail("entered REPL"))
    requests = _model(monkeypatch, ["workflow reply", "opinion"])
    assert cli.main(["-p", "your opinion", "-q"]) == 0
    assert [request[-1] for request in requests] == [("user", "the workflow"), ("user", "your opinion")]
    assert capsys.readouterr().out.strip() == "opinion"


def test_resumed_session_with_exchange_files_opens_the_repl(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-user.md": "the workflow"})
    _model(monkeypatch, ["workflow reply"])
    assert js(monkeypatch, "--session", "seeded") == 0
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class EnteredRepl(Exception):
        pass

    def entered(**kwargs):
        raise EnteredRepl
    monkeypatch.setattr(screen, "build_app", entered)
    with pytest.raises(EnteredRepl):
        cli.main(["--session", "seeded"])


def test_benchmark_expansion_happens_after_its_setup(tmp_path, monkeypatch):
    marker = tmp_path / "ready"
    _agent(tmp_path, monkeypatch, {
        "01-user.md": f"!{{sh printf ready > '{marker}'; printf opening}}",
        "02-benchmark.md": f"!{{sh cat '{marker}'}}",
    })
    requests = _model(monkeypatch, ["opening reply", "benchmark reply"])
    assert cli.main(["--bench", "voice", "-q"]) == 0
    assert [request[-1] for request in requests] == [("user", "opening"), ("user", "ready")]


def test_malformed_benchmark_reports_a_load_error(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-benchmark.md": "---\nmax_tokens: [\n---\nproblem"})
    assert cli.main(["--bench", "voice", "-q"]) == 2


def test_global_benchmark_agent_is_automatically_selected(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {"01-benchmark.md": "global benchmark"})
    global_directory = Path.home() / ".js" / "agents" / "voice"
    global_directory.parent.mkdir(parents=True)
    directory.rename(global_directory)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    requests = _model(monkeypatch, ["done"])
    assert cli.main(["-q"]) == 0
    assert requests[0][-1] == ("user", "global benchmark")


def test_project_agent_does_not_read_lower_benchmark_files(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-user.md": "project opening", "01-agent.md": "project reply"})
    global_directory = Path.home() / ".js" / "agents" / "voice"
    global_directory.mkdir(parents=True)
    (global_directory / "01-benchmark.md").write_text("GLOBAL BENCHMARK", encoding="utf-8")
    requests = _model(monkeypatch, ["done"])
    assert cli.main(["-p", "typed", "-q"]) == 0
    assert requests[0][-1] == ("user", "typed")
    assert requests[0][1:3] == [("user", "project opening"), ("assistant", "project reply")]


@pytest.mark.parametrize("repl", [False, True])
def test_transcript_keeps_startup_exchanges_in_conversation_order(tmp_path, monkeypatch, repl):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening prompt"})
    monkeypatch.setenv("JS_TRANSCRIPT_LOG_DIR", str(tmp_path / "transcript"))
    _model(monkeypatch, ["opening reply", "typed reply"])
    if repl:
        run_async(monkeypatch, from_env(session="seeded"), ["typed prompt"])
    else:
        assert js(monkeypatch, "--session", "seeded", "-p", "typed prompt") == 0
    logs = (tmp_path / "transcript" / "seeded.log").read_text(encoding="utf-8")
    assert logs.index("opening prompt") < logs.index("opening reply") < logs.index("typed prompt") < logs.index("typed reply")


def test_repl_shows_the_generated_startup_reply(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    _model(monkeypatch, ["VISIBLE_OPENING_REPLY"])
    run_async(monkeypatch, from_env(session="seeded"), [])
    assert "VISIBLE_OPENING_REPLY" in capsys.readouterr().out


@pytest.mark.parametrize("resume_exchanges", [0, 2])
def test_repl_displays_each_fired_message_and_reply(tmp_path, monkeypatch, capsys, resume_exchanges):
    _agent(tmp_path, monkeypatch, {
        "01-user.md": "first startup prompt",
        "02-user.md": "second startup prompt",
    })
    cfg = from_env(session="seeded")
    cfg.settings["RESUME_EXCHANGES"] = resume_exchanges
    _model(monkeypatch, ["first startup reply", "second startup reply"])

    run_async(monkeypatch, cfg, [])

    output = capsys.readouterr().out
    assert output.index("first startup prompt") < output.index("first startup reply")
    assert output.index("first startup reply") < output.index("second startup prompt")
    assert output.index("second startup prompt") < output.index("second startup reply")
    # Drawn as they happen, not again from the resume view.
    assert output.count("first startup prompt") == 1
    assert output.count("second startup reply") == 1


def test_debug_prompt_mode_shows_each_user_line_ahead_of_its_reply(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {"01-user.md": "startup prompt"})
    _model(monkeypatch, ["startup reply", "typed reply"])
    assert js(monkeypatch, "--session", "seeded", "--debug", "-p", "typed prompt") == 0
    output = capsys.readouterr().out
    assert output.index("startup prompt") < output.index("startup reply")
    assert output.index("startup reply") < output.index("typed prompt")
    assert output.index("typed prompt") < output.index("typed reply")


def test_plain_prompt_mode_prints_only_the_answer(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {"01-user.md": "startup prompt"})
    _model(monkeypatch, ["startup reply", "typed reply"])
    assert js(monkeypatch, "--session", "seeded", "-p", "typed prompt") == 0
    output = capsys.readouterr().out
    assert "typed reply" in output
    assert "typed prompt" not in output
    assert "startup prompt" not in output


def test_benchmarks_never_write_to_a_session_selected_by_environment(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {
        "01-user.md": "opening %%CURRENT_SESSION_FULLPATH%%", "02-benchmark.md": "benchmark",
    })
    cfg = from_env(session="valuable")
    memory.append_message(cfg.session_file, {"role": "user", "content": "valuable question"})
    memory.append_message(cfg.session_file, {"role": "assistant", "content": "valuable reply"})
    before = cfg.session_file.read_bytes()
    monkeypatch.setenv("JS_SESSION", "valuable")
    requests = _model(monkeypatch, ["opening reply", "benchmark reply"])
    assert cli.main(["--bench", "voice", "-q"]) == 0
    assert cfg.session_file.read_bytes() == before
    assert requests[0][-1] == ("user", "opening /dev/null")


def test_startup_reply_records_effective_reasoning_when_the_following_turn_fails(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    _model(monkeypatch, ["opening reply", RuntimeError("following turn failed")])
    assert js(monkeypatch, "--session", "seeded", "--reasoning", "high", "-p", "now") == 1
    assert last_stamp(_saved(tmp_path))["reasoning"] == "high"


def test_seeded_reply_stamp_follows_model_and_provider_overrides(tmp_path, monkeypatch):
    """The stamp names the model the turn runs with, not cfg's, when a caller
    passes `model_override`/`provider_id_override`."""
    import asyncio
    from types import SimpleNamespace

    from js import agent_messages

    directory = _agent(tmp_path, monkeypatch, {"01-user.md": "example", "01-agent.md": "example reply"})
    cfg = from_env(session="seeded")
    spec = persona.load_prompt_spec(directory)
    messages: list[dict] = []
    telemetry = SimpleNamespace(transcript_log=None)
    asyncio.run(agent_messages.initialize(
        cfg, spec, messages, telemetry,
        model_override="other-model", provider_id_override="other-provider",
    ))
    stamp = last_stamp(cfg.session_file)
    assert stamp["model"] == "other-model"
    assert stamp["provider"] == "other-provider"


def _setup_and_two_benchmarks(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {
        "01-user.md": "warm up",
        "02-benchmark.md": "benchmark one", "03-benchmark.md": "benchmark two",
    })
    return _model(monkeypatch, ["warmed", "first", "second"])


def test_benchmark_setup_turn_runs_once_and_every_benchmark_starts_from_it(tmp_path, monkeypatch):
    requests = _setup_and_two_benchmarks(tmp_path, monkeypatch)
    assert cli.main(["--bench", "voice", "-q"]) == 0
    setup = [("system", "SYSTEM\n"), ("user", "warm up")]
    assert requests == [
        setup,
        [*setup, ("assistant", "warmed"), ("user", "benchmark one")],
        [*setup, ("assistant", "warmed"), ("user", "benchmark two")],
    ]


def test_benchmark_stats_count_only_the_benchmark_turn(tmp_path, monkeypatch):
    _setup_and_two_benchmarks(tmp_path, monkeypatch)
    out = tmp_path / "stats.json"
    assert cli.main(["--bench", "voice", "-q", "--stats-json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [row["calls"] for row in payload["benchmarks"]] == [1, 1]
    assert payload["setup"]["calls"] == 1
