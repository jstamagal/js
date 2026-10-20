"""A broken SKILL.md file must be skipped, not crash discovery.

ALL-CLAIMS skills.py HIGH: discover_skills catches only ValueError from
_index_skill; a dangling symlink, a mode-000 file or a SKILL.md that is a
directory raises OSError and stops the whole session from starting.
"""

from pathlib import Path
import os


from js.skills import discover_skills


def test_unsearchable_skill_root_parent_does_not_abort_other_roots(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "good")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0)
    try:
        catalog = discover_skills(
            tmp_path, builtin_dir=root, global_dir=blocked / "skills",
            user_dir=tmp_path / "none",
        )
    finally:
        blocked.chmod(0o700)
    assert [record.name for record in catalog.skills] == ["ok"]


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


def test_glob_match_is_bounded_on_hostile_patterns(monkeypatch):
    """A path skill glob with many ``**`` segments must stay cheap.

    ALL-CLAIMS skills.py MED: naive recursion over every split point per
    ``**`` took 6.2 s for one matches_path against a hostile SKILL.md. The
    memo bounds work to (len(parts) + 1) * (len(pattern) + 1) states.
    """
    import js.skills as skills
    from js.skills import _glob_match

    calls = {"n": 0}
    real = skills.fnmatchcase

    def counted(value, pattern):
        calls["n"] += 1
        return real(value, pattern)

    monkeypatch.setattr(skills, "fnmatchcase", counted)

    alternating = tuple(x for _ in range(10) for x in ("**", "a")) + ("z",)
    assert _glob_match(("a",) * 18, alternating) is False
    bound = (18 + 1) * (len(alternating) + 1)
    assert calls["n"] <= bound, (calls["n"], bound)

    calls["n"] = 0
    adjacent = ("**",) * 12 + ("z",)
    assert _glob_match(("a",) * 14, adjacent) is False
    assert calls["n"] <= (14 + 1) * (len(adjacent) + 1), calls["n"]


def test_glob_match_semantics_unchanged():
    from js.skills import _glob_match

    assert _glob_match(("a", "b", "c"), ("**", "c"))
    assert _glob_match(("a", "b", "c"), ("**", "b", "**"))
    assert _glob_match(("a", "b", "c"), ("a", "**"))
    assert _glob_match(("a", "b", "c"), ("**",))
    assert _glob_match((), ("**",))
    assert not _glob_match(("a",), ())
    # one pattern component per path component: ('x', '?', 'y') needs three.
    assert _glob_match(("x", "q", "y"), ("x", "?", "y"))
    assert not _glob_match(("x", "y"), ("x", "?", "y"))
    assert _glob_match(("a", "a", "a"), ("**", "a", "**", "a", "**"))
    assert not _glob_match(("a", "b"), ("**", "a", "a"))
