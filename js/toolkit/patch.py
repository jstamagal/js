"""The patch tool: exact string replacement in one file, with a fuzzy match
over quotes, dashes and whitespace, behind the read-before-edit guard."""

from __future__ import annotations

import difflib
from pathlib import Path

from .core import ToolContext, view as _view
from .fs import (
    _read_regular_bytes,
    changed_since_read,
    detect_line_ending,
    hash_bytes,
    normalize_line_endings,
    unified_diff,
)
from .path_hints import no_such_file
from .reads import require_read


def _normalize_edit(raw: object, label: str) -> tuple[str, str, bool] | str:
    """Validate one edit's *shape* — the part that does not depend on file content.

    Returns ``(old, new, replace_all)`` or an ERROR string. Runs for every edit
    before the file is touched, so a malformed batch is rejected without a read
    or a write. ``search``/``content`` are accepted as aliases here for the same
    reason they are accepted at the top level: models emit both spellings.
    """
    if not isinstance(raw, dict):
        return f"ERROR: {label}each edit must be an object with old_string and new_string"
    old = raw.get("old_string") if raw.get("old_string") is not None else raw.get("search")
    new = raw.get("new_string") if raw.get("new_string") is not None else raw.get("content")
    if old is None or new is None:
        return f"ERROR: {label}every edit requires old_string and new_string"
    old, new = str(old), str(new)
    return old, new, bool(raw.get("replace_all", False))


def _nearest_hint(text: str, old: str) -> str:
    """The closest line in the file to the first line of a missed old_string.

    A miss is almost always a near-miss: one changed character, different
    whitespace, a line that moved. Showing the model the closest real line and
    its number turns a blind retry into a fix."""
    first = next((line for line in old.splitlines() if line.strip()), "").strip()
    if not first:
        return ' Read the file again.'
    lines = text.splitlines()
    best = difflib.get_close_matches(first, [line.strip() for line in lines], n=1, cutoff=0.6)
    if not best:
        return ' Nothing similar is in the file; read it again.'
    for number, line in enumerate(lines, 1):
        if line.strip() == best[0]:
            return f' Closest line is {number}: {line!r}. Anchor old_string on the file as it is now.'
    return ' Read the file again.'


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    start_line = normalize_line_endings(text[:start], "\n").count("\n") + 1
    end_line = normalize_line_endings(text[:end], "\n").count("\n") + 1
    if end > start and text[end - 1] in "\r\n":
        end_line -= 1
    total_lines = max(1, len(text.splitlines()))
    return start_line, min(end_line, total_lines)


def _count_overlapping(text: str, needle: str) -> int:
    """Occurrences of *needle* in *text*, counting overlapping ones.

    ``str.count`` advances past each match, so ``"ababa".count("aba")`` is 1 even
    though ``aba`` starts at both offset 0 and offset 2. Uniqueness must be
    judged on the real set of possible matches."""
    count = 0
    start = text.find(needle)
    while start != -1:
        count += 1
        start = text.find(needle, start + 1)
    return count


def _transform_read_ranges(
    ranges: list[tuple[int, int]],
    start: int,
    end: int,
    line_delta: int,
) -> list[tuple[int, int]]:
    transformed: list[tuple[int, int]] = []
    for seen_start, seen_end in ranges:
        if seen_start < start:
            transformed.append((seen_start, min(seen_end, start - 1)))
        if seen_end > end:
            transformed.append((max(seen_start, end + 1) + line_delta, seen_end + line_delta))
    transformed.append((start, max(start, end + line_delta)))
    transformed.sort()
    merged: list[tuple[int, int]] = []
    for seen_start, seen_end in transformed:
        if seen_end < seen_start:
            continue
        if not merged or seen_start > merged[-1][1] + 1:
            merged.append((seen_start, seen_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], seen_end))
    return merged


# Characters the fuzzy match reads as their ASCII form: smart quotes, Unicode
# dashes and minus, and the no-break and typographic spaces. Each one maps to
# one character, so a line's length is kept up to its trailing whitespace.
_FUZZY_CHARS = str.maketrans({
    **dict.fromkeys("\u2018\u2019\u201a\u201b", "'"),
    **dict.fromkeys("\u201c\u201d\u201e\u201f", '"'),
    **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    **dict.fromkeys("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
                    "\u202f\u205f\u3000", " "),
})


def _line_parts(line: str) -> tuple[str, str, str]:
    """A line split into its text, its trailing whitespace and its line ending."""
    content = line.rstrip("\r\n")
    body = content.rstrip()
    return body, content[len(body):], line[len(content):]


