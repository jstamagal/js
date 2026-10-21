"""Parts hand structured output to a caller's sink without writing to stdio."""

from types import SimpleNamespace

from js import output, stream_transport


def test_network_fallback_emits_an_event_before_rendering(capsys):
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        stream_transport.say_or_print(2, "catalog")
    assert len(events) == 1
    assert events[0].name == "network"
    assert dict(events[0].fields) == {"level": 2, "line": "catalog", "fallback": True}
    assert capsys.readouterr() == ("", "")


def test_retry_emits_its_wait_and_failure_without_rendering(capsys):
    from js import retry

    events = []
    failure = RuntimeError("busy")
    with output.using_sink(SimpleNamespace(emit=events.append)):
        retry.announce(1, retry.Budget(attempts=2, max_wait=0), 12, failure)
    assert [event.name for event in events] == ["retry_wait"]
    fields = dict(events[0].fields)
    assert (fields["n"], fields["attempts"], fields["delay"], fields["error"]) == (1, 2, 12, failure)
    assert capsys.readouterr() == ("", "")


def test_compaction_flight_failure_goes_to_the_callback(monkeypatch, capsys):
    import pytest

    from js import compaction

    failure = OSError("read only")
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(compaction, "CompactionFlight", unavailable)
    events = []
    with pytest.raises(OSError):
        compaction._clearing_flight([], cfg=None, system="", trigger={}, flight_data={},
                                    log_dir=None, emit=lambda name, **fields: events.append((name, fields)))
    assert events[0][0] == "compaction_output"
    notice = events[0][1]["output"]
    assert dict(notice.fields)["fields"]["error"] is failure
    assert capsys.readouterr() == ("", "")


def test_flight_autolog_failure_goes_to_its_event_callback(tmp_path, capsys):
    from js.compaction_flight import CompactionFlight
    from test_config_compaction_layers import _compact_test_cfg

    blocked = tmp_path / "blocked"
    blocked.write_text("file")
    cfg = _compact_test_cfg(tmp_path, {"REQUEST_LOG_DIR": str(blocked)})
    events = []
    flight = CompactionFlight(cfg, "system", [], trigger={}, forced=True, focus="", preserve_from=None,
                              details={}, directory=tmp_path / "flights",
                              emit=lambda name, **fields: events.append((name, fields)))
    flight.close()
    assert events[0][0] == "compaction_output"
    assert isinstance(dict(events[0][1]["output"].fields)["fields"]["error"], OSError)
    assert capsys.readouterr() == ("", "")


def test_agent_registry_warning_is_an_event(tmp_path, capsys):
    from js.toolkit.registry import _agent_tools

    agent = tmp_path / "read"
    agent.mkdir()
    (agent / "prompt.md").write_text("prompt")
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        assert _agent_tools(tmp_path, {"read"}) == ()
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["agent"] == "read"
    assert capsys.readouterr() == ("", "")


def test_unmatched_policy_entry_is_an_event(capsys):
    from js.toolkit import policy

    rules = policy.expand(["absent:eager"], policy.ToolsConfig(), "test")
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        policy.warn_unmatched([], rules)
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["entry"] == rules[0].label
    assert capsys.readouterr() == ("", "")


def test_description_warning_is_an_event(monkeypatch, capsys):
    from js.toolkit import descriptions

    monkeypatch.setattr(descriptions, "_WARNED", set())
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        descriptions._warn_once("invalid marker")
        descriptions._warn_once("invalid marker")
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["problem"] == "invalid marker"
    assert capsys.readouterr() == ("", "")


def test_fetch_fallback_warning_is_an_event(monkeypatch, tmp_path, capsys):
    from js import tool_binaries
    from js.toolkit import ToolContext, process_net

    monkeypatch.setattr(tool_binaries, "_URLLIB_FALLBACK_REPORTED", set())
    monkeypatch.setattr(process_net, "resolve_binary", lambda name: None)
    def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(process_net.urllib.request, "urlopen", unavailable)
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        process_net.fetch("https://example.invalid/file", save="file", context=ToolContext(cwd=tmp_path))
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["purpose"] == "fetch(save=...)"
    assert capsys.readouterr() == ("", "")


def test_kernel_note_is_an_event(capsys):
    from js.toolkit import kernel

    events = []
    context = SimpleNamespace(kernel_render_max_lines=5)
    with output.using_sink(SimpleNamespace(emit=events.append)):
        kernel.render_event(context, "normal", "saved")
    assert [event.name for event in events] == ["kernel_note"]
    assert dict(events[0].fields)["content"] == "saved"
    assert capsys.readouterr() == ("", "")


def test_user_echo_is_an_event(capsys):
    from js import display

    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        display.echo_user("hello")
    assert [event.name for event in events] == ["user_echo"]
    assert dict(events[0].fields)["text"] == "hello"
    assert capsys.readouterr() == ("", "")


def test_ex_write_reports_through_an_event(tmp_path, capsys):
    import asyncio

    from js import exline

    events = []
    path = tmp_path / "buffer"
    with output.using_sink(SimpleNamespace(emit=events.append)):
        asyncio.run(exline.run_ex(f"w {path}", SimpleNamespace(text="saved"),
                                  is_command=lambda verb: False, dispatch=None))
    assert path.read_text() == "saved\n"
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["path"] == path
    assert capsys.readouterr() == ("", "")


