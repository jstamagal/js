from __future__ import annotations

from pathlib import Path

from js.toolkit import ToolContext
from js.toolkit import wiki as wiki_module
from js.toolkit.core import call_tool
from js.toolkit.wiki import convert as wiki_convert_module
from js.toolkit.wiki import ops as wiki_ops
from js.toolkit.wiki.convert import wiki_convert
from js.toolkit.wiki.ops import (
    wiki_archive,
    wiki_finish_ingest,
    wiki_inbox,
    wiki_purpose,
    wiki_search,
)
from js.toolkit.wiki.pages import wiki_write


def _ctx(tmp_path: Path, max_bytes: int = 4096) -> ToolContext:
    return ToolContext(cwd=tmp_path, max_tool_result_bytes=max_bytes)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "wiki-test"
    vault.mkdir()
    return vault


def test_wiki_purpose_reports_counts_inbox_and_orphaned_source_pages(tmp_path):
    vault = _vault(tmp_path)
    (vault / "PURPOSE.md").write_text("Purpose line\n", encoding="utf-8")
    for name in ("sources", "entities", "concepts", "synthesis", "inbox"):
        (vault / name).mkdir()
    (vault / "inbox" / "raw.txt").write_text("raw\n", encoding="utf-8")
    (vault / "inbox" / ".hidden").write_text("hidden\n", encoding="utf-8")
    (vault / "inbox" / "_skipped").mkdir()
    (vault / "sources" / "raw-summary.md").write_text(
        '---\nsource: "inbox/raw.txt"\n---\n# Raw\n',
        encoding="utf-8",
    )
    (vault / "entities" / "person.md").write_text("# Person\n", encoding="utf-8")
    (vault / "concepts" / "idea.md").write_text("# Idea\n", encoding="utf-8")

    actual = wiki_purpose(str(vault), context=_ctx(tmp_path))

    assert f"# Vault: {vault}" in actual
    assert "Purpose line" in actual
    assert "- sources: 1" in actual
    assert "- entities: 1" in actual
    assert "- concepts: 1" in actual
    assert "- synthesis: 0" in actual
    assert "- inbox units waiting: 1" in actual
    assert "ORPHANS" in actual
    assert 'inbox/raw.txt' in actual
    assert 'call wiki_archive(vault, "raw.txt")' in actual


