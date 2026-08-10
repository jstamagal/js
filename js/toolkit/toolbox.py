"""A toolbox that outlives the session, so tools get polished instead of rewritten.

The kernel gives an agent tools for one session. This gives it tools forever:
qwen writes `parse_nginx_log` on Monday, fable finds the timezone bug on Tuesday
and saves a revision, and the next session starts with the fixed version already
loadable. Three properties make that a toolbox rather than a directory of
scripts.

PROVENANCE. Every tool carries who wrote each revision and why, as one
machine-readable comment line at the top of the file. "fable refined this" is
only useful if the next model can see what fable was fixing. The header is
parsed and regenerated, never hand-edited.

HISTORY. `save` never overwrites. It archives revision N to `.history/name.rN.py`
and writes N+1. A model that "improves" a working tool into a broken one is one
`restore` away from the version that worked. `restore` itself writes the old body
as a NEW revision, so history is append-only and nothing is ever lost.

ISOLATION. Every tool file is exec'd on its own inside the kernel with its own
try/except. A toolbox an agent writes to unsupervised will contain a broken file
eventually; that must cost one tool, not the whole box.

This module sits ON TOP of `kernel`. It imports the kernel to reach the live
session; the kernel never imports this. Remove `toolbox` from an agent's surface
and the kernel is unaffected.
"""

from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import paths as _paths
from . import kernel as _kernel
from .core import Tool, ToolContext
from .descriptions import load_description
from .sanitize import int_or_default, text_or_default

_HEADER = re.compile(r"^#\s*js-toolbox:\s*(\{.*?\})\s*$", re.MULTILINE)
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REVISION_SUFFIX = re.compile(r"\.r(\d+)\.py$")

ACTIONS = ("list", "save", "load", "history", "restore")

# Recovers a definition's source from the KERNEL process, where it lives, AND
# works out what it needs from the surrounding namespace to still run tomorrow.
#
# The second half is not a nicety. A function saved bare is a function that
# NameErrors on load: `summarise` used `statistics.fmean`, `statistics` was
# imported in a different cell, the saved file had no import, and the tool was
# broken the moment the session that wrote it ended. So the probe resolves every
# free name against the live namespace: modules become import lines prepended to
# the saved body, and anything it cannot resolve comes back as a warning naming
# the name.
#
# `inspect.getsource` covers cells IPython registered with linecache and files a
# previous `load` exec'd; the `In` scan is the fallback for anything it misses.
_SOURCE_PROBE = """
def __js_src(target_name):
    import ast, builtins, inspect, json, textwrap

    def _body():
        target = globals().get(target_name)
        if target is not None and callable(target):
            try:
                return textwrap.dedent(inspect.getsource(target))
            except (OSError, TypeError):
                pass
        history = globals().get('In')
        if isinstance(history, list):
            wanted = ('def ' + target_name, 'class ' + target_name)
            for cell in reversed(history):
                if not isinstance(cell, str) or not any(w in cell for w in wanted):
                    continue
                try:
                    tree = ast.parse(cell)
                except SyntaxError:
                    continue
                for node in tree.body:
                    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    if isinstance(node, kinds) and node.name == target_name:
                        return ast.get_source_segment(cell, node)
        return ''

    def _free(src):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return []
        bound, used = {target_name}, []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                (used.append(node.id) if isinstance(node.ctx, ast.Load)
                 else bound.add(node.id))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.alias):
                bound.add((node.asname or node.name).split('.')[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                bound.update(node.names)
        seen, out = set(), []
        for name in used:
            if name in bound or name in dir(builtins) or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    source = _body()
    imports, unresolved = [], []
    for name in _free(source):
        value = globals().get(name)
        module = getattr(value, '__name__', None) if inspect.ismodule(value) else None
        if module:
            imports.append('import ' + module
                           + ('' if module == name else ' as ' + name))
        else:
            unresolved.append(name)
    return json.dumps({'source': source, 'imports': imports,
                       'unresolved': unresolved})
print('__JS_SRC__' + __js_src(%(name)r))
del __js_src
"""

