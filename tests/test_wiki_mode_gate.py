"""The JS_WIKI_MODE tool-layer gate: ingest writes only source pages, synthesize
never re-ingests. This is the enforcement behind the two-pass contract — prompt
text alone can't stop a misbehaving cheap model, this can."""
from __future__ import annotations

from js.toolkit import ToolContext
from js.toolkit.wiki.pages import wiki_write


def _ctx(tmp_path):
    return ToolContext(cwd=tmp_path)


def _vault(tmp_path):
    p = tmp_path / "vault"
    p.mkdir(exist_ok=True)  # wiki_write refuses a non-existent vault root
    return str(p)


def test_ingest_mode_rejects_entity_concept_synthesis(monkeypatch, tmp_path):
    monkeypatch.setenv("JS_WIKI_MODE", "ingest")
    v = _vault(tmp_path)
    for kind in ("entity", "concept", "synthesis"):
        r = wiki_write(v, kind, "body", slug=f"x-{kind}", title="X", context=_ctx(tmp_path))
        assert r.startswith("ERROR"), (kind, r)
    # source is the one allowed kind in ingest mode
    ok = wiki_write(v, "source", "body", slug="s", title="S",
                    source="Clippings/s", context=_ctx(tmp_path))
    assert not ok.startswith("ERROR"), ok
    assert (tmp_path / "vault" / "sources" / "s.md").exists()
    assert not (tmp_path / "vault" / "concepts").exists()


def test_synthesize_mode_rejects_source(monkeypatch, tmp_path):
    monkeypatch.setenv("JS_WIKI_MODE", "synthesize")
    v = _vault(tmp_path)
    r = wiki_write(v, "source", "body", slug="s", title="S",
                   source="Clippings/s", context=_ctx(tmp_path))
    assert r.startswith("ERROR"), r
    # entity/concept/synthesis all allowed in synthesize mode
    for kind in ("entity", "concept", "synthesis"):
        ok = wiki_write(v, kind, "body", slug=f"k-{kind}", title="K", context=_ctx(tmp_path))
        assert not ok.startswith("ERROR"), (kind, ok)


def test_no_mode_is_permissive(monkeypatch, tmp_path):
    # the normal `js` agent (no wiki mode) can still write any kind
    monkeypatch.delenv("JS_WIKI_MODE", raising=False)
    v = _vault(tmp_path)
    for kind in ("source", "entity", "concept", "synthesis"):
        r = wiki_write(v, kind, "body", slug=f"k-{kind}", title="K",
                       source="Clippings/x", context=_ctx(tmp_path))
        assert not r.startswith("ERROR"), (kind, r)


def test_query_lint_modes_are_permissive(monkeypatch, tmp_path):
    # query mode files synthesis pages; lint touches anything — must not be gated
    v = _vault(tmp_path)
    for mode in ("query", "lint"):
        monkeypatch.setenv("JS_WIKI_MODE", mode)
        ok = wiki_write(v, "synthesis", "body", slug=f"syn-{mode}", title="Y", context=_ctx(tmp_path))
        assert not ok.startswith("ERROR"), (mode, ok)
