"""`js schedule`: a daily job on the systemd user manager or in the crontab,
found again by the listing and by --remove. PATH holds only fake `systemctl`,
`crontab` and `uv`, so nothing reaches this machine's own."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from js import cli, paths, schedule_cli

MARKER = schedule_cli.LAUNCHER_MARKER.decode()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A bin directory that is the whole PATH, a js launcher, and the state
    the fakes keep."""
    fake = tmp_path / "fakebin"
    fake.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("PATH", str(fake))
    monkeypatch.setenv("FAKE_STATE", str(state))
    js = Path.home() / ".local" / "bin" / "js"
    js.parent.mkdir(parents=True)
    js.write_text(f"#!/bin/sh\n{MARKER} in /src.\n")
    uv_dir = tmp_path / "odd place" / "bin"
    uv_dir.mkdir(parents=True)
    _script(uv_dir / "uv", "exit 0")
    monkeypatch.setenv("PATH", f"{fake}:{uv_dir}")
    monkeypatch.setenv("JS_HOME", str(paths.default_home()))
    return {"bin": fake, "state": state, "js": js, "uv_dir": uv_dir}


def _js(*args: str, home: Path | None = None) -> int:
    """`js schedule ...` on `home`, the default home when None."""
    return cli.main(["--home", str(home or paths.default_home()), "schedule", *args])


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def _systemd(machine, *, up: bool = True) -> None:
    _script(machine["bin"] / "systemctl", f"""
printf '%s\\n' "$*" >> "$FAKE_STATE/systemctl.log"
case "$*" in
  *show-environment*) exit {0 if up else 1} ;;
  *NextElapseUSecRealtime*) echo "Sat 2026-11-28 05:04:00 CET" ;;
esac
exit 0""")


def _cron(machine, *, table: bytes | None = None, broken: bool = False) -> Path:
    stored = machine["state"] / "crontab"
    if table is not None:
        stored.write_bytes(table)
    list_failure = 'echo "crontab: cannot read" >&2; exit 1' if broken else \
        'echo "no crontab for someone" >&2; exit 1'
    _script(machine["bin"] / "crontab", f"""
case "$1" in
  -l) if [ -f "$FAKE_STATE/crontab" ]; then /bin/cat "$FAKE_STATE/crontab"; else {list_failure}; fi ;;
  -) /bin/cat > "$FAKE_STATE/crontab"; echo x >> "$FAKE_STATE/writes" ;;
  *) exit 2 ;;
esac""")
    return stored


def _calls(machine) -> list[str]:
    log = machine["state"] / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


def _units() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def test_systemd_schedule_writes_the_pair_and_enables_the_timer(machine):
    _systemd(machine)
    assert _js("rotate", "--at", "04:30") == 0
    service = (_units() / "js-rotate.service").read_text()
    timer = (_units() / "js-rotate.timer").read_text()
    assert f"ExecStart={machine['js']} rotate\n" in service
    assert str(machine["uv_dir"]) in service
    assert "OnCalendar=*-*-* 04:30:00" in timer
    assert "Persistent=true" in timer
    calls = _calls(machine)
    assert "--user enable js-rotate.timer" in calls
    assert "--user restart js-rotate.timer" in calls


def test_systemd_schedule_twice_leaves_one_pair(machine):
    _systemd(machine)
    assert _js("rotate") == 0
    assert _js("rotate", "--at", "06:00") == 0
    assert sorted(p.name for p in _units().iterdir()) == ["js-rotate.service", "js-rotate.timer"]
    assert "06:00:00" in (_units() / "js-rotate.timer").read_text()


def test_systemd_listing_and_remove(machine, capsys):
    _systemd(machine)
    assert _js("rotate") == 0
    capsys.readouterr()
    assert _js() == 0
    out = capsys.readouterr().out
    assert "rotate" in out and "systemd" in out and "2026-11-28 05:04" in out
    assert _js("--remove", "rotate") == 0
    assert not list(_units().iterdir())
    assert "--user disable --now js-rotate.timer" in _calls(machine)
    capsys.readouterr()
    assert _js() == 0
    assert "rotate" not in capsys.readouterr().out


def test_a_hand_made_pair_is_adopted(machine):
    _systemd(machine)
    _units().mkdir(parents=True)
    (_units() / "js-rotate.timer").write_text("[Timer]\nOnCalendar=*-*-* 05:00:00\n")
    (_units() / "js-rotate.service").write_text("[Service]\nExecStart=%h/.local/bin/js --rotate\n")
    [(job, unit)] = schedule_cli.systemd_jobs()
    assert (job.name, unit, job.home) == ("rotate", "js-rotate", paths.default_home().resolve())
    assert _js("rotate") == 0
    assert sorted(p.name for p in _units().iterdir()) == ["js-rotate.service", "js-rotate.timer"]
    assert _js("--remove", "rotate") == 0
    assert not list(_units().iterdir())


def test_another_home_has_its_own_units_and_passes_home(machine, tmp_path):
    _systemd(machine)
    other = tmp_path / "second home"
    assert _js("rotate") == 0
    assert _js("rotate", home=other) == 0
    timers = sorted(p.name for p in _units().glob("*.timer"))
    assert len(timers) == 2 and "js-rotate.timer" in timers
    [service] = [p for p in _units().glob("js-rotate-*.service")]
    assert f'"{other}"' in service.read_text()
    assert "--home" not in (_units() / "js-rotate.service").read_text()
    homes = {job.home for job, _ in schedule_cli.systemd_jobs()}
    assert homes == {paths.default_home().resolve(), other.resolve()}
    assert _js("--remove", "rotate", home=other) == 0
    assert sorted(p.name for p in _units().iterdir()) == ["js-rotate.service", "js-rotate.timer"]


