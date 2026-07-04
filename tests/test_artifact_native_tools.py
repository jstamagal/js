from __future__ import annotations

import inspect
import json
import re
import subprocess
from pathlib import Path

from js.toolkit import ToolContext
from js.toolkit import artifact as artifact_tools
from js.toolkit.artifact import prompts as artifact_prompts


def _ctx(tmp_path: Path, max_bytes: int = 4096) -> ToolContext:
    return ToolContext(cwd=tmp_path, max_tool_result_bytes=max_bytes, fetch_timeout_s=9)


def test_base_prompt_documents_artifact_write_page_in_real_argument_order():
    """The BASE system prompt tells the model artifact_write_page's argument
    order — it must match the real signature or the model passes title/body
    into the wrong positional slots."""
    real_params = [
        name for name in inspect.signature(artifact_tools.artifact_write_page).parameters
        if name != "context"
    ]

    match = re.search(r"artifact_write_page\(([^)]*)\)", artifact_prompts.BASE)
    assert match, "BASE prompt must document artifact_write_page's argument order"
    documented_params = [p.strip() for p in match.group(1).split(",")]

    assert documented_params == real_params


def _artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    (root / "files").mkdir(parents=True)
    (root / "files" / "alpha.md").write_text("alpha preview text " * 8, encoding="utf-8")
    (root / "files" / "beta.txt").write_text("beta asset text\n", encoding="utf-8")
    (root / "files" / "alphabet.md").write_text("alphabet preview\n", encoding="utf-8")
    manifest = [
        {
            "slug": "alpha",
            "title": "Alpha Report",
            "kind": "markdown",
            "tags": ["ops"],
            "desc": "First report",
            "html": "alpha.html",
            "src": "files/alpha.md",
            "created": "2026-01-02T00:00:00",
        },
        {
            "slug": "beta",
            "title": "Beta Log",
            "kind": "page",
            "tags": [],
            "desc": "",
            "html": "beta/index.html",
            "asset": "files/beta.txt",
            "created": "2026-01-03T00:00:00",
        },
        {
            "slug": "alphabet",
            "title": "Alphabet Soup",
            "kind": "markdown",
            "tags": ["notes"],
            "desc": "Name overlap",
            "html": "alphabet.html",
            "src": "files/alphabet.md",
            "created": "2026-01-01T00:00:00",
        },
    ]
    curation = {
        "topics": ["ops", "notes"],
        "assignments": {"alpha": ["ops"], "alphabet": ["ops", "notes"]},
        "refs": {"alpha": ["beta"]},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "curation.json").write_text(json.dumps(curation), encoding="utf-8")
    return root


