"""Real ast-grep search and rewrite coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from js.toolkit import ToolContext
from js.toolkit import fs
from js.toolkit.fs import ast_search, undo


requires_ast_grep = pytest.mark.skipif(
    not Path(fs._AST_GREP_BINARY).is_file(), reason="ast-grep 0.45.1 not installed"
)


@requires_ast_grep
def test_ast_search_matches_calls_across_layout_but_only_parsed_code(tmp_path):
    target = tmp_path / "calls.txt"
    target.write_text(
        "foo(1)\n"
        "foo(\n"
        "    1,\n"
        "    2,\n"
        ")\n"
        "# foo(comment)\n"
        'message = "foo(string)"\n',
        encoding="utf-8",
    )
    context = ToolContext(cwd=tmp_path)

    actual = ast_search("foo($$$ARGS)", path="calls.txt", lang="Python", context=context)

    expected_source = ["foo(1)", "foo(", "    1,", "    2,", ")"]
    expected = [f"{target}:1", f"1{fs._line_hash(expected_source[0])}|{expected_source[0]}", f"{target}:2"]
    expected.extend(
        f"{line_number}{fs._line_hash(line)}|{line}"
        for line_number, line in enumerate(expected_source[1:], start=2)
    )
    assert actual.splitlines() == expected


@requires_ast_grep
def test_ast_search_limits_and_deduplicates_results(tmp_path):
    target = tmp_path / "calls.py"
    target.write_text("foo(1)\nfoo(2)\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    first = ast_search("foo($ARG)", path="calls.py", max_results=1, context=context)
    second = ast_search("foo($ARG)", path="calls.py", max_results=1, context=context)

    assert first == f"{target}:1\n1{fs._line_hash('foo(1)')}|foo(1)"
    assert second == first + "\n[deduplicated repeated search]"


@requires_ast_grep
def test_ast_search_caps_output_at_tool_context_limit(tmp_path):
    (tmp_path / "calls.py").write_text("foo(a_very_long_argument_name)\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_tool_result_bytes=80)

    actual = ast_search("foo($ARG)", path="calls.py", context=context)

    assert len(actual.encode("utf-8")) <= 80
    assert actual.endswith("[truncated: limits.max_tool_result_bytes (80) reached]")


@requires_ast_grep
def test_ast_search_reports_no_matches_and_failures_cleanly(tmp_path):
    (tmp_path / "code.py").write_text("value = 1\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    no_match = ast_search("foo($ARG)", path="code.py", context=context)
    invalid_pattern = ast_search("$$$", path="code.py", lang="Python", context=context)
    invalid_language = ast_search("value", path="code.py", lang="Brainfuck", context=context)
    missing_path = ast_search("value", path="missing.py", context=context)

    assert no_match == "(no matches)"
    assert invalid_pattern == "ERROR: Error: Cannot parse query as a valid pattern."
    assert invalid_language == "ERROR: unsupported ast-grep language: Brainfuck"
    assert missing_path == f"ERROR: Path does not exist: {tmp_path / 'missing.py'}"


@requires_ast_grep
def test_ast_search_rewrite_defaults_to_diff_only(tmp_path):
    target = tmp_path / "code.py"
    original = "old(1)\n"
    target.write_text(original, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = ast_search(
        "old($ARG)", path="code.py", rewrite="new($ARG)", context=context
    )

    assert actual == (
        "DRY RUN: no files changed. Pass apply=true to apply.\n"
        f"--- {target}\n"
        f"+++ {target}\n"
        "@@ -1 +1 @@\n"
        "-old(1)\n"
        "+new(1)"
    )
    assert target.read_text(encoding="utf-8") == original


@requires_ast_grep
def test_ast_search_apply_snapshots_all_files_clears_cache_and_undoes(tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("old(1)\n", encoding="utf-8")
    second.write_text("old(2)\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)
    ast_search("old($ARG)", path=".", context=context)

    actual = ast_search(
        "old($ARG)", path=".", rewrite="new($ARG)", apply=True, context=context
    )

    assert actual.startswith("rewrote 2 matches in 2 files\n")
    assert first.read_text(encoding="utf-8") == "new(1)\n"
    assert second.read_text(encoding="utf-8") == "new(2)\n"
    assert ast_search("old($ARG)", path=".", context=context) == "(no matches)"
    assert undo("a.py", context=context).startswith(f"restored {first} (hash ")
    assert undo("b.py", context=context).startswith(f"restored {second} (hash ")
    assert first.read_text(encoding="utf-8") == "old(1)\n"
    assert second.read_text(encoding="utf-8") == "old(2)\n"


@requires_ast_grep
def test_ast_search_refuses_apply_past_max_results(tmp_path):
    target = tmp_path / "code.py"
    original = "old(1)\nold(2)\n"
    target.write_text(original, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = ast_search(
        "old($ARG)",
        path="code.py",
        rewrite="new($ARG)",
        apply=True,
        max_results=1,
        context=context,
    )

    assert actual == (
        "ERROR: rewrite matched more than max_results=1; "
        "narrow path or increase max_results before applying"
    )
    assert target.read_text(encoding="utf-8") == original