def _fuzzy_view(text: str) -> tuple[str, list[int]]:
    """*text* with each line's trailing whitespace dropped and _FUZZY_CHARS
    replaced, and for each character of that view the index in *text* of the
    character it stands for."""
    view: list[str] = []
    origin: list[int] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        body, trailing, ending = _line_parts(line)
        view.append(body.translate(_FUZZY_CHARS))
        origin.extend(range(offset, offset + len(body)))
        offset += len(body) + len(trailing)
        view.append(ending)
        origin.extend(range(offset, offset + len(ending)))
        offset += len(ending)
    return "".join(view), origin


def _keep_untouched_chars(original: str, old: str, new: str) -> str:
    """*new*, one line, with the characters it keeps from *old* taken from
    *original*, the file's line that *old* matched. A kept character the file
    writes in a non-ASCII form stays in its ASCII form where the edit inserts
    that same ASCII character right beside it, so lengthening an en dash to
    ``--`` gives ``--``."""
    orig_body, orig_trailing, orig_ending = _line_parts(original)
    old_body, old_trailing, old_ending = _line_parts(old)
    new_body, new_trailing, new_ending = _line_parts(new)
    if len(orig_body) != len(old_body):
        return new
    opcodes = difflib.SequenceMatcher(None, old_body, new_body, autojunk=False).get_opcodes()
    body: list[str] = []
    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag != "equal":
            body.append(new_body[j1:j2])
            continue
        kept = list(orig_body[i1:i2])
        before = new_body[opcodes[index - 1][3]:opcodes[index - 1][4]] if index else ""
        after = new_body[opcodes[index + 1][3]:opcodes[index + 1][4]] if index + 1 < len(opcodes) else ""
        position = i1
        while position < i2 and orig_body[position] != old_body[position] and before.endswith(old_body[position]):
            kept[position - i1] = old_body[position]
            position += 1
        position = i2 - 1
        while position >= i1 and orig_body[position] != old_body[position] and after.startswith(old_body[position]):
            kept[position - i1] = old_body[position]
            position -= 1
        body.append("".join(kept))
    trailing = orig_trailing if old_trailing == new_trailing else new_trailing
    ending = orig_ending if old_ending == new_ending else new_ending
    return "".join(body) + trailing + ending


def _keep_untouched(original: str, old: str, new: str) -> str:
    """The replacement for *original*, the file text that *old* matched after
    fuzzy normalisation: *new*, with every line and character the edit left as
    it was in *old* taken from *original*, byte for byte."""
    orig_lines = original.splitlines(keepends=True)
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    if len(orig_lines) == len(old_lines) - 1:
        # The whitespace-only last line of *old* matched no whitespace in the
        # file, so the match ends at the line ending before it.
        orig_lines.append("")
    if len(orig_lines) != len(old_lines):
        return new
    out: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            out.extend(orig_lines[i1:i2])
        elif tag == "replace" and i2 - i1 == j2 - j1:
            out.extend(
                _keep_untouched_chars(orig, before, after)
                for orig, before, after in zip(
                    orig_lines[i1:i2], old_lines[i1:i2], new_lines[j1:j2], strict=True
                )
            )
        else:
            out.extend(new_lines[j1:j2])
    return "".join(out)