def test_systemd_wins_over_cron(machine):
    _systemd(machine)
    stored = _cron(machine)
    assert _js("rotate") == 0
    assert (_units() / "js-rotate.timer").exists()
    assert not stored.exists()


USER_TABLE = (b"# my jobs\r\n"
              b"MAILTO=me\n"
              b"0 3 * * * date +\\%F >> ~/x.log\n"
              b"15 4 * * * /home/me/.local/bin/js rotate\n"
              b"\xff\xfe odd bytes\n"
              b"@reboot sleep 1")


def test_cron_schedule_keeps_other_lines_byte_for_byte(machine):
    stored = _cron(machine, table=USER_TABLE)
    _systemd(machine, up=False)
    assert _js("rotate", "--at", "5:07") == 0
    after = stored.read_bytes()
    assert after.startswith(USER_TABLE + b"\n")
    [line] = after[len(USER_TABLE) + 1:].splitlines()
    assert line.startswith(b"7 5 * * * ")
    assert shlex.quote(str(machine["js"])).encode() + b" rotate" in line
    assert str(machine["uv_dir"]).encode() in line
    assert _js("rotate") == 0
    assert _js("--remove", "rotate") == 0
    assert stored.read_bytes() == USER_TABLE + b"\n"


def test_cron_schedule_is_idempotent_and_listed(machine, capsys):
    stored = _cron(machine)
    assert _js("rotate") == 0
    first = stored.read_bytes()
    assert _js("rotate") == 0
    assert stored.read_bytes() == first
    assert (machine["state"] / "writes").read_text().count("x") == 1
    assert (paths.default_home() / "logs" / "schedule").is_dir()
    capsys.readouterr()
    assert _js() == 0
    out = capsys.readouterr().out
    assert "rotate" in out and "cron" in out and "05:00" in out


def test_cron_remove_when_nothing_is_there_writes_nothing(machine):
    stored = _cron(machine, table=USER_TABLE)
    assert _js("--remove", "rotate") == 0
    assert stored.read_bytes() == USER_TABLE
    assert not (machine["state"] / "writes").exists()


def test_cron_carries_home_and_keeps_homes_apart(machine, tmp_path):
    stored = _cron(machine)
    other = tmp_path / "home%2"
    assert _js("rotate") == 0
    assert _js("rotate", home=other) == 0
    lines = stored.read_bytes().splitlines()
    assert len(lines) == 2
    assert b"--home" not in lines[0]
    assert b"--home " + str(other).replace("%", "\\%").encode() + b" rotate" in lines[1]
    assert {job.home for job, _ in schedule_cli.cron_jobs()} == {paths.default_home().resolve(), other.resolve()}
    assert _js("--remove", "rotate") == 0
    [left] = stored.read_bytes().splitlines()
    assert b"--home" in left


def test_unreadable_crontab_is_never_written(machine, capsys):
    stored = _cron(machine, broken=True)
    assert _js("rotate") == 1
    assert not stored.exists()
    assert "rotate" in capsys.readouterr().out


def test_neither_backend_prints_the_line_and_fails(machine, capsys):
    assert _js("rotate", "--at", "03:15") == 1
    out = capsys.readouterr().out.strip()
    assert out.startswith("15 3 * * * ")
    assert out.endswith(schedule_cli.Job("rotate", paths.default_home().resolve()).cron_tag())


def test_cleanup_is_refused_without_the_acknowledgement(machine, capsys):
    _systemd(machine)
    stored = _cron(machine)
    assert _js("cleanup") != 0
    assert capsys.readouterr().err.strip()
    assert not _units().exists()
    assert not stored.exists()
    assert _calls(machine) == []


def test_cleanup_with_the_acknowledgement(machine):
    _systemd(machine)
    assert _js("cleanup", "--allow-unattended-cleanup") == 0
    assert f"ExecStart={machine['js']} cleanup\n" in (_units() / "js-cleanup.service").read_text()
    assert _js("--remove", "cleanup") == 0
    assert not list(_units().iterdir())


def test_a_home_template_replaces_the_stock_one(machine):
    _systemd(machine)
    (paths.default_home() / "schedule").mkdir(parents=True)
    (paths.default_home() / "schedule" / "timer").write_text("[Timer]\nOnCalendar=weekly ${AT}\n")
    assert _js("rotate") == 0
    assert (_units() / "js-rotate.timer").read_text() == "[Timer]\nOnCalendar=weekly 05:00\n"


def test_an_empty_home_template_is_refused(machine):
    _systemd(machine)
    (paths.default_home() / "schedule").mkdir(parents=True)
    (paths.default_home() / "schedule" / "service").write_text("")
    assert _js("rotate") == 1
    assert not (_units() / "js-rotate.service").exists()


def test_a_bad_time_is_refused(machine):
    _systemd(machine)
    assert _js("rotate", "--at", "25:00") == 1
    assert not _units().exists()


def test_the_stock_templates_ship():
    for name in ("service", "timer", "cron"):
        assert (paths.defaults_dir() / "schedule" / name).read_text().strip()
    assert os.access(paths.defaults_dir() / "schedule", os.R_OK)
