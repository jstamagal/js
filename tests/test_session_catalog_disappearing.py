"""Concurrent session removal cannot abort a catalog of surviving sessions."""

from js import memory, session_catalog


def test_catalog_keeps_neighbors_when_enumerated_session_disappears(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    paths = [root / "agent" / f"{name}.jsonl" for name in ("a", "b", "c")]
    for path in paths:
        memory.append_message(path, {"role": "user", "content": path.stem})
    victim = paths[1]
    real_files = session_catalog._session_files

    def disappearing_files(directory):
        candidates = list(real_files(directory))
        victim.unlink()
        yield from candidates

    monkeypatch.setattr(session_catalog, "_session_files", disappearing_files)
    directories = {path for path in root.rglob("*") if path.is_dir()}

    records = session_catalog.catalog_sessions(root)

    assert [record["path"] for record in records] == [str(paths[0]), str(paths[2])]
    assert [record["agent"] for record in records] == ["agent", "agent"]
    assert all(record["user_turns"] == 1 for record in records)
    assert all(record["size"] == paths[index].stat().st_size for record, index in zip(records, [0, 2], strict=True))
    assert {path for path in root.rglob("*") if path.is_dir()} == directories
    assert not victim.exists()


def test_catalog_all_candidates_disappear_without_recreating_paths(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    victim = root / "agent" / "one.jsonl"
    victim.parent.mkdir(parents=True)
    victim.write_text('{"role":"user","content":"gone"}\n', encoding="utf-8")
    real_files = session_catalog._session_files

    def disappearing_files(directory):
        candidates = list(real_files(directory))
        victim.unlink()
        victim.parent.rmdir()
        yield from candidates

    monkeypatch.setattr(session_catalog, "_session_files", disappearing_files)

    assert session_catalog.catalog_sessions(root) == []
    assert not victim.parent.exists()


def test_catalog_missing_root_stays_absent(tmp_path):
    root = tmp_path / "missing" / "sessions"

    assert session_catalog.catalog_sessions(root) == []
    assert not root.parent.exists()
