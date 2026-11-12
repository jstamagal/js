from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import threading

import pytest

from js import media


def test_concurrent_stores_use_independent_partial_files(tmp_path, monkeypatch):
    tmp_path = tmp_path / "media"
    tmp_path.mkdir()
    monkeypatch.setattr(media.paths, "media_dir", lambda: tmp_path)
    write_bytes = Path.write_bytes
    barrier = threading.Barrier(4)
    data = b"same media" * 1000

    def synchronized_write(path, payload):
        result = write_bytes(path, payload)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Path, "write_bytes", synchronized_write)
    with ThreadPoolExecutor(max_workers=4) as executor:
        parts = list(executor.map(lambda _: media.store(data, "image/png"), range(4)))

    digest = hashlib.sha256(data).hexdigest()
    assert parts == [{"type": "media", "sha256": digest, "mime": "image/png", "size": len(data)}] * 4
    assert (tmp_path / digest).read_bytes() == data
    assert all(media.load(part) == data for part in parts)
    assert list(tmp_path.iterdir()) == [tmp_path / digest]


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_store_failure_removes_partial_file(tmp_path, monkeypatch, failure):
    tmp_path = tmp_path / "media"
    tmp_path.mkdir()
    monkeypatch.setattr(media.paths, "media_dir", lambda: tmp_path)
    write_bytes = Path.write_bytes

    def failed_write(path, data):
        write_bytes(path, data[:2])
        raise OSError("write failed")

    def failed_replace(partial, destination):
        write_bytes(destination, b"existing data")
        raise OSError("replace failed")

    if failure == "write":
        monkeypatch.setattr(Path, "write_bytes", failed_write)
    else:
        monkeypatch.setattr(media.os, "replace", failed_replace)
    with pytest.raises(OSError, match=f"{failure} failed"):
        media.store(b"sample media", "image/png")
    if failure == "replace":
        destination = tmp_path / hashlib.sha256(b"sample media").hexdigest()
        assert destination.read_bytes() == b"existing data"
        assert list(tmp_path.iterdir()) == [destination]
    else:
        assert list(tmp_path.iterdir()) == []