def test_clipboard_failure_is_an_event(capsys):
    import asyncio

    from js import clipimage, messages

    events = []
    error = clipimage.ClipboardError(messages.FAILED, error="clipboard unavailable")
    with output.using_sink(SimpleNamespace(emit=events.append)):
        asyncio.run(clipimage._say(SimpleNamespace(full_screen=True), error))
    assert len(events) == 1
    assert dict(events[0].fields)["fields"] == error.fields
    assert capsys.readouterr() == ("", "")


def test_invalid_environment_setting_emits_a_warning(capsys):
    from js import settings

    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        assert settings.env_value(settings.SPEC_BY_KEY["RETRY_COUNT_MAX"],
                                  {"JS_RETRY_COUNT_MAX": "invalid"}, warn=True) is None
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["name"] == "JS_RETRY_COUNT_MAX"
    assert capsys.readouterr() == ("", "")


def test_invalid_login_section_emits_a_warning(capsys):
    from js import logins

    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        store = logins._parse_store(None, {"broken": 1})
    assert store.quarantined == {"broken": 1}
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["name"] == "logins.toml [broken]"
    assert capsys.readouterr() == ("", "")


def test_codex_refresh_save_failure_is_an_event(monkeypatch, capsys):
    from dataclasses import replace

    from js import codex_auth, logins

    stored = logins.Login(provider_id="openai-codex", codex_refresh_token="old")
    logins.save_login(stored)
    def unavailable(login):
        raise OSError("read only")

    monkeypatch.setattr(logins, "save_login", unavailable)
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        assert codex_auth.save_refreshed_login(replace(stored, codex_refresh_token="new"),
                                               derived_from=stored) is False
    assert len(events) == 1
    assert isinstance(dict(events[0].fields)["fields"]["error"], OSError)
    assert capsys.readouterr() == ("", "")


def test_xai_refresh_save_failure_is_an_event(monkeypatch, capsys):
    from dataclasses import replace

    from js import logins, xai_auth

    stored = logins.Login(provider_id="xai-oauth", xai_refresh_token="old")
    logins.save_login(stored)
    def unavailable(login):
        raise OSError("read only")

    monkeypatch.setattr(logins, "save_login", unavailable)
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        assert xai_auth.save_refreshed_login(replace(stored, xai_refresh_token="new"),
                                             derived_from=stored) is False
    assert len(events) == 1
    assert isinstance(dict(events[0].fields)["fields"]["error"], OSError)
    assert capsys.readouterr() == ("", "")


def test_download_fallback_helper_emits_once(monkeypatch, capsys):
    from js import tool_binaries

    monkeypatch.setattr(tool_binaries, "_URLLIB_FALLBACK_REPORTED", set())
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        tool_binaries.warn_urllib_fallback("installer")
        tool_binaries.warn_urllib_fallback("installer")
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["purpose"] == "installer"
    assert capsys.readouterr() == ("", "")


def test_skipped_session_records_emit_a_warning(tmp_path, capsys):
    import json

    from js import memory

    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"kind": "message", "version": memory.SCHEMA_VERSION + 1,
                               "message": {"role": "user", "content": "future"}}) + "\n")
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        assert memory.load_replay_messages(path) == []
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["path"] == path
    assert capsys.readouterr() == ("", "")


def test_ignored_sampling_value_emits_a_warning(tmp_path, capsys):
    from js import persona

    events = []
    path = tmp_path / "agent.yaml"
    with output.using_sink(SimpleNamespace(emit=events.append)):
        assert persona._coerce_sampling(path, {"temperature": -1}) == {}
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["name"] == f"sampling.temperature in {path}"
    assert capsys.readouterr() == ("", "")


def test_skill_discovery_warning_is_an_event(monkeypatch, capsys):
    from js import skills

    monkeypatch.setattr(skills, "_WARNED", set())
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        skills._warn_once("invalid skill")
        skills._warn_once("invalid skill")
    assert len(events) == 1
    assert dict(events[0].fields)["fields"]["error"] == "invalid skill"
    assert capsys.readouterr() == ("", "")


def test_jsrc_migration_notice_is_an_event(tmp_path, capsys):
    from js import rename

    path = tmp_path / "jsrc"
    path.write_text("set model.id fixture\n")
    events = []
    with output.using_sink(SimpleNamespace(emit=events.append)):
        assert rename.migrate_jsrc_file(path)
    assert [event.name for event in events] == ["migration_notice"]
    assert dict(events[0].fields)["fields"]["path"] == path
    assert capsys.readouterr() == ("", "")


def test_running_shell_follows_its_output_tail(monkeypatch):
    from js import console
    from js.toolkit import process_net
    asked = []
    opened = console._OpenCall("shell", {"command": "sleep 9"})

    def live_tail(context, command, rows):
        asked.append(rows)
        opened.done.set()
        return None

    monkeypatch.setattr(process_net, "live_tail", live_tail)
    monkeypatch.setattr(console, "_LIVE_AFTER_S", 0)
    monkeypatch.setattr(console, "_LIVE_TICK_S", 0)
    console.ToolTrace._follow(None, opened, None, 1)
    assert asked and asked[0] > 0