def test_artifact_overview_uses_temp_artifact_dir_manifest_and_curation(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    monkeypatch.setenv("ARTIFACT_DIR", str(root))
    monkeypatch.setenv("ARTIFACT_URL", "http://artifacts.local/base/")

    actual = json.loads(artifact_tools.artifact_overview(context=_ctx(tmp_path)))

    assert actual["root"] == str(root)
    assert actual["base_url"] == "http://artifacts.local/base"
    assert actual["count"] == 3
    assert actual["topic_counts"] == {"ops": 2, "notes": 1}
    assert actual["unassigned"] == ["beta"]
    assert actual["refs_count"] == 1
    assert [entry["slug"] for entry in actual["recent"]] == ["beta", "alpha", "alphabet"]
    assert actual["recent"][0]["url"] == "http://artifacts.local/base/beta/index.html"


def test_artifact_read_matches_slug_html_and_fragments_with_previews(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    monkeypatch.setenv("ARTIFACT_DIR", str(root))
    monkeypatch.setenv("ARTIFACT_URL", "http://artifacts.local")

    exact = json.loads(artifact_tools.artifact_read("alpha", context=_ctx(tmp_path, max_bytes=80)))
    html = json.loads(artifact_tools.artifact_read("beta/index.html", context=_ctx(tmp_path)))
    ambiguous = artifact_tools.artifact_read("alp", context=_ctx(tmp_path))
    missing = artifact_tools.artifact_read("missing", context=_ctx(tmp_path))

    assert exact["slug"] == "alpha"
    assert exact["url"] == "http://artifacts.local/alpha.html"
    assert exact["text_preview"].startswith("alpha preview text")
    assert exact["text_preview"] == "alpha preview text alpha preview text al"
    assert html["slug"] == "beta"
    assert html["asset_url"] == "http://artifacts.local/files/beta.txt"
    assert html["text_preview"] == "beta asset text\n"
    assert ambiguous == "ERROR: ambiguous artifact: alpha, alphabet"
    assert missing == "ERROR: no artifact matches 'missing'"


def test_artifact_read_rejects_boolean_slug_without_crashing(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    monkeypatch.setenv("ARTIFACT_DIR", str(root))

    actual = artifact_tools.artifact_read(True, context=_ctx(tmp_path))

    assert actual == "ERROR: no artifact matches ''"


def test_artifact_subprocess_runner_builds_cli_env_and_reports_failures(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(artifact_tools, "ARTIFACT_BIN", "artifact-test")

    def run_ok(cmd, cwd, env, capture_output, text, timeout):
        calls.append(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="found\n", stderr="")

    monkeypatch.setattr(artifact_tools.subprocess, "run", run_ok)

    ok = artifact_tools.artifact_search("needle", limit=7, context=_ctx(tmp_path))

    assert ok == "found"
    assert calls[0]["cmd"] == ["artifact-test", "search", "needle", "--limit", "7"]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["env"]["ARTIFACT_PUSH"] == "auto"
    assert calls[0]["capture_output"] is True
    assert calls[0]["text"] is True
    assert calls[0]["timeout"] == 9

    def run_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(artifact_tools.subprocess, "run", run_missing)
    missing = artifact_tools.artifact_search("needle", context=_ctx(tmp_path))

    def run_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("artifact-test", 9)

    monkeypatch.setattr(artifact_tools.subprocess, "run", run_timeout)
    timed_out = artifact_tools.artifact_search("needle", context=_ctx(tmp_path))

    def run_failed(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="bad stdout", stderr="bad stderr")

    monkeypatch.setattr(artifact_tools.subprocess, "run", run_failed)
    failed = artifact_tools.artifact_search("needle", context=_ctx(tmp_path))

    assert missing == "ERROR: artifact command not found: artifact-test"
    assert timed_out == "ERROR: artifact command timed out"
    assert failed == "ERROR: artifact search needle --limit 20 failed\nbad stderr"


def test_artifact_search_sanitizes_boolean_and_negative_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_tools, "ARTIFACT_BIN", "artifact-test")
    calls: list[list[str]] = []

    def run_ok(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="found\n", stderr="")

    monkeypatch.setattr(artifact_tools.subprocess, "run", run_ok)

    assert artifact_tools.artifact_search("needle", limit=True, context=_ctx(tmp_path)) == "found"
    assert artifact_tools.artifact_search("needle", limit=-1, context=_ctx(tmp_path)) == "found"
    assert artifact_tools.artifact_search("needle", limit="bad", context=_ctx(tmp_path)) == "found"

    assert calls == [
        ["artifact-test", "search", "needle", "--limit", "20"],
        ["artifact-test", "search", "needle", "--limit", "20"],
        ["artifact-test", "search", "needle", "--limit", "20"],
    ]


def test_artifact_search_sanitizes_boolean_query(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_tools, "ARTIFACT_BIN", "artifact-test")
    calls: list[list[str]] = []

    def run_ok(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="found\n", stderr="")

    monkeypatch.setattr(artifact_tools.subprocess, "run", run_ok)

    assert artifact_tools.artifact_search(True, context=_ctx(tmp_path)) == "found"
    assert calls == [["artifact-test", "search", "", "--limit", "20"]]


def test_artifact_curate_validates_json_calls_cli_and_removes_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_tools, "ARTIFACT_BIN", "artifact-test")
    calls: list[list[str]] = []
    temp_paths: list[Path] = []
    temp_payloads: list[dict] = []

    def run_stub(cmd, cwd, env, capture_output, text, timeout):
        calls.append(cmd)
        temp_path = Path(cmd[2])
        temp_paths.append(temp_path)
        temp_payloads.append(json.loads(temp_path.read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(cmd, 0, stdout="curated\n", stderr="")

    monkeypatch.setattr(artifact_tools.subprocess, "run", run_stub)
    payload = {"topics": ["ops"], "assignments": {"alpha": ["ops"]}, "refs": {}}

    actual = artifact_tools.artifact_curate(json.dumps(payload), context=_ctx(tmp_path))
    invalid = artifact_tools.artifact_curate("{bad json", context=_ctx(tmp_path))

    assert actual == "curated"
    assert calls[0][0:2] == ["artifact-test", "curate"]
    assert temp_payloads == [payload]
    assert temp_paths and not temp_paths[0].exists()
    assert invalid.startswith("ERROR: invalid curation JSON:")
    assert len(calls) == 1


def test_artifact_curate_rejects_boolean_json_without_crashing(tmp_path):
    actual = artifact_tools.artifact_curate(True, context=_ctx(tmp_path))

    assert actual.startswith("ERROR: invalid curation JSON:")


def test_artifact_write_page_chooses_new_or_update_and_deletes_body_tempfile(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    monkeypatch.setenv("ARTIFACT_DIR", str(root))
    calls: list[list[str]] = []
    body_paths: list[Path] = []
    bodies: list[str] = []

    def run_stub(args, context):
        calls.append(args)
        body_path = Path(args[1] if args[0] == "new" else args[2])
        body_paths.append(body_path)
        bodies.append(body_path.read_text(encoding="utf-8"))
        return f"{args[0]} ok"

    monkeypatch.setattr(artifact_tools, "_run_artifact", run_stub)

    updated = artifact_tools.artifact_write_page(
        title="Updated Alpha",
        body="updated body\n",
        slug="alpha",
        tags="ops,summary",
        desc="Updated desc",
        context=_ctx(tmp_path),
    )
    created = artifact_tools.artifact_write_page(
        title="New Page",
        body="new body\n",
        tags="fresh",
        context=_ctx(tmp_path),
    )

    assert updated == "update ok"
    assert created == "new ok"
    assert calls[0][0:2] == ["update", "alpha"]
    assert calls[0][3:] == ["--title", "Updated Alpha", "--tags", "ops,summary", "--desc", "Updated desc"]
    assert calls[1][0] == "new"
    assert calls[1][2:] == ["--title", "New Page", "--tags", "fresh"]
    assert bodies == ["updated body\n", "new body\n"]
    assert body_paths and all(not path.exists() for path in body_paths)


def test_artifact_write_page_sanitizes_boolean_string_fields(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    bodies: list[str] = []

    def run_stub(args, context):
        calls.append(args)
        body_path = Path(args[1])
        bodies.append(body_path.read_text(encoding="utf-8"))
        return "new ok"

    monkeypatch.setattr(artifact_tools, "_run_artifact", run_stub)

    actual = artifact_tools.artifact_write_page(
        title=True,
        body=True,
        tags=True,
        desc=True,
        context=_ctx(tmp_path),
    )

    assert actual == "new ok"
    assert bodies == [""]
    assert calls[0][2:] == ["--title", ""]


def test_artifact_ingest_rejects_boolean_paths_without_crashing(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(artifact_tools, "_run_artifact", lambda args, context: calls.append(args) or "ingested")

    actual = artifact_tools.artifact_ingest(True, tags=True, desc=True, context=_ctx(tmp_path))

    assert actual == "ERROR: no paths supplied"
    assert calls == []


def test_artifact_ingest_resolves_paths_and_rejects_empty_input(tmp_path, monkeypatch):
    first = tmp_path / "first.md"
    second = tmp_path / "nested" / "second.md"
    second.parent.mkdir()
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run_stub(args, context):
        calls.append(args)
        return "ingested"

    monkeypatch.setattr(artifact_tools, "_run_artifact", run_stub)

    actual = artifact_tools.artifact_ingest(
        "first.md\nnested/second.md",
        tags="raw",
        desc="Imported",
        context=_ctx(tmp_path),
    )
    empty = artifact_tools.artifact_ingest(" \n\t ", context=_ctx(tmp_path))

    assert actual == "ingested"
    assert calls == [
        [
            "ingest",
            str(first),
            str(second),
            "--tags",
            "raw",
            "--desc",
            "Imported",
        ]
    ]
    assert empty == "ERROR: no paths supplied"


def test_read_text_keeps_preview_when_byte_limit_splits_a_codepoint(tmp_path):
    """A byte-limited slice can cut mid multibyte char; the preview must survive
    (lenient decode) instead of the whole body dropping to empty."""
    p = tmp_path / "artifact.txt"
    p.write_text("A" * 10 + "€" * 10, encoding="utf-8")   # € = 3 bytes each
    out = artifact_tools._read_text(p, 11)   # 10 'A' + first byte of the first €
    assert out.startswith("A" * 10)
    assert out != ""
