"""Only discovered theme files become entries in the theme cache."""

import pytest

from js import fset, paths


@pytest.fixture
def themes_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    themes = tmp_path / "themes"
    themes.mkdir()
    return themes


def test_missing_theme_can_be_created_after_lookup(themes_dir):
    with pytest.raises(ValueError):
        fset.theme_entries("new-theme")
    with pytest.raises(ValueError):
        fset.theme_entries("new-theme")
    (themes_dir / "new-theme").write_text("fset PALETTE_04 #123456\n", encoding="utf-8")
    assert fset.theme_entries("new-theme") == {"PALETTE_04": "#123456"}


@pytest.mark.parametrize("content, expected", [
    ("fset PALETTE_04 #123456\n", {"PALETTE_04": "#123456"}),
    ("", {}),
])
def test_existing_theme_keeps_cached_read_semantics(monkeypatch, themes_dir, content, expected):
    path = themes_dir / "existing"
    path.write_text(content, encoding="utf-8")
    read_file = fset.read_file
    reads = []

    def counted(path):
        reads.append(path)
        return read_file(path)

    monkeypatch.setattr(fset, "read_file", counted)
    assert fset.theme_entries("existing") == expected
    path.write_text("fset PALETTE_04 #abcdef\n", encoding="utf-8")
    assert fset.theme_entries("existing") == expected
    assert reads == [path]
