"""`js schedule`: daily jobs for the current user that run `js rotate` or
`js cleanup`, installed only when asked for by name.

    js schedule                     what js has scheduled here, and when each next runs
    js schedule rotate [--at HH:MM] run `js rotate` every day
    js schedule cleanup --allow-unattended-cleanup [--at HH:MM]
    js schedule --remove JOB        take the job away

The backend is the first that works here: a systemd user manager
(`systemctl --user`), then `crontab`. A job already scheduled stays on the
backend that holds it. With neither, the cron line to add by hand is printed
and the exit is 1.

What gets installed comes from `defaults/schedule/` through the setup layers
(`paths.find`), so a home `schedule/service`, `schedule/timer` or
`schedule/cron` replaces the stock one. The templates are filled with
`${JOB}`, `${COMMAND}`, `${PATH}`, `${AT}`, `${HOUR}`, `${MINUTE}` and
`${LOG}`; the values are escaped for the file they go into.

A job is one (job, home) pair. Its identity is added by this module, not by
the templates: under systemd the unit names, `js-<job>` for the default home
and `js-<job>-<hash>` for another one, and a first line naming the home in the
service; under cron the tag ` # js schedule <job> <home>` at the end of the
line. Every other line of the crontab is kept byte for byte.

The job runs js by absolute path, the launcher `just install` writes, with
`--home` when the home is not the default one. Its PATH holds the
launcher's directory and the directory of the `uv` it starts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from string import Template

from . import messages as msgs, paths

JOBS = ("rotate", "cleanup")
DEFAULT_AT = "05:00"
LAUNCHER_MARKER = b"# js launcher written by just install"
CRON_TAG = "# js schedule"
_BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
_AT = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_SERVICE_HOME = "# js schedule {job} home="


class ScheduleError(Exception):
    """A refusal, with the message that says why."""


def parser():
    p = msgs.ArgumentParser(prog="js schedule", allow_abbrev=False,
                            description=msgs.SCHEDULE_DESCRIPTION.words())
    p.add_argument("job", nargs="?", choices=JOBS, metavar="JOB", help=msgs.SCHEDULE_JOB.words())
    p.add_argument("--at", default=DEFAULT_AT, metavar="HH:MM", help=msgs.SCHEDULE_AT.words(at=DEFAULT_AT))
    p.add_argument("--remove", action="store_true", help=msgs.SCHEDULE_REMOVE.words())
    p.add_argument("--allow-unattended-cleanup", action="store_true",
                   help=msgs.SCHEDULE_ALLOW_CLEANUP.words(warning=msgs.SCHEDULE_CLEANUP_WARNING.words()))
    return p


# --- the machine ----------------------------------------------------------

def executable(name: str) -> str | None:
    """`name` on PATH. Every program this module runs is found here."""
    return shutil.which(name)


def _run(argv: list[str], *, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    found = executable(argv[0])
    if found is None:
        raise FileNotFoundError(argv[0])
    return subprocess.run([found, *argv[1:]], input=stdin, capture_output=True,
                          env={**os.environ, "LC_ALL": "C"}, check=False)


def unit_dir() -> Path:
    """The systemd user unit directory."""
    config = os.environ.get("XDG_CONFIG_HOME") or str(paths.user_home() / ".config")
    return Path(config) / "systemd" / "user"


def systemd_available() -> bool:
    """Whether a systemd user manager answers."""
    if executable("systemctl") is None:
        return False
    try:
        return _run(["systemctl", "--user", "show-environment"]).returncode == 0
    except OSError:
        return False


def cron_available() -> bool:
    return executable("crontab") is not None


def launcher() -> Path:
    """The js the job runs: the launcher `just install` writes, else the `js`
    on PATH."""
    installed = paths.user_home() / ".local" / "bin" / "js"
    try:
        if LAUNCHER_MARKER in installed.read_bytes()[:512]:
            return installed
    except OSError:
        pass
    found = executable("js")
    if found is None:
        raise ScheduleError(msgs.SCHEDULE_NO_LAUNCHER.text(path=installed))
    return Path(found).absolute()


def job_path(js: Path) -> str:
    """The PATH the job runs with: js's directory, then uv's, then the system's."""
    entries = [str(js.parent)]
    if (uv := executable("uv")) is not None:
        entries.append(str(Path(uv).absolute().parent))
    entries.extend(_BASE_PATH)
    return ":".join(dict.fromkeys(entries))


