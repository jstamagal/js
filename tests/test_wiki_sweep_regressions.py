import pytest

from js.toolkit.core import ToolContext
from js.toolkit.wiki.helpers import slugify
from js.toolkit.wiki.pages import wiki_write


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("a" * 59 + " bbb", "a" * 59),
        ("a" * 60 + " bbb", "a" * 60),
        ("a" * 58 + " bbb", "a" * 58 + "-b"),
        ("!!! " + "a" * 70, "a" * 60),
        ("!!!", "untitled"),
        ("", "untitled"),
    ],
)
def test_slugify_has_no_edge_separator_after_truncation(title, expected):
    result = slugify(title)
    assert result == expected
    assert 0 < len(result) <= 60
    assert not result.startswith("-")
    assert not result.endswith("-")


@pytest.mark.parametrize("flag", [False, "false", "False", "0", "no", "off", True, "true", "1"])
@pytest.mark.parametrize("name", ["overwrite", "override_dedup"])
def test_wiki_write_coerces_boolean_flags_for_direct_calls(tmp_path, flag, name):
    vault = tmp_path / "vault"
    vault.mkdir()
    context = ToolContext(cwd=tmp_path, max_tool_result_bytes=4096)
    initial = wiki_write(str(vault), "concept", "original content", slug="chain-of-draft", context=context)
    assert initial.startswith("wrote ")
    original_page = vault / "concepts" / "chain-of-draft.md"
    original_content = original_page.read_text()
    target = "chain-of-draft" if name == "overwrite" else "chain-of-draft-prompting"
    page = vault / "concepts" / f"{target}.md"
    enabled = flag is True or flag in ("true", "1")

    result = wiki_write(str(vault), "concept", "new content", slug=target, context=context, **{name: flag})

    if enabled:
        assert result.startswith("overwrote " if name == "overwrite" else "wrote ")
        assert "new content" in page.read_text()
    else:
        assert result.startswith("EXISTS:" if name == "overwrite" else "NEAR-MATCH:")
        assert original_page.read_text() == original_content
        if name == "override_dedup":
            assert not page.exists()
