"""Invalid UTF-8 content returns an error before files or undo state change."""


import pytest

from js.toolkit import fs
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("surrogate", ["\ud83d", "\udcff"])
@pytest.mark.parametrize("operation", ["new", "overwrite", "patch", "batch"])
def test_invalid_surrogate_preserves_file_and_undo_state(tmp_path, surrogate, operation):
    target = tmp_path / "nested" / "file.txt"
    context = ToolContext(cwd=tmp_path)
    if operation != "new":
        target.parent.mkdir()
        target.write_bytes(b"original\r\n")
        target.chmod(0o640)
        fs.read(str(target), context=context)
        # Keep a real earlier undo entry: a rejected operation must not alter it.
        assert fs.write(str(target), content="before\n", overwrite=True, context=context).startswith("wrote ")
    original = target.read_bytes() if target.exists() else None
    prior_stat = target.stat() if target.exists() else None
    entries = context.undo.count(target)
    coverage = context.reads.coverage(target)

    if operation in {"new", "overwrite"}:
        result = fs.write(str(target), content=f"invalid {surrogate}\n", overwrite=True, context=context)
    elif operation == "patch":
        result = fs.patch(str(target), old_string="before", new_string=f"invalid {surrogate}", context=context)
    else:
        result = fs.patch(str(target), edits=[
            {"old_string": "before", "new_string": "valid intermediate"},
            {"old_string": "valid intermediate", "new_string": f"invalid {surrogate}"},
        ], context=context)

    assert result.startswith("ERROR:")
    assert context.undo.count(target) == entries
    assert context.reads.coverage(target) == coverage
    if original is None:
        assert not target.exists()
        assert not target.parent.exists()
    else:
        assert target.read_bytes() == original
        after_stat = target.stat()
        assert (after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_mode) == (
            prior_stat.st_ino, prior_stat.st_mtime_ns, prior_stat.st_mode,
        )
        assert fs.undo(str(target), context=context).startswith("restored ")
        assert target.read_bytes() == b"original\r\n"