# --- one job --------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    name: str
    home: Path

    @property
    def default_home(self) -> bool:
        return self.home == paths.default_home().resolve(strict=False)

    @property
    def unit(self) -> str:
        """The unit name without its suffix."""
        if self.default_home:
            return f"js-{self.name}"
        return f"js-{self.name}-{hashlib.sha256(str(self.home).encode()).hexdigest()[:8]}"

    def argv(self, js: Path) -> list[str]:
        home = [] if self.default_home else ["--home", str(self.home)]
        return [str(js), *home, self.name]

    def cron_tag(self) -> str:
        return _cron_escape(f"{CRON_TAG} {self.name} {self.home}")

    def log(self) -> Path:
        return self.home / "logs" / "schedule" / f"{self.name}.log"


def parse_at(at: str) -> tuple[int, int]:
    match = _AT.fullmatch(at.strip())
    if match is None:
        raise ScheduleError(msgs.SCHEDULE_BAD_AT.text(at=at))
    return int(match[1]), int(match[2])


def _template(name: str) -> Template:
    found = paths.find(Path("schedule") / name)
    if found is None:
        raise ScheduleError(msgs.SCHEDULE_TEMPLATE_MISSING.text(path=paths.defaults_dir() / "schedule" / name))
    text = found.read_text(encoding="utf-8")
    if not text.strip():
        raise ScheduleError(msgs.SCHEDULE_TEMPLATE_EMPTY.text(path=found))
    return Template(text)


def _fill(name: str, **values: str) -> str:
    try:
        return _template(name).substitute(values)
    except (KeyError, ValueError) as exc:
        raise ScheduleError(msgs.SCHEDULE_TEMPLATE_BAD.text(path=Path("schedule") / name, error=exc)) from exc


def _systemd_word(word: str) -> str:
    """`word` as one argument of a unit's ExecStart or Environment value."""
    word = word.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[\w@%+=:,./-]+", word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_value(text: str) -> str:
    """`text` inside a double-quoted unit value."""
    return text.replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"')


def _cron_escape(text: str) -> str:
    """`text` with cron's `%` (a newline to cron) escaped."""
    return text.replace("%", "\\%")


def service_text(job: Job, js: Path) -> str:
    body = _fill("service", JOB=job.name, COMMAND=" ".join(_systemd_word(w) for w in job.argv(js)),
                 PATH=_systemd_value(job_path(js)))
    return _SERVICE_HOME.format(job=job.name) + str(job.home) + "\n" + body


def timer_text(job: Job, at: str) -> str:
    hour, minute = parse_at(at)
    return _fill("timer", JOB=job.name, AT=f"{hour:02d}:{minute:02d}", HOUR=str(hour), MINUTE=str(minute))


def cron_line(job: Job, js: Path, at: str) -> str:
    """The crontab line for `job`, its tag at the end, without a newline."""
    hour, minute = parse_at(at)
    quoted = lambda text: _cron_escape(shlex.quote(text))  # noqa: E731
    line = _fill("cron", JOB=job.name, COMMAND=" ".join(quoted(w) for w in job.argv(js)),
                 PATH=quoted(job_path(js)), LOG=quoted(str(job.log())),
                 AT=f"{hour:02d}:{minute:02d}", HOUR=str(hour), MINUTE=str(minute))
    return line.rstrip("\n") + " " + job.cron_tag()


# --- systemd --------------------------------------------------------------

def _units(job: Job) -> tuple[Path, Path]:
    directory = unit_dir()
    return directory / f"{job.unit}.service", directory / f"{job.unit}.timer"


