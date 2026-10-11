"""A broken SKILL.md file must be skipped, not crash discovery.

ALL-CLAIMS skills.py HIGH: discover_skills catches only ValueError from
_index_skill; a dangling symlink, a mode-000 file or a SKILL.md that is a
directory raises OSError and stops the whole session from starting.
"""

from pathlib import Path
import os

import pytest

from js.skills import discover_skills


def _skill(root: Path, name: str, body: str = "---\nname: ok\ndescription: fine\n---\nbody\n") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_dangling_symlink_skill_is_skipped(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "good")
    bad = root / "dangling" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.symlink_to(tmp_path / "no-such-target")
    catalog = discover_skills(tmp_path, builtin_dir=root, global_dir=tmp_path / "none", user_dir=tmp_path / "none2")
    assert [r.name for r in catalog.skills] == ["ok"]


def test_directory_as_skill_md_is_skipped(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "good")
    (root / "isdir" / "SKILL.md").mkdir(parents=True)
    catalog = discover_skills(tmp_path, builtin_dir=root, global_dir=tmp_path / "none", user_dir=tmp_path / "none2")
    assert [r.name for r in catalog.skills] == ["ok"]


def test_unreadable_skill_md_is_skipped(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "good")
    bad = _skill(root, "dark", "---\nname: dark\ndescription: d\n---\nx\n")
    os.chmod(bad, 0o000)
    try:
        catalog = discover_skills(tmp_path, builtin_dir=root, global_dir=tmp_path / "none", user_dir=tmp_path / "none2")
    finally:
        os.chmod(bad, 0o644)
    assert [r.name for r in catalog.skills] == ["ok"]
