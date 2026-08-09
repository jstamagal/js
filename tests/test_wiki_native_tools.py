from __future__ import annotations

from pathlib import Path

from js.toolkit import ToolContext
from js.toolkit import wiki as wiki_module
from js.toolkit.core import call_tool
from js.toolkit.wiki import convert as wiki_convert_module
from js.toolkit.wiki.convert import wiki_convert
from js.toolkit.wiki.ops import wiki_finish_ingest
from js.toolkit.wiki.pages import wiki_write


def _ctx(tmp_path: Path, max_bytes: int = 4096) -> ToolContext:
    return ToolContext(cwd=tmp_path, max_tool_result_bytes=max_bytes)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "wiki-test"
    vault.mkdir()
    return vault


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


def test_wiki_write_frontmatter_overwrite_guard_and_upsert(tmp_path):
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
    context.remember_read(page, "test")
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
    assert 'tags: ["wiki/concept", "reasoning", "prompts"]' in before
    assert 'confidence: "medium"' in before
    assert "source_count: 2" in before
    assert "# Chain of Draft" in before
    assert "Initial body" in before
    assert blocked.startswith("EXISTS:")
    assert after_blocked == before
    assert wiki_write(str(vault), "concept", "unsafe", slug="Chain of Draft Prompting", overwrite=True, context=_ctx(tmp_path)).startswith("ERROR: You must read")
    assert "(type: concept)" in upserted
    assert 'confidence: "high"' in after_upsert
    assert "source_count: 3" in after_upsert
    assert "Merged body" in after_upsert
    assert "Initial body" not in after_upsert


def test_wiki_write_sanitizes_boolean_metadata_inputs(tmp_path):
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
    assert 'tags: ["wiki/concept"]' in content
    assert "source_count: 1" in content
    assert "True" not in content


def test_wiki_write_near_match_guard_for_shared_entity_and_concept_pages(tmp_path):
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


def test_wiki_finish_ingest_archives_logs_and_rejects_traversal(tmp_path):
    vault = _vault(tmp_path)
    (vault / "PURPOSE.md").write_text("purpose\n")
    (vault / "inbox").mkdir()
    (vault / "inbox" / "unit.md").write_text("raw\n")

    result = wiki_finish_ingest(str(vault), "unit.md", "Unit", "one source", context=_ctx(tmp_path))

    assert "archived:" in result
    assert not (vault / "inbox" / "unit.md").exists()
    assert (vault / "Clippings" / "unit.md").read_text() == "raw\n"
    assert "one source" in (vault / "log.md").read_text()
    assert wiki_finish_ingest(str(vault), "../escape", "Bad", context=_ctx(tmp_path)).startswith("ERROR")


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