def systemd_has(job: Job) -> bool:
    return _units(job)[1].is_file()


def _systemctl(*args: str) -> None:
    done = _run(["systemctl", "--user", *args])
    if done.returncode != 0:
        raise ScheduleError(msgs.SCHEDULE_COMMAND_FAILED.text(
            command="systemctl --user " + " ".join(args),
            error=done.stderr.decode(errors="replace").strip() or f"exit {done.returncode}"))


def systemd_schedule(job: Job, js: Path, at: str) -> None:
    service, timer = _units(job)
    service_body, timer_body = service_text(job, js), timer_text(job, at)
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(service_body, encoding="utf-8")
    timer.write_text(timer_body, encoding="utf-8")
    _systemctl("daemon-reload")
    _systemctl("enable", timer.name)
    # restart, so a changed --at takes on a timer that was already running
    _systemctl("restart", timer.name)


def systemd_remove(job: Job) -> bool:
    service, timer = _units(job)
    if not timer.exists() and not service.exists():
        return False
    if executable("systemctl") is not None:
        _run(["systemctl", "--user", "disable", "--now", timer.name])
    for each in (timer, service):
        each.unlink(missing_ok=True)
    if executable("systemctl") is not None:
        _run(["systemctl", "--user", "daemon-reload"])
    return True


def _service_home(service: Path, job: str) -> Path:
    """The home a js service names on its first line; the default home for a
    service without that line."""
    prefix = _SERVICE_HOME.format(job=job)
    try:
        first = service.read_text(encoding="utf-8").partition("\n")[0]
    except OSError:
        first = ""
    if first.startswith(prefix):
        return Path(first[len(prefix):])
    return paths.default_home().resolve(strict=False)


def systemd_jobs() -> list[tuple[Job, str]]:
    """Each js job with a timer in the unit directory, and its unit name."""
    directory = unit_dir()
    found = []
    for name in JOBS:
        pattern = re.compile(rf"js-{name}(-[0-9a-f]{{8}})?\.timer")
        for timer in sorted(directory.glob(f"js-{name}*.timer")) if directory.is_dir() else ():
            if pattern.fullmatch(timer.name):
                job = Job(name, _service_home(timer.with_suffix(".service"), name))
                found.append((job, timer.stem))
    return found


def systemd_next(unit: str) -> str:
    try:
        done = _run(["systemctl", "--user", "show", f"{unit}.timer", "-p", "NextElapseUSecRealtime", "--value"])
    except OSError:
        return msgs.SCHEDULE_NEXT_UNKNOWN.text()
    text = done.stdout.decode(errors="replace").strip()
    return text if done.returncode == 0 and text and text != "n/a" else msgs.SCHEDULE_NEXT_UNKNOWN.text()


# --- cron -----------------------------------------------------------------

def read_crontab() -> bytes:
    """The user's crontab; empty when there is none. Any other failure is a
    ScheduleError, so nothing is written over a crontab that was not read."""
    done = _run(["crontab", "-l"])
    if done.returncode == 0:
        return done.stdout
    if b"no crontab" in done.stderr.lower():
        return b""
    raise ScheduleError(msgs.SCHEDULE_COMMAND_FAILED.text(
        command="crontab -l", error=done.stderr.decode(errors="replace").strip() or f"exit {done.returncode}"))


def _write_crontab(text: bytes) -> None:
    done = _run(["crontab", "-"], stdin=text)
    if done.returncode != 0:
        raise ScheduleError(msgs.SCHEDULE_COMMAND_FAILED.text(
            command="crontab -", error=done.stderr.decode(errors="replace").strip() or f"exit {done.returncode}"))


def _is_line_of(line: bytes, job: Job) -> bool:
    return line.rstrip(b"\r\n").endswith(b" " + job.cron_tag().encode())


def _without(table: bytes, job: Job) -> bytes:
    return b"".join(line for line in table.splitlines(keepends=True) if not _is_line_of(line, job))


def cron_has(job: Job) -> bool:
    return any(_is_line_of(line, job) for line in read_crontab().splitlines())


