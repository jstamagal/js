from pathlib import Path

import ai
import ai.types.usage
import pytest

from js import cli, memory, persona, runtime, session_store
from js.config import from_env
from js.model_client import ModelStreamResult
from js.session_catalog import last_stamp
from js.toolkit import ToolContext
from js.toolkit.meta import task
from repl_driver import run_async, run_blocking


def _agent(tmp_path, monkeypatch, files):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JS_AGENT", "voice")
    monkeypatch.setenv("JS_MODEL", "offline-test-model")
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    directory = tmp_path / ".js" / "agents" / "voice"
    directory.mkdir(parents=True)
    (directory / "00-role.md").write_text("SYSTEM", encoding="utf-8")
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


def test_message_keywords_are_separate_from_ordinary_prompt_files(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {
        "1-user.md": "USER",
        "1-agent.md": "ASSISTANT",
        "2-benchmark.md": "BENCHMARK",
        "02-ape.md": "APE",
    })
    source = tmp_path / "troop.md"
    source.write_text("TROOP", encoding="utf-8")
    (directory / "01-troop.md").symlink_to(source)
    spec = persona.load_prompt_spec(directory)
    assert spec.system == "SYSTEM\n\nTROOP\n\nAPE\n"


def test_pairs_seed_exact_contents_and_agent_only_files_seed_assistant(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {
        "01-user.md": "  user\r\n\r\n",
        "01-agent.md": "---\nexample: literal\n---\n  assistant\n\n",
        "02-agent.md": "arr matey\n",
    })
    requests = _model(monkeypatch, ["done"])
    assert cli._run_prompt("now", session="seeded") == 0
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
    assert cli._run_prompt("now", session="seeded") == 0
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
    assert cli._run_prompt("first", session="seeded") == 0
    assert cli._run_prompt("second", session="seeded") == 0
    assert len(requests) == 2
    saved = memory.load_replay_messages(_saved(tmp_path))
    assert [message["content"] for message in saved].count("example") == 1
    assert ("user", "example") in requests[1]
    assert ("assistant", "example reply") in requests[1]


@pytest.mark.parametrize("blocking", [True, False])
def test_repl_initializes_before_the_first_typed_message(tmp_path, monkeypatch, blocking):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    cfg = from_env(session="seeded")
    requests = _model(monkeypatch, ["opening reply", "typed reply"])
    if blocking:
        run_blocking(cfg, ["typed"])
    else:
        run_async(monkeypatch, cfg, ["typed"])
    assert [request[-1] for request in requests] == [("user", "opening"), ("user", "typed")]
    assert [message["content"] for message in memory.load_replay_messages(cfg.session_file)] == [
        "opening", "opening reply", "typed", "typed reply",
    ]


def test_project_message_files_survive_lower_layer_manifest_fallback(tmp_path):
    repo, global_root, project = (tmp_path / name for name in ("repo", "global", "project"))
    for root in (repo, project):
        (root / "voice").mkdir(parents=True)
    (repo / "voice" / "agent.yaml").write_text("model: lower-model\n", encoding="utf-8")
    (repo / "voice" / "01-user.md").write_text("LOWER", encoding="utf-8")
    (project / "voice" / "01-user.md").write_text("PROJECT", encoding="utf-8")
    spec = persona.load_agent_prompt_spec(
        "voice", repo_prompts_root=repo, global_agents_root=global_root, project_agents_root=project,
    )
    assert spec.model == "lower-model"
    assert [exchange.user for exchange in spec.exchanges] == ["PROJECT"]


def test_duplicate_numeric_role_is_an_error(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {"1-user.md": "one", "01-user.md": "another"})
    with pytest.raises(ValueError):
        persona.load_prompt_spec(directory)


def test_failed_startup_persists_its_prompt_without_sending_the_cli_prompt(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    requests = _model(monkeypatch, [RuntimeError("offline failure")])
    assert cli._run_prompt("now", session="seeded") == 1
    assert requests[0][-1] == ("user", "opening")
    assert [message["content"] for message in memory.load_replay_messages(_saved(tmp_path))] == ["opening"]


def test_task_agents_initialize_their_own_session(tmp_path, monkeypatch):
    directory = _agent(tmp_path, monkeypatch, {})
    worker = directory.parent / "worker"
    worker.mkdir()
    (worker / "01-user.md").write_text("%%CURRENT_SESSION_AGENT%%", encoding="utf-8")
    (worker / "01-agent.md").write_text("%%CURRENT_SESSION%%", encoding="utf-8")
    cfg = from_env(session="seeded")
    context = ToolContext(cwd=tmp_path)
    context.config = cfg
    requests = _model(monkeypatch, ["done"])
    result = task(["work"], agent_id="worker", context=context)
    assert "done" in result
    [child_session] = list(cfg.session_file.with_suffix("").glob("*.jsonl"))
    assert requests[0][1:3] == [("user", "worker"), ("assistant", child_session.stem)]
    assert [message["content"] for message in memory.load_replay_messages(child_session)][:2] == ["worker", child_session.stem]


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
    monkeypatch.setattr(cli, "PromptSession", lambda **kwargs: pytest.fail("entered REPL"))
    assert cli.main([]) == 2


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


def test_project_agent_without_benchmarks_shadows_global_benchmarks(tmp_path, monkeypatch):
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
        assert cli._run_prompt("typed prompt", session="seeded") == 0
    logs = (tmp_path / "transcript" / "seeded.log").read_text(encoding="utf-8")
    assert logs.index("opening prompt") < logs.index("opening reply") < logs.index("typed prompt") < logs.index("typed reply")


def test_blocking_repl_shows_the_generated_startup_reply(tmp_path, monkeypatch, capsys):
    _agent(tmp_path, monkeypatch, {"01-user.md": "opening"})
    _model(monkeypatch, ["VISIBLE_OPENING_REPLY"])
    run_blocking(from_env(session="seeded"), [])
    assert "VISIBLE_OPENING_REPLY" in capsys.readouterr().out


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
    assert cli._run_prompt("now", session="seeded", reasoning="high") == 1
    assert last_stamp(_saved(tmp_path))["reasoning"] == "high"
