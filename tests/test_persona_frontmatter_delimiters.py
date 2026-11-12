"""Runtime and legacy migration agree on exact frontmatter delimiter lines."""

import pytest

from js import agent_migration, persona


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_empty_frontmatter_closes_on_immediate_following_line(tmp_path, newline):
    text = newline.join(["---", "---", "", "  body", "", ""])
    data, body = persona._split_frontmatter(tmp_path / "prompt.md", text)
    assert data == {}
    assert body == newline + "  body"


@pytest.mark.parametrize("delimiter", ["----", "---x", "--- ", " ---"])
def test_delimiter_prefix_does_not_close_frontmatter(tmp_path, delimiter):
    text = "---\nmodel: unit\n" + delimiter + "\nbody\n"
    with pytest.raises(ValueError):
        persona._split_frontmatter(tmp_path / "prompt.md", text)
    (tmp_path / "00-prompt.md").write_text(text)
    result, old, body = agent_migration.plan(tmp_path, lambda _name: True)
    assert result.action == "error"
    assert old is None and body is None


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("yaml_text", ["", "model: unit\n# preserved comment\n"])
def test_migration_uses_same_yaml_and_body_boundaries(tmp_path, newline, yaml_text):
    path = tmp_path / "00-prompt.md"
    text = "---\n" + yaml_text + "---\n  body\n\n"
    path.write_bytes(text.replace("\n", newline).encode())
    result, old, body = agent_migration.plan(tmp_path, lambda _name: True)
    assert result.action == "migrate"
    assert old == path
    assert body == "  body"
    if yaml_text:
        assert "# preserved comment" in result.manifest_text


def test_frontmatter_can_close_at_end_of_file(tmp_path):
    assert persona._split_frontmatter(tmp_path / "prompt.md", "---\n---") == ({}, "")


def test_nonfrontmatter_keeps_existing_body_trimming(tmp_path):
    assert persona._split_frontmatter(tmp_path / "prompt.md", "  body\n\n") == (None, "  body")