def cron_schedule(job: Job, js: Path, at: str) -> None:
    line = cron_line(job, js, at).encode()
    before = read_crontab()
    kept = _without(before, job)
    if kept and not kept.endswith(b"\n"):
        kept += b"\n"
    after = kept + line + b"\n"
    job.log().parent.mkdir(parents=True, exist_ok=True)
    if after != before:
        _write_crontab(after)


def cron_remove(job: Job) -> bool:
    before = read_crontab()
    after = _without(before, job)
    if after == before:
        return False
    _write_crontab(after)
    return True


_CRON_ENTRY = re.compile(rb"\s*(\d+)\s+(\d+)\s+\*\s+\*\s+\*\s.*" + re.escape(CRON_TAG.encode()) + rb" (\w+) (.+?)\s*")


def cron_jobs() -> list[tuple[Job, str]]:
    """Each js line in the crontab: its job and when it next runs."""
    found = []
    for line in read_crontab().splitlines():
        match = _CRON_ENTRY.fullmatch(line)
        if match is None or match[3].decode() not in JOBS:
            continue
        home = Path(match[4].decode(errors="replace").replace("\\%", "%"))
        found.append((Job(match[3].decode(), home), _next_daily(int(match[2]), int(match[1]))))
    return found


def _next_daily(hour: int, minute: int, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now().astimezone()
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += dt.timedelta(days=1)
    return at.strftime("%a %Y-%m-%d %H:%M %Z").strip()


# --- the command ----------------------------------------------------------

def _backend_holding(job: Job) -> str | None:
    if systemd_has(job):
        return "systemd"
    if cron_available():
        try:
            if cron_has(job):
                return "cron"
        except (ScheduleError, OSError):
            pass
    return None


def _backend() -> str | None:
    if systemd_available():
        return "systemd"
    if cron_available():
        return "cron"
    return None


def schedule(job: Job, at: str) -> int:
    parse_at(at)
    js = launcher()
    backend = _backend_holding(job) or _backend()
    if backend is None:
        msgs.warn(msgs.SCHEDULE_NO_BACKEND)
        print(cron_line(job, js, at))
        return 1
    if backend == "systemd":
        systemd_schedule(job, js, at)
        msgs.say(msgs.SCHEDULE_DONE_SYSTEMD, job=job.name, at=at, unit=f"{job.unit}.timer",
                 directory=unit_dir())
    else:
        try:
            cron_schedule(job, js, at)
        except ScheduleError:
            print(cron_line(job, js, at))
            raise
        msgs.say(msgs.SCHEDULE_DONE_CRON, job=job.name, at=at, log=job.log())
    return 0


def remove(job: Job) -> int:
    removed = systemd_remove(job)
    if cron_available():
        removed = cron_remove(job) or removed
    msgs.say(msgs.SCHEDULE_REMOVED if removed else msgs.SCHEDULE_NOT_SCHEDULED, job=job.name)
    return 0


def listing() -> int:
    rows = [(job, "systemd", systemd_next(unit)) for job, unit in systemd_jobs()]
    if cron_available():
        rows += [(job, "cron", when) for job, when in cron_jobs()]
    if not rows:
        msgs.say(msgs.SCHEDULE_NOTHING)
        return 0
    for job, backend, when in rows:
        msgs.say(msgs.SCHEDULE_ROW, job=job.name, backend=backend, next=when, home=job.home)
    return 0


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    job = Job(args.job, paths.home()) if args.job else None
    try:
        if job is None:
            if args.remove:
                msgs.warn(msgs.SCHEDULE_REMOVE_NEEDS_JOB)
                return 2
            return listing()
        if args.remove:
            return remove(job)
        if job.name == "cleanup":
            msgs.warn(msgs.SCHEDULE_CLEANUP_WARNING)
            if not args.allow_unattended_cleanup:
                msgs.warn(msgs.SCHEDULE_CLEANUP_REFUSED)
                return 2
        return schedule(job, args.at)
    except (ScheduleError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 1
