"""The lsp tool: server choice, the protocol path against a fake server, jailed
servers, and real language servers when they are installed."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from js import jail, settings
from js.toolkit import ToolContext, call_tool, lsp as lsp_mod, policy
from js.toolkit.registry import build_default_registry

FAKE_SERVER = Path(__file__).with_name("fake_lsp_server.py")
REPO = Path(__file__).resolve().parents[1]
needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")


@pytest.fixture(autouse=True)
def stop_servers():
    yield
    lsp_mod.shutdown_servers()


def fake(context: ToolContext, log: Path, *flags: str, script: Path = FAKE_SERVER) -> None:
    context.lsp_servers = [{
        "name": "fake", "command": [sys.executable, str(script), str(log), *flags],
        "extensions": [".fake"], "roots": ["fake.toml"],
    }]


def sent(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def methods(log: Path) -> list[str]:
    return [message.get("method", "<response>") for message in sent(log)]


def run(context: ToolContext, **args) -> str:
    return call_tool(build_default_registry().resolve("lsp"), args, context)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "fake.toml").write_text("")
    context = ToolContext(cwd=root)
    context.lsp_timeout_s = 20
    fake(context, tmp_path / "lsp.log")
    return root, context, tmp_path / "lsp.log"


def test_diagnostics_follow_edits_on_disk(project):
    root, context, log = project
    source = root / "pkg" / "mod.fake"
    source.write_text("x = 1\n  ERROR here\n")

    first = run(context, operation="diagnostics", file_path=str(source))
    source.write_text("x = 1\nfine\n")
    clean = run(context, operation="diagnostics", file_path=str(source))
    source.write_text("x = 1\nfine\nstill ERROR\n")
    again = run(context, operation="diagnostics", file_path=str(source))

    assert "2:3: error: found ERROR" in first
    assert "found ERROR" not in clean
    assert "3:7: error: found ERROR" in again
    assert "2:3" not in again
    order = methods(log)
    assert order.count("textDocument/didOpen") == 1
    assert order.count("textDocument/didChange") == 2
    assert order.count("textDocument/didSave") == 2
    changes = [m["params"]["textDocument"]["version"] for m in sent(log) if m.get("method") == "textDocument/didChange"]
    assert changes == [2, 3]


def test_unchanged_file_returns_cached_diagnostics_without_resending(project):
    root, context, log = project
    source = root / "mod.fake"
    source.write_text("ERROR\n")

    run(context, operation="diagnostics", file_path=str(source))
    second = run(context, operation="diagnostics", file_path=str(source))

    assert "1:1: error: found ERROR" in second
    assert methods(log).count("textDocument/didChange") == 0


def test_definition_references_and_hover_by_position(project):
    root, context, log = project
    source = root / "pkg" / "mod.fake"
    source.write_text("def target():\n    pass\n\ntarget()\n\U0001f600 = target\n")

    definition = run(context, operation="definition", file_path=str(source), line=4, symbol="target")
    references = run(context, operation="references", file_path=str(source), line=1, character=5)
    # Column 5 on line 5 is after a character outside the BMP: two UTF-16 units.
    hover = run(context, operation="hover", file_path=str(source), line=5, character=5)

    rel = os.path.join("pkg", "mod.fake")
    assert f"{rel}:1:5: def target():" in definition
    assert f"{rel}:1:5:" in references
    assert f"{rel}:4:1: target()" in references
    assert f"{rel}:5:5:" in references
    assert "hover `target`" in hover


def test_one_server_serves_every_call_in_a_workspace(project):
    root, context, log = project
    (root / "a.fake").write_text("def a():\n")
    (root / "pkg" / "b.fake").write_text("a\n")

    run(context, operation="hover", file_path=str(root / "a.fake"), line=1, symbol="a")
    run(context, operation="references", file_path=str(root / "pkg" / "b.fake"), line=1, character=1)

    initializes = [m for m in sent(log) if m.get("method") == "initialize"]
    assert len(initializes) == 1
    assert initializes[0]["params"]["rootUri"] == lsp_mod.file_uri(root)


def test_server_requests_are_answered(project):
    root, context, log = project
    (root / "a.fake").write_text("x\n")

    run(context, operation="hover", file_path=str(root / "a.fake"), line=1, character=1)

    replies = [m for m in sent(log) if "method" not in m and m.get("id") == 1001]
    assert replies and replies[0]["result"] == [None]


def test_file_no_server_covers_is_refused(project):
    root, context, _log = project
    source = root / "notes.unknownext"
    source.write_text("text\n")

    result = run(context, operation="diagnostics", file_path=str(source))

    assert result.startswith("ERROR:")
    assert ".unknownext" in result


def test_server_missing_from_path_is_refused(project):
    root, context, _log = project
    context.lsp_servers = [{"name": "ghost", "command": ["js-no-such-langserver-7731"], "extensions": [".fake"]}]
    source = root / "a.fake"
    source.write_text("x\n")

    result = run(context, operation="diagnostics", file_path=str(source))

    assert result.startswith("ERROR:")
    assert "js-no-such-langserver-7731" in result


def test_server_that_dies_at_start_is_refused_with_its_stderr(project, tmp_path):
    root, context, _log = project
    fake(context, tmp_path / "crash.log", "--crash")
    source = root / "a.fake"
    source.write_text("x\n")

    result = run(context, operation="diagnostics", file_path=str(source))

    assert result.startswith("ERROR:")
    assert "fake server failed to boot" in result


def test_bad_lsp_servers_setting_is_refused(project):
    root, context, _log = project
    context.lsp_servers = {"python": "pyright"}
    source = root / "a.fake"
    source.write_text("x\n")

    assert run(context, operation="diagnostics", file_path=str(source)).startswith("ERROR:")


def test_no_diagnostics_within_the_timeout_is_not_reported_clean(project, tmp_path):
    root, context, _log = project
    fake(context, tmp_path / "quiet.log", "--no-diagnostics")
    context.lsp_timeout_s = 1
    source = root / "a.fake"
    source.write_text("ERROR\n")

    result = run(context, operation="diagnostics", file_path=str(source))

    assert "lsp.timeout_s" in result
    assert "found ERROR" not in result


def test_position_operations_need_a_position(project):
    root, context, _log = project
    source = root / "a.fake"
    source.write_text("x\n")

    assert run(context, operation="hover", file_path=str(source)).startswith("ERROR:")
    assert run(context, operation="hover", file_path=str(source), line=1).startswith("ERROR:")
    assert run(context, operation="hover", file_path=str(source), line=9, character=1).startswith("ERROR:")
    assert run(context, operation="hover", file_path=str(source), line=1, symbol="nope").startswith("ERROR:")


def test_workspace_root_prefers_markers_then_git_then_cwd(tmp_path):
    spec = lsp_mod.ServerSpec("fake", ("x",), (".fake",), ("fake.toml",))
    top = tmp_path / "top"
    (top / "sub" / "deep").mkdir(parents=True)
    (top / ".git").mkdir()
    (top / "sub" / "fake.toml").write_text("")
    work = tmp_path / "work"
    (work / "loose").mkdir(parents=True)
    context = ToolContext(cwd=work)

    assert lsp_mod.workspace_root(top / "sub" / "deep" / "a.fake", spec, context) == top / "sub"
    (top / "sub" / "fake.toml").unlink()
    assert lsp_mod.workspace_root(top / "sub" / "deep" / "a.fake", spec, context) == top
    assert lsp_mod.workspace_root(work / "loose" / "a.fake", spec, context) == work
    # The operator's home (tmp_path here) is never a root, even holding .git.
    (tmp_path / ".git").mkdir()
    (tmp_path / "sub").mkdir()
    assert lsp_mod.workspace_root(tmp_path / "sub" / "a.fake", spec, ToolContext(cwd=tmp_path)) == tmp_path / "sub"


def test_every_default_server_entry_parses():
    specs = lsp_mod.parse_servers(settings.default_value("lsp.servers"))
    covered = {ext for spec in specs for ext in spec.extensions}

    assert {".py", ".rs", ".go", ".ts", ".tsx", ".js"} <= covered


def test_lsp_is_read_only_and_lazy_for_the_default_agent():
    entries = yaml.safe_load((REPO / "prompts" / "defaultagent" / "agent.yaml").read_text())["tools"]
    registry = build_default_registry()
    surface = registry.select(entries, config=policy.ToolsConfig(), warn=False)

    assert registry.resolve("lsp").read_only
    assert "lsp" in surface.lazy
    assert "notebook_edit" in surface.lazy


@pytest.fixture
def operator_home():
    home = Path(os.environ["HOME"])
    (home / ".zshrc").write_text("export TOKEN=operator-secret\n")
    return home


def test_unjailed_server_sees_the_home(project, operator_home):
    root, context, _log = project
    source = root / "a.fake"
    source.write_text("whoami\n")

    listing = run(context, operation="hover", file_path=str(source), line=1, character=1)

    assert ".zshrc" in listing


@needs_bwrap
def test_server_runs_in_the_jail(tmp_path, operator_home, monkeypatch):
    root = tmp_path / "jailed"
    root.mkdir()
    (root / "fake.toml").write_text("")
    script = root / "server.py"
    shutil.copyfile(FAKE_SERVER, script)
    source = root / "a.fake"
    source.write_text("whoami\nERROR\n")
    monkeypatch.chdir(root)
    jail.enter(root)
    context = ToolContext(cwd=root)
    context.jail_bind = (sys.base_prefix, sys.prefix)
    context.lsp_timeout_s = 20
    fake(context, root / "lsp.log", script=script)

    listing = run(context, operation="hover", file_path=str(source), line=1, character=1)
    diagnostics = run(context, operation="diagnostics", file_path=str(source))

    assert not listing.startswith("ERROR"), listing
    assert ".zshrc" not in listing
    assert "2:1: error: found ERROR" in diagnostics


# --- real language servers, when installed -----------------------------------

REAL_CASES = {
    "python": {
        "files": {"pyproject.toml": "[project]\nname = 'probe'\nversion = '0'\n",
                  "probe.py": "def f(x: int) -> int:\n    return x\n\n\nf('a')\n"},
        "target": "probe.py", "error_line": 5, "use": (5, "f"), "definition": "probe.py:1:5",
    },
    "go": {
        "files": {"go.mod": "module example.com/probe\n\ngo 1.21\n",
                  "main.go": "package main\n\nfunc f() int { return 1 }\n\nfunc main() {\n\tvar s string = f()\n\t_ = s\n}\n"},
        "target": "main.go", "error_line": 6, "use": (6, "f"), "definition": "main.go:3:6",
        "needs": ["go"],
    },
    "rust": {
        "files": {"Cargo.toml": "[package]\nname = \"probe\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
                  "src/main.rs": "fn f() -> i32 { 1 }\n\nfn main() {\n    let _x = f();\n    let y = ;\n}\n"},
        "target": "src/main.rs", "error_line": 5, "use": (4, "f"), "definition": "src/main.rs:1:4",
        "needs": ["cargo"],
    },
    "typescript": {
        "files": {"tsconfig.json": "{\"compilerOptions\": {\"strict\": true}}\n",
                  "index.ts": "function f(): number { return 1 }\nconst s: string = f();\nexport { s };\n"},
        "target": "index.ts", "error_line": 2, "use": (2, "f"), "definition": "index.ts:1:10",
    },
}
_EXTENSIONS = {"python": ".py", "go": ".go", "rust": ".rs", "typescript": ".ts"}


@pytest.mark.parametrize("language", sorted(REAL_CASES))
def test_real_language_server(language, tmp_path):
    case = REAL_CASES[language]
    specs = lsp_mod.parse_servers(settings.default_value("lsp.servers"))
    serving = [spec for spec in specs if _EXTENSIONS[language] in spec.extensions]
    if not any(shutil.which(spec.command[0]) for spec in serving):
        pytest.skip(f"no {language} language server on PATH")
    missing = [tool for tool in case.get("needs", []) if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"{language} language server needs {', '.join(missing)}")
    root = tmp_path / "probe"
    for name, text in case["files"].items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    context = ToolContext(cwd=root)
    context.lsp_timeout_s = 120
    target = str(root / case["target"])

    definition = run(context, operation="definition", file_path=target,
                     line=case["use"][0], symbol=case["use"][1])
    diagnostics = run(context, operation="diagnostics", file_path=target)

    assert case["definition"] in definition, definition
    assert any(row.startswith(f"{case['error_line']}:") and ": error: " in row
               for row in diagnostics.splitlines()), diagnostics