# One round trip, per-file isolation preserved: the loop lives in the kernel so
# a tool whose module-level code raises costs exactly that tool.
_LOAD_PROBE = """
def __js_load(payload):
    import json
    loaded, problems = [], []
    for item_name, item_path, item_source in json.loads(payload):
        try:
            exec(compile(item_source, item_path, 'exec'), globals())
        except BaseException as exc:
            problems.append(item_name + ': ' + type(exc).__name__ + ': ' + str(exc))
            continue
        loaded.append(item_name)
    return json.dumps({'loaded': loaded, 'problems': problems})
print('__JS_LOAD__' + __js_load(%(payload)r))
del __js_load
"""


def toolbox_dirs(cwd: Path) -> tuple[Path, Path]:
    """(global, project). Project wins on a name clash, mirroring agent precedence."""
    return _paths.config_dir() / "toolbox", Path(cwd) / ".js" / "toolbox"


@dataclass
class ToolRecord:
    name: str
    path: Path
    scope: str                      # "global" or "project"
    revision: int = 1
    history: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def authors(self) -> list[str]:
        seen: list[str] = []
        for entry in self.history:
            who = entry.get("model") or "unknown"
            if who not in seen:
                seen.append(who)
        return seen

    def summary(self) -> str:
        chain = " -> ".join(self.authors) or "unknown"
        line = f"{self.name} r{self.revision} [{self.scope}] {chain}"
        return line + (f"  BROKEN: {self.error}" if self.error else "")


def _parse_header(text: str) -> dict:
    match = _HEADER.search(text)
    if not match:
        return {}
    try:
        loaded = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _render_header(meta: dict) -> str:
    return "# js-toolbox: " + json.dumps(meta, separators=(",", ":"), sort_keys=True)


def discover(cwd: Path) -> dict[str, ToolRecord]:
    """Index every tool file. A file that will not parse is recorded, never raised."""
    found: dict[str, ToolRecord] = {}
    for scope, directory in zip(("global", "project"), toolbox_dirs(cwd), strict=True):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            name = path.stem
            if not _SAFE_NAME.match(name):
                continue
            record = ToolRecord(name=name, path=path, scope=scope)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                record.error = f"unreadable: {exc}"
                found[name] = record
                continue
            meta = _parse_header(text)
            record.revision = int_or_default(meta.get("revision"), 1, minimum=1)
            raw_history = meta.get("history")
            record.history = raw_history if isinstance(raw_history, list) else []
            try:
                ast.parse(text, filename=str(path))
            except SyntaxError as exc:
                record.error = f"syntax error line {exc.lineno}: {exc.msg}"
            found[name] = record       # project scope overwrites global by ordering
    return found


def describe(cwd: Path) -> str:
    """The listing the model reads to know what it already has."""
    records = discover(cwd)
    if not records:
        return "TOOLBOX (empty)"
    lines = [record.summary() for record in sorted(records.values(), key=lambda r: r.name)]
    return "TOOLBOX\n" + "\n".join(lines)


def _archived_revisions(record: ToolRecord) -> list[int]:
    history_dir = record.path.parent / ".history"
    if not history_dir.is_dir():
        return []
    return sorted(
        int(match.group(1))
        for path in history_dir.glob(f"{record.name}.r*.py")
        if (match := _REVISION_SUFFIX.search(path.name))
    )