def _widen_over_whitespace(
    text: str, old: str, positions: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Fuzzy match spans in *text* grown over the whitespace _fuzzy_view drops
    from *old*: the end over up to as many whitespace characters as *old*'s
    last line ends with, when that line has no line ending, and the start back
    over up to as many as *old*'s first line holds, when that line is only
    whitespace. A span never grows into its neighbour."""
    lines = old.splitlines(keepends=True)
    first_body, first_trailing, first_ending = _line_parts(lines[0])
    last_body, last_trailing, last_ending = _line_parts(lines[-1])
    back = len(first_trailing) if first_ending and not first_body else 0
    forward = 0 if last_ending else len(last_trailing)

    def blank(char: str) -> bool:
        return char.isspace() and char not in "\r\n"

    widened: list[tuple[int, int]] = []
    for index, (start, end) in enumerate(positions):
        floor = widened[-1][1] if widened else 0
        ceiling = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        for _ in range(back):
            if start <= floor or not blank(text[start - 1]):
                break
            start -= 1
        for _ in range(forward):
            if end >= ceiling or not blank(text[end]):
                break
            end += 1
        widened.append((start, end))
    return widened


def _apply_edit(
    text: str,
    old: str,
    new: str,
    replace_all: bool,
    label: str,
    *,
    target: Path,
    context: ToolContext,
    seen_ranges: list[tuple[int, int]],
    content_hash: str | None = None,
    stale: str | None = None,
) -> tuple[str, int, list[tuple[int, int]], bool] | str:
    """Apply one replacement to ``text`` in memory. Returns the updated text,
    its match count, the read coverage moved to it, and whether the match was
    fuzzy; or an ERROR string. Line endings are normalised per edit against the
    text as it stands *now*, so a later edit sees an earlier one's result.

    ``stale`` is the changed-since-read diff for the file, or None. It is the
    error for an edit that misses, and for one that is not self-evident; an
    exact match of EDIT_READ_BYPASS_CHARACTERS or more is applied without it and
    without the read ledger.

    When ``old`` is not in the text exactly, it is matched in _fuzzy_view of
    both, each match grows over the whitespace that view dropped from ``old``
    (_widen_over_whitespace), and the replacement keeps the file's own bytes
    wherever the edit left ``old`` as it was (_keep_untouched)."""
    line_ending = detect_line_ending(text)
    if any(char in text or char in new for char in "\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        return f"ERROR: {label}unsupported line separator; patch requires LF, CRLF, or CR lines"
    old_norm = normalize_line_endings(old, line_ending)
    new_norm = normalize_line_endings(new, line_ending)
    if not old_norm:
        return f"ERROR: {label}old_string cannot be empty"
    # patch leaves out an edit whose old_string equals its new_string before
    # it gets here, so every edit applied changes the text.
    haystack, needle, origin = text, old_norm, None
    occurrences = _count_overlapping(text, old_norm)
    if occurrences == 0 and context.edit_fuzzy:
        haystack, origin = _fuzzy_view(text)
        needle = _fuzzy_view(old_norm)[0]
        occurrences = _count_overlapping(haystack, needle) if needle else 0
    if occurrences == 0:
        return stale or f'ERROR: {label}Could not find match for search text: {old!r}.{_nearest_hint(text, old_norm)}'
    if occurrences > 1 and not replace_all:
        return f'ERROR: {label}Multiple matches found for search text: {old!r}. Either provide a more specific search pattern or use replace_all.'
    positions: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = haystack.find(needle, cursor)
        if start < 0:
            break
        end = start + len(needle)
        positions.append((start, end) if origin is None else (origin[start], origin[end - 1] + 1))
        cursor = end
        if not replace_all:
            break
    fuzzy = origin is not None
    if fuzzy:
        positions = _widen_over_whitespace(text, old_norm, positions)
    replacements = [
        _keep_untouched(text[start:end], old_norm, new_norm) if fuzzy else new_norm
        for start, end in positions
    ]
    if fuzzy and all(text[start:end] == replacement
                     for (start, end), replacement in zip(positions, replacements, strict=True)):
        return (
            f'ERROR: {label}old_string matched only after normalising quotes, dashes and whitespace, and new_string differs from it only there, so this edit would not change the file'
        )
    # An exact match of EDIT_READ_BYPASS_CHARACTERS or more is its own proof:
    # the model reproduced those bytes and they are in the file now, so it needs
    # no prior read. A shorter or fuzzy match needs the lines shown by a read.
    if fuzzy or len(old_norm) < context.edit_read_bypass_characters:
        if stale:
            return stale
        guard = require_read(context, target, "edit it", content_hash=content_hash)
        if guard:
            return guard
        line_ranges = [_line_span(text, start, end) for start, end in positions]
        guard = require_read(context, target, "edit it", line_ranges=line_ranges, seen_ranges=seen_ranges)
        if guard:
            return f"ERROR: {label}{guard.removeprefix('ERROR: ')}"
    updated = text
    transformed = seen_ranges
    line_delta = new_norm.count(line_ending) - old_norm.count(line_ending)
    for (start, end), replacement in reversed(list(zip(positions, replacements, strict=True))):
        start_line, end_line = _line_span(updated, start, end)
        updated = updated[:start] + replacement + updated[end:]
        transformed = _transform_read_ranges(transformed, start_line, end_line, line_delta)
    return updated, len(positions), transformed, fuzzy


def patch(
    path: str | None = None,
    file_path: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    search: str | None = None,
    content: str | None = None,
    replace_all: bool = False,
    edits: list[dict] | None = None,
    context: ToolContext | None = None,
) -> str:
    """Exact string replacement in one file, in one of two forms.

    Scalar (the common case)::

        patch(file_path, old_string, new_string, replace_all=False)

    Batch — several replacements applied in order, each seeing the previous
    one's result::

        patch(file_path, edits=[{old_string, new_string, replace_all}, ...])

    Both forms are ONE code path: same read-before-edit guard, same match rules
    (must exist, must be unique unless replace_all), one snapshot, one write.
    An edit whose old_string equals its new_string once line endings match the
    file's changes nothing: it is left out and the result names it, and a call
    whose every edit is one writes nothing and says so. Batch validation is
    all-or-nothing: every other edit is validated and applied in memory first,
    so a miss or an ambiguity anywhere in the list writes nothing and names the
    offending edit. The final filesystem write is not crash-atomic. Mixing the
    two forms is an error, never a silent precedence rule.
    """
    assert context is not None
    raw_path = file_path or path
    if not raw_path:
        return "ERROR: path is required"

    old = old_string if old_string is not None else search
    new = new_string if new_string is not None else content
    batch = edits is not None

    if batch and (old is not None or new is not None or replace_all):
        return (
            "ERROR: pass either the scalar form (old_string/new_string/replace_all) "
            "or edits, not both"
        )

    if not batch:
        if old is None or new is None:
            return "ERROR: old_string and new_string are required"
        single = _normalize_edit({"old_string": old, "new_string": new, "replace_all": replace_all}, "")
        if isinstance(single, str):
            return single
        pending = [single]
    else:
        if not isinstance(edits, list) or not edits:
            return "ERROR: edits must be a non-empty list of {old_string, new_string} objects"
        pending = []
        for index, raw in enumerate(edits, start=1):
            normalized = _normalize_edit(raw, f"edit {index}: ")
            if isinstance(normalized, str):
                return normalized
            pending.append(normalized)

    target = context.resolve_path(raw_path, write=True)
    # Reject a non-regular target by type before any read: opening a FIFO with
    # no writer parks in open() forever, so read_bytes() never returns.
    if not target.exists():
        return no_such_file(context, target)
    if not target.is_file():
        return f"ERROR: not a regular file: {target}"
    try:
        source_bytes = _read_regular_bytes(target)
        source = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"ERROR: {exc}"
    source_hash = hash_bytes(source_bytes)
    # The read guard is decided per edit: an exact match of the current text
    # needs no read, and a stale read only matters for an edit that misses or
    # is too short to be its own proof.
    stale = changed_since_read(
        context, target, "edit it", source_bytes, source_hash, edits_lines=True
    )

    updated = source
    coverage = context.reads.coverage(target)
    seen_ranges = coverage.ranges
    was_whole = coverage.whole
    if was_whole:
        total_lines = max(1, len(source.splitlines()))
        seen_ranges = [(1, total_lines)]
    replacements = 0
    fuzzy_edits = 0
    unchanged: list[int] = []
    for index, (old_text, new_text, edit_replace_all) in enumerate(pending, start=1):
        line_ending = detect_line_ending(updated)
        if normalize_line_endings(old_text, line_ending) == normalize_line_endings(new_text, line_ending):
            unchanged.append(index)
            continue
        applied = _apply_edit(
            updated,
            old_text,
            new_text,
            edit_replace_all,
            f"edit {index}: " if batch else "",
            target=target,
            context=context,
            seen_ranges=seen_ranges,
            content_hash=source_hash,
            stale=stale,
        )
        if isinstance(applied, str):
            return applied
        updated, count, seen_ranges, fuzzy = applied
        replacements += count if edit_replace_all else 1
        fuzzy_edits += fuzzy

    if len(unchanged) == len(pending):
        return f"{target} unchanged: every edit's old_string equals its new_string, so nothing was written (hash {source_hash})."

    try:
        data = updated.encode("utf-8")
    except UnicodeEncodeError as exc:
        return f"ERROR: {exc}"
    context.snapshot(target)
    target.write_bytes(data)
    content_hash = hash_bytes(data)
    context.reads.replace(
        target,
        content_hash,
        seen_ranges,
        len(updated.splitlines()),
        whole_file=was_whole,
    )
    context.reads.remember_content(target, content_hash, data)
    _view(path=str(target), before=source, after=updated, edits=len(pending), replacements=replacements)
    diff = unified_diff(source, updated, str(target), str(target))
    if len(diff) > 4000:
        diff = diff[:4000] + "\n... [diff truncated]"
    if batch:
        applied_edits = len(pending) - len(unchanged)
        summary = f"{applied_edits} edit{'s' if applied_edits != 1 else ''}"
    else:
        summary = f"{replacements} replacement{'s' if replacements != 1 else ''}"
    if fuzzy_edits:
        who = f"{fuzzy_edits} of them" if batch else "old_string"
        summary += f", {who} matched after normalising quotes, dashes and whitespace"
    if unchanged:
        named = f"edit{'s' if len(unchanged) != 1 else ''} {', '.join(map(str, unchanged))}"
        summary += f', {named} left out: old_string equals new_string'
    return f"patched {target} ({summary}, hash {content_hash})\n{diff}"
