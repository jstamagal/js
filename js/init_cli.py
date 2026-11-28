"""`js init`, `js init --full` and `js init agent NAME`: scaffolding.

`js init` makes the current directory a js project: a git repository when it
is not inside one, and a `.js/` holding an empty `jsrc` and the `agents/`,
`skills/`, `commands/` and `toolbox/` directories, each kept in git by a
`.gitkeep`. `--full` adds a README.md, a LICENSE and a .gitignore.
`js init agent NAME` makes `<home>/agents/NAME/` with an agent.yaml and a
01-prompt.md.

Nothing that exists is written to: a file is created with exclusive mode and
a directory with a plain mkdir, so a path that is already there (a dangling
symlink included) is left as it is and reported as left alone. The texts
written come from `defaults/scaffold/`, found through the setup layers, so a
home `scaffold/` file replaces the stock one. agent.yaml and gitignore are
copied as they are; 01-prompt.md, README.md and LICENSE have `${...}` holes.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import subprocess
from pathlib import Path
from string import Template

from . import messages as msgs, paths

# The directories of a project `.js/`, beside its jsrc.
PROJECT_DIRS = ("agents", "skills", "commands", "toolbox")
_KEEP = ".gitkeep"


def parser() -> argparse.ArgumentParser:
    """`js init [--full]`."""
    p = msgs.ArgumentParser(prog="js init", allow_abbrev=False, description=msgs.INIT_DESCRIPTION.words())
    p.add_argument("--full", action="store_true", help=msgs.INIT_OPT_FULL.words())
    return p


def agent_parser() -> argparse.ArgumentParser:
    """`js init agent NAME`."""
    p = msgs.ArgumentParser(prog="js init agent", allow_abbrev=False,
                            description=msgs.INIT_AGENT_DESCRIPTION.words())
    p.add_argument("name", metavar="NAME", help=msgs.INIT_OPT_AGENT_NAME.words())
    return p


def main(argv: list[str]) -> int:
    """`js init [--full]`, or `js init agent NAME` when the first word is `agent`."""
    if argv[:1] == ["agent"]:
        return run_init_agent(agent_parser().parse_args(argv[1:]).name)
    return run_init(full=parser().parse_args(argv).full)


def _text(name: str) -> str:
    """Scaffold file ``name`` as written."""
    found = paths.find(Path("scaffold") / name)
    if found is None:
        raise ValueError(msgs.INIT_TEMPLATE_MISSING.text(path=paths.defaults_dir() / "scaffold" / name))
    return found.read_text(encoding="utf-8")


def _template(name: str) -> Template:
    """Scaffold file ``name`` with `${...}` holes to fill."""
    return Template(_text(name))


def _make_file(path: Path, text: str) -> bool:
    """Create ``path`` holding ``text``; False when something is already there."""
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        return False
    return True


def _make_dir(path: Path) -> bool:
    """Create the directory ``path``; False when something is already there."""
    try:
        path.mkdir()
    except FileExistsError:
        return False
    return True


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=False)


def _inside_repository(directory: Path) -> bool:
    probe = _git(directory, "rev-parse", "--is-inside-work-tree")
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _holder(directory: Path) -> str:
    """The copyright holder: git's user.name here, else the OS user."""
    name = _git(directory, "config", "user.name").stdout.strip()
    return name or getpass.getuser()


class _Report:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.made: list[str] = []
        self.kept: list[str] = []

    def file(self, rel: str, text: str) -> None:
        (self.made if _make_file(self.directory / rel, text) else self.kept).append(rel)

    def dir(self, rel: str) -> bool:
        made = _make_dir(self.directory / rel)
        (self.made if made else self.kept).append(rel + "/")
        return made

    def say(self) -> None:
        if not self.made:
            msgs.say(msgs.INIT_NOTHING_MADE, directory=self.directory, kept=", ".join(self.kept))
            return
        if not self.kept:
            msgs.say(msgs.INIT_MADE, made=", ".join(self.made), directory=self.directory)
            return
        msgs.say(msgs.INIT_MADE_LEFT_ALONE, made=", ".join(self.made), directory=self.directory,
                 kept=", ".join(self.kept))


def _skeleton(report: _Report) -> None:
    """git init outside a repository, then the `.js/` skeleton. The skeleton
    is reported as `.js/` when it was all made or all already there, else
    part by part."""
    directory = report.directory
    if not _inside_repository(directory):
        init = _git(directory, "init", "-q")
        if init.returncode != 0:
            detail = (init.stderr or init.stdout).strip() or f"exit {init.returncode}"
            raise OSError(msgs.INIT_GIT_FAILED.text(error=detail))
        report.made.append(".git/")
    layer = directory / ".js"
    fresh = _make_dir(layer)
    inner = _Report(layer)
    inner.file("jsrc", "")
    for name in PROJECT_DIRS:
        if inner.dir(name):
            _make_file(layer / name / _KEEP, "")
    if fresh:
        report.made.append(".js/")
        return
    if not inner.made:
        report.kept.append(".js/")
        return
    report.made += [".js/" + rel for rel in inner.made]
    report.kept += [".js/" + rel for rel in inner.kept]


def run_init(*, full: bool) -> int:
    """`js init`, and with ``full`` `js init --full`, in the current directory."""
    directory = Path.cwd()
    if (directory / ".js").resolve(strict=False) == paths.home().resolve(strict=False):
        msgs.warn(msgs.INIT_IN_HOME, home=paths.home())
        return 2
    report = _Report(directory)
    try:
        if full:
            # Read every template before anything is made.
            readme, license_text, gitignore = _template("README.md"), _template("LICENSE"), _text("gitignore")
        _skeleton(report)
        if full:
            report.file("README.md", readme.safe_substitute(title=directory.name or str(directory)))
            report.file("LICENSE", license_text.safe_substitute(
                year=datetime.date.today().year, holder=_holder(directory)))
            report.file(".gitignore", gitignore)
    except (OSError, ValueError) as exc:
        if report.made:
            report.say()
        msgs.warn(msgs.FAILED, error=exc)
        return 1
    report.say()
    return 0


def run_init_agent(name: str) -> int:
    """`js init agent NAME`: a new agent in the home's agents directory."""
    try:
        rel = paths.agent_path(name)
    except ValueError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    target = paths.global_agents_dir() / rel
    if target.exists() or target.is_symlink():
        msgs.warn(msgs.INIT_AGENT_EXISTS, agent=name, path=target)
        return 1
    try:
        yaml_text = _text("agent.yaml")
        prompt_text = _template("01-prompt.md").safe_substitute(name=name)
        if (paths.defaults_dir() / "agents" / rel).is_dir():
            msgs.warn(msgs.INIT_AGENT_SHADOWS_STOCK, agent=name)
        target.mkdir(parents=True)
        _make_file(target / "agent.yaml", yaml_text)
        _make_file(target / "01-prompt.md", prompt_text)
    except FileExistsError:
        msgs.warn(msgs.INIT_AGENT_EXISTS, agent=name, path=target)
        return 1
    except (OSError, ValueError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 1
    msgs.say(msgs.INIT_AGENT_MADE, agent=name, path=target)
    return 0