def write_revision(
    cwd: Path,
    name: str,
    body: str,
    *,
    model: str = "",
    note: str = "",
    scope: str = "global",
    stamp: float | None = None,
) -> str:
    """Promote a body to disk as revision N+1, archiving revision N first."""
    if not _SAFE_NAME.match(name):
        return f"ERROR: {name!r} is not a valid tool name"
    body = body.strip()
    if not body:
        return f"ERROR: no source for {name!r}"
    try:
        ast.parse(body)
    except SyntaxError as exc:
        return f"ERROR: {name!r} does not parse: line {exc.lineno}: {exc.msg}"

    global_dir, project_dir = toolbox_dirs(cwd)
    directory = project_dir if scope == "project" else global_dir
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"ERROR: could not create {directory}: {exc}"
    target = directory / f"{name}.py"

    existing = discover(cwd).get(name)
    revision = 1
    history: list[dict] = []
    if existing is not None and existing.path == target:
        revision = existing.revision + 1
        history = list(existing.history)
        archive = directory / ".history" / f"{name}.r{existing.revision}.py"
        try:
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:
            return f"ERROR: could not archive revision {existing.revision}: {exc}"

    when = time.strftime("%Y-%m-%d", time.localtime(stamp if stamp is not None else time.time()))
    history.append({"revision": revision, "model": model or "unknown",
                    "date": when, "note": note})
    meta = {"name": name, "revision": revision, "history": history}
    try:
        target.write_text(_render_header(meta) + "\n\n" + body.rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        return f"ERROR: could not write {target}: {exc}"

    verb = "saved" if revision == 1 else "refined"
    chain = " -> ".join(entry.get("model", "?") for entry in history)
    return f"{verb} {name} r{revision} [{scope}] {target}\nauthors: {chain}"


def history_of(cwd: Path, name: str) -> str:
    record = discover(cwd).get(name)
    if record is None:
        return f"ERROR: no tool named {name!r}"
    lines = [f"{record.name} r{record.revision} [{record.scope}] {record.path}"]
    for entry in record.history:
        note = entry.get("note") or "(no note)"
        lines.append(f"  r{entry.get('revision', '?')} {entry.get('date', '?')} "
                     f"{entry.get('model', 'unknown')}: {note}")
    archived = _archived_revisions(record)
    lines.append("  restorable: " + (", ".join(f"r{n}" for n in archived) or "none"))
    return "\n".join(lines)


def restore(cwd: Path, name: str, revision: int, *, model: str = "") -> str:
    """Roll back by writing the archived body as a NEW revision. Append-only."""
    record = discover(cwd).get(name)
    if record is None:
        return f"ERROR: no tool named {name!r}"
    archive = record.path.parent / ".history" / f"{name}.r{revision}.py"
    if not archive.is_file():
        available = _archived_revisions(record)
        have = ", ".join(f"r{n}" for n in available) or "none"
        return f"ERROR: no revision {revision} of {name!r}; restorable: {have}"
    try:
        text = archive.read_text(encoding="utf-8")
    except OSError as exc:
        return f"ERROR: could not read {archive}: {exc}"
    body = _HEADER.sub("", text).strip()
    return write_revision(cwd, name, body, model=model or "restore",
                          note=f"rolled back to r{revision}", scope=record.scope)


@dataclass
class Extracted:
    """A definition lifted out of the kernel, plus what it needs to run alone."""

    source: str = ""
    imports: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def body(self) -> str:
        """The self-contained file body: hoisted imports, then the definition."""
        if not self.source.strip():
            return ""
        prefix = "\n".join(self.imports) + "\n\n" if self.imports else ""
        return prefix + self.source.strip()


def source_from_kernel(session: Any, name: str) -> Extracted:
    probe = _SOURCE_PROBE % {"name": name}
    result = _kernel.run_cell(session, probe, timeout=30,
                              store_history=False, marker="__JS_SRC__")
    if not result.marker:
        return Extracted()
    try:
        value = json.loads(result.marker)
    except json.JSONDecodeError:
        return Extracted()
    if not isinstance(value, dict):
        return Extracted()
    return Extracted(
        source=str(value.get("source") or ""),
        imports=[str(item) for item in value.get("imports", [])],
        unresolved=[str(item) for item in value.get("unresolved", [])],
    )


def load_into_kernel(session: Any, cwd: Path) -> tuple[list[str], list[str]]:
    """Exec every healthy tool file into the kernel. (loaded names, problem lines)."""
    payload: list[list[str]] = []
    problems: list[str] = []
    for name, record in sorted(discover(cwd).items()):
        if record.error:
            problems.append(f"{name}: {record.error}")
            continue
        try:
            payload.append([name, str(record.path), record.path.read_text(encoding="utf-8")])
        except OSError as exc:
            problems.append(f"{name}: unreadable: {exc}")
    if not payload:
        return [], problems
    probe = _LOAD_PROBE % {"payload": json.dumps(payload)}
    result = _kernel.run_cell(session, probe, timeout=120,
                              store_history=False, marker="__JS_LOAD__")
    if not result.marker:
        detail = (result.error or result.stderr).strip() or "no report from the kernel"
        return [], [*problems, f"load failed: {detail}"]
    try:
        report = json.loads(result.marker)
    except json.JSONDecodeError:
        return [], [*problems, "load failed: unparseable report from the kernel"]
    return list(report.get("loaded", [])), [*problems, *report.get("problems", [])]


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


def toolbox(
    action: str = "list",
    name: str = "",
    note: str = "",
    scope: str = "global",
    revision: int = 0,
    source: str = "",
    verbosity: str = "",
    context: ToolContext | None = None,
) -> str:
    if context is None:
        return "ERROR: missing ToolContext"
    verb = text_or_default(action, "list").strip().lower()
    name = text_or_default(name).strip()
    scope = "project" if text_or_default(scope, "global").strip().lower() == "project" else "global"
    level = _kernel.resolve_verbosity(context, verbosity)
    cwd = Path(context.cwd)
    model = str(getattr(context, "model", "") or "unknown")

    if verb not in ACTIONS:
        return f"ERROR: action must be one of {', '.join(ACTIONS)}"

    if verb == "list":
        listing = describe(cwd)
        _kernel.render_event(context, level, listing)
        return _kernel.cap_for_model(listing, context)

    if verb == "history":
        if not name:
            return "ERROR: history needs a tool name"
        report = history_of(cwd, name)
        _kernel.render_event(context, level, report)
        return _kernel.cap_for_model(report, context)

    if verb == "restore":
        if not name:
            return "ERROR: restore needs a tool name"
        target = int_or_default(revision, 0, minimum=1)
        if target == 0:
            return "ERROR: restore needs revision=<n>; call history first to see what exists"
        report = restore(cwd, name, target, model=model)
        _kernel.render_event(context, level, report,
                             style="bold red" if report.startswith("ERROR") else "green")
        return _kernel.cap_for_model(report, context)

    if verb == "save":
        if not name:
            return "ERROR: save needs a tool name"
        body = text_or_default(source)
        warnings: list[str] = []
        if not body.strip():
            session, problem, _started = _kernel.get_session(context)
            if session is None:
                return (f"{problem}\nWithout a kernel, save needs the definition passed "
                        "explicitly in `source`.")
            extracted = source_from_kernel(session, name)
            body = extracted.body()
            if extracted.imports:
                warnings.append("hoisted into the file: " + "; ".join(extracted.imports))
            if extracted.unresolved:
                warnings.append(
                    "WARNING this definition also uses "
                    + ", ".join(extracted.unresolved)
                    + " from the session namespace, which is NOT in the saved file. "
                    "Save those too, or inline them, or the tool will NameError when "
                    "a later session loads it."
                )
        if not body.strip():
            return (f"ERROR: no source found for {name!r}. Define it in the kernel first, "
                    "or pass the definition in `source`.")
        report = "\n".join([write_revision(cwd, name, body, model=model,
                                           note=text_or_default(note), scope=scope),
                            *warnings])
        _kernel.render_event(context, level, report,
                             style="bold red" if report.startswith("ERROR") else "green")
        return _kernel.cap_for_model(report, context)

    # load
    session, problem, started_now = _kernel.get_session(context)
    if session is None:
        _kernel.render_event(context, level, problem, style="bold red")
        return problem
    if started_now:
        _kernel.render_event(context, level, f"kernel started in {session.cwd}",
                             verbose_only=True)
    loaded, problems = load_into_kernel(session, cwd)
    if loaded:
        _kernel.refresh_namespace(session)
    records = discover(cwd)
    lines: list[str] = []
    if loaded:
        lines.append("TOOLBOX loaded " + ", ".join(
            records[item].summary() if item in records else item for item in loaded))
    else:
        lines.append("TOOLBOX loaded nothing" + (" (the toolbox is empty)"
                                                 if not records else ""))
    for problem_line in problems:
        lines.append(f"TOOLBOX BROKEN {problem_line}")
    if session.namespace:
        lines.append("NAMESPACE " + ", ".join(sorted(session.namespace)))
    for line in lines:
        _kernel.render_event(context, level, line,
                             style="red" if "BROKEN" in line else "green")
    return _kernel.cap_for_model("\n".join(lines), context)


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "toolbox",
            load_description("toolbox"),
            toolbox,
            {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "name": {"type": "string"},
                "note": {"type": "string"},
                "scope": {"type": "string", "enum": ["global", "project"]},
                "revision": {"type": "integer"},
                "source": {"type": "string"},
                "verbosity": {"type": "string", "enum": list(_kernel.VERBOSITY_LEVELS)},
            },
            required=("action",),
        ),
    )