def test_wiki_purpose_skips_orphan_scan_in_leave_in_place_mode(tmp_path, monkeypatch):
    """The same source/inbox pairing that reports ORPHANS in normal (archiving)
    mode must NOT be flagged when leave-in-place is active — otherwise every
    successfully-ingested unit is a permanent false orphan (js-drain default)."""
    vault = _vault(tmp_path)
    for name in ("sources", "entities", "concepts", "synthesis", "inbox"):
        (vault / name).mkdir()
    (vault / "inbox" / "raw.txt").write_text("raw\n", encoding="utf-8")
    (vault / "sources" / "raw-summary.md").write_text(
        '---\nsource: "inbox/raw.txt"\n---\n# Raw\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("JS_WIKI_NO_ARCHIVE", "1")
    via_env = wiki_purpose(str(vault), context=_ctx(tmp_path))
    monkeypatch.delenv("JS_WIKI_NO_ARCHIVE", raising=False)

    context = _ctx(tmp_path)
    context.wiki_no_archive = True
    via_context_flag = wiki_purpose(str(vault), context=context)

    normal_mode = wiki_purpose(str(vault), context=_ctx(tmp_path))

    assert "ORPHANS" not in via_env
    assert "ORPHANS" not in via_context_flag
    assert "ORPHANS" in normal_mode  # sanity: guard only fires in leave-in-place mode


def test_wiki_write_override_dedup_reachable_through_declared_tool_schema(tmp_path):
    """override_dedup must be usable through the wiki_write Tool's DECLARED
    schema (params dict, what a schema-enforcing provider validates against
    with additionalProperties:false) — not just as a raw Python kwarg."""
    vault = _vault(tmp_path)
    context = _ctx(tmp_path)
    tool = next(t for t in wiki_module.tools() if t.name == "wiki_write")
    assert "override_dedup" in tool.params

    call_tool(
        tool,
        {
            "vault": str(vault),
            "kind": "entity",
            "body": "Existing shared page",
            "slug": "dayton-dcs165-4",
            "title": "Dayton DCS165-4",
        },
        context,
    )
    blocked = call_tool(
        tool,
        {
            "vault": str(vault),
            "kind": "entity",
            "body": "Duplicate-looking sibling",
            "slug": "dayton-dcs165-4-specs",
            "title": "Dayton DCS165-4 Specs",
        },
        context,
    )
    overridden = call_tool(
        tool,
        {
            "vault": str(vault),
            "kind": "entity",
            "body": "Actually distinct despite the overlap",
            "slug": "dayton-dcs165-4-specs",
            "title": "Dayton DCS165-4 Specs",
            "override_dedup": True,
        },
        context,
    )

    assert blocked.startswith("NEAR-MATCH:")
    assert "(type: entity)" in overridden
    assert (vault / "entities" / "dayton-dcs165-4-specs.md").exists()


def test_wiki_inbox_lists_only_processable_units(tmp_path):
    vault = _vault(tmp_path)
    inbox = vault / "inbox"
    inbox.mkdir()
    (inbox / ".hidden").write_text("hidden\n", encoding="utf-8")
    (inbox / "_skipped").mkdir()
    (inbox / "project").mkdir()
    (inbox / "standalone.md").write_text("body\n", encoding="utf-8")

    actual = wiki_inbox(str(vault), context=_ctx(tmp_path))

    expected = (
        f"2 unit(s) in {inbox} -- process ONE per cycle:\n"
        "  project  -- PROJECT (folder)\n"
        "  standalone.md  -- standalone file"
    )
    assert actual == expected


def test_wiki_write_frontmatter_overwrite_guard_and_upsert(tmp_path, monkeypatch):
    monkeypatch.delenv("JS_WIKI_MODE", raising=False)
    vault = _vault(tmp_path)
    context = _ctx(tmp_path)

    first = wiki_write(
        str(vault),
        "concept",
        "Initial body",
        slug="Chain of Draft Prompting",
        title="Chain of Draft",
        tags="reasoning, prompts",
        confidence="medium",
        source_count=2,
        context=context,
    )
    page = vault / "concepts" / "chain-of-draft-prompting.md"
    before = page.read_text(encoding="utf-8")

    blocked = wiki_write(
        str(vault),
        "concept",
        "Clobber body",
        slug="Chain of Draft Prompting",
        title="Chain of Draft",
        context=context,
    )
    after_blocked = page.read_text(encoding="utf-8")
    upserted = wiki_write(
        str(vault),
        "concept",
        "Merged body",
        slug="Chain of Draft Prompting",
        title="Chain of Draft",
        confidence="high",
        source_count=3,
        overwrite=True,
        context=context,
    )
    after_upsert = page.read_text(encoding="utf-8")

    assert "(type: concept)" in first
    assert page.exists()
    assert "type: concept" in before
    assert "tags: [wiki/concept, reasoning, prompts]" in before
    assert "confidence: medium" in before
    assert "source_count: 2" in before
    assert "# Chain of Draft" in before
    assert "Initial body" in before
    assert blocked.startswith("EXISTS:")
    assert after_blocked == before
    assert "(type: concept)" in upserted
    assert "confidence: high" in after_upsert
    assert "source_count: 3" in after_upsert
    assert "Merged body" in after_upsert
    assert "Initial body" not in after_upsert


def test_wiki_write_sanitizes_boolean_metadata_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv("JS_WIKI_MODE", raising=False)
    vault = _vault(tmp_path)
    context = _ctx(tmp_path)

    actual = wiki_write(
        str(vault),
        "concept",
        True,
        slug="Boolean Tags",
        tags=True,
        confidence=True,
        source_count=True,
        context=context,
    )

    page = vault / "concepts" / "boolean-tags.md"
    content = page.read_text(encoding="utf-8")

    assert actual.startswith("wrote ")
    assert "tags: [wiki/concept]" in content
    assert "source_count: 1" in content
    assert "True" not in content


def test_wiki_write_near_match_guard_for_shared_entity_and_concept_pages(tmp_path, monkeypatch):
    monkeypatch.delenv("JS_WIKI_MODE", raising=False)
    vault = _vault(tmp_path)
    context = _ctx(tmp_path)
    wiki_write(
        str(vault),
        "entity",
        "Existing shared page",
        slug="dayton-dcs165-4",
        title="Dayton DCS165-4",
        context=context,
    )

    blocked = wiki_write(
        str(vault),
        "entity",
        "Duplicate-looking sibling",
        slug="dayton-dcs165-4-specs",
        title="Dayton DCS165-4 Specs",
        context=context,
    )
    overridden = wiki_write(
        str(vault),
        "entity",
        "Actually distinct despite the overlap",
        slug="dayton-dcs165-4-specs",
        title="Dayton DCS165-4 Specs",
        override_dedup=True,
        context=context,
    )

    assert blocked.startswith("NEAR-MATCH:")
    assert "dayton-dcs165-4.md" in blocked
    assert "UPSERT into IT" in blocked
    assert "(type: entity)" in overridden
    assert (vault / "entities" / "dayton-dcs165-4-specs.md").exists()


def test_wiki_archive_leave_mode_and_finish_ingest_closeout(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    inbox = vault / "inbox"
    inbox.mkdir()
    (inbox / "raw.txt").write_text("raw\n", encoding="utf-8")

    monkeypatch.setenv("JS_WIKI_NO_ARCHIVE", "1")
    skipped = wiki_archive(str(vault), "raw.txt", context=_ctx(tmp_path))
    monkeypatch.delenv("JS_WIKI_NO_ARCHIVE", raising=False)
    monkeypatch.setenv("JS_WIKI_MODE", "ingest")

    closed = wiki_finish_ingest(
        str(vault),
        "raw.txt",
        "Raw Title",
        "closeout note",
        context=_ctx(tmp_path),
    )
    log_text = (vault / "log.md").read_text(encoding="utf-8")
    missing = wiki_finish_ingest(
        str(vault),
        "missing.txt",
        "Missing Title",
        context=_ctx(tmp_path),
    )

    assert skipped == "archive skipped (leave-in-place): inbox/raw.txt kept"
    assert closed.startswith("archived: inbox/raw.txt -> Clippings/raw.txt")
    assert "logged:" in closed
    assert "git: deferred" in closed
    assert not (inbox / "raw.txt").exists()
    assert (vault / "Clippings" / "raw.txt").read_text(encoding="utf-8") == "raw\n"
    assert "Raw Title" in log_text
    assert "closeout note" in log_text
    assert "Archived: raw.txt -> Clippings/raw.txt" in log_text
    assert missing.startswith("NOT logging")
    assert "archive failed: ERROR: no inbox unit:" in missing
    assert "Missing Title" not in log_text


def test_wiki_finish_ingest_leave_mode_validates_unit_and_logs_left_in_place(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    inbox = vault / "inbox"
    inbox.mkdir()
    (inbox / "raw.txt").write_text("raw\n", encoding="utf-8")
    monkeypatch.setenv("JS_WIKI_NO_ARCHIVE", "1")
    monkeypatch.delenv("JS_WIKI_MODE", raising=False)

    closed = wiki_finish_ingest(str(vault), "raw.txt", "Raw Title", "done note", context=_ctx(tmp_path))
    missing = wiki_finish_ingest(str(vault), "missing.txt", "Missing Title", context=_ctx(tmp_path))
    log_text = (vault / "log.md").read_text(encoding="utf-8")

    assert closed.startswith("archive skipped (leave-in-place): inbox/raw.txt kept")
    assert (inbox / "raw.txt").read_text(encoding="utf-8") == "raw\n"
    assert "Left in place: inbox/raw.txt" in log_text
    assert "Archived: raw.txt -> Clippings/raw.txt" not in log_text
    assert missing.startswith("NOT logging")
    assert "archive failed: ERROR: no inbox unit:" in missing
    assert "Missing Title" not in log_text


def test_wiki_search_constructs_qmd_collection_query(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    calls: list[list[str]] = []

    def run_stub(cmd, context):
        calls.append(cmd)
        return 0, "result one\n", ""

    monkeypatch.setattr(wiki_ops, "run", run_stub)

    actual = wiki_search(str(vault), "needle", context=_ctx(tmp_path))

    assert actual == "result one"
    assert calls == [["qmd", "query", "needle", "-c", "wiki-test"]]


def test_wiki_search_sanitizes_boolean_query(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    calls: list[list[str]] = []

    def run_stub(cmd, context):
        calls.append(cmd)
        return 0, "found\n", ""

    monkeypatch.setattr(wiki_ops, "run", run_stub)

    actual = wiki_search(str(vault), True, context=_ctx(tmp_path))

    assert actual == "found"
    assert calls == [["qmd", "query", "", "-c", "wiki-test"]]

def test_wiki_convert_reads_text_peeks_structured_files_and_copies_media(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    note = tmp_path / "note.md"
    data = tmp_path / "data.json"
    image = tmp_path / "photo.png"
    note.write_text("plain text\n", encoding="utf-8")
    data.write_text("\n".join(f'{{"line": {i}}}' for i in range(45)), encoding="utf-8")
    image.write_bytes(b"fake-png-bytes")
    run_calls: list[list[str]] = []

    def run_stub(cmd, context):
        run_calls.append(cmd)
        return 0, "ocr words\n", ""

    monkeypatch.setattr(wiki_convert_module, "run", run_stub)

    text_actual = wiki_convert(str(note), context=_ctx(tmp_path))
    structured_actual = wiki_convert(str(data), context=_ctx(tmp_path, max_bytes=2048))
    image_actual = wiki_convert(str(image), vault=str(vault), context=_ctx(tmp_path))

    assert text_actual == "plain text\n"
    assert '{"line": 0}' in structured_actual
    assert '{"line": 39}' in structured_actual
    assert '{"line": 40}' not in structured_actual
    assert "--- (45 lines total; first 40 shown) ---" in structured_actual
    assert image_actual == "MEDIA image. embed: ![[photo.png]]\n--- OCR (tesseract) ---\nocr words"
    assert (vault / "assets" / "photo.png").read_bytes() == b"fake-png-bytes"
    assert run_calls == [["tesseract", str(image), "stdout"]]


def test_wiki_convert_fallback_tests_file_description_not_the_path(tmp_path, monkeypatch):
    """`file` prints '<path>: <desc>'. A binary living under a path containing
    "text" (e.g. .../context/...) must classify off the description, not the path."""
    sub = tmp_path / "context"
    sub.mkdir()
    blob = sub / "thing.bin"
    blob.write_bytes(b"\x00\x01\x02BOOT")

    def run_stub(cmd, context):
        return 0, f"{cmd[-1]}: DOS/MBR boot sector", ""

    monkeypatch.setattr(wiki_convert_module, "run", run_stub)

    actual = wiki_convert(str(blob), context=_ctx(tmp_path))
    assert actual.startswith("UNREADABLE/binary:")


def test_wiki_convert_soffice_failure_does_not_return_stale_tmp_output(tmp_path, monkeypatch):
    """soffice writes /tmp/<stem>.txt; on a failed conversion a stale same-stem file
    from a prior run must not be returned as this file's content."""
    from pathlib import Path as _P

    doc = tmp_path / "zzstalestem98765.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0garbage-ole")   # bogus .doc
    stale = _P("/tmp") / "zzstalestem98765.txt"
    stale.write_text("STALECONTENT", encoding="utf-8")

    def run_stub(cmd, context):
        return 1, "", "source file could not be loaded"   # soffice fails, writes nothing

    monkeypatch.setattr(wiki_convert_module, "run", run_stub)
    try:
        actual = wiki_convert(str(doc), context=_ctx(tmp_path))
        assert "STALECONTENT" not in actual
        assert actual.startswith("ERROR soffice:")
    finally:
        stale.unlink(missing_ok=True)
