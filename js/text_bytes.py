"""UTF-8 accounting for model-facing text budgets.

A lone surrogate (from `json.loads('"\\ud800"')` or a surrogateescape read)
has no UTF-8 form; it counts and cuts as the `?` that `errors="replace"`
writes, so a budget check never raises."""


def _utf8(text: str) -> bytes:
    return text.encode("utf-8", errors="replace")


def byte_size(text: str) -> int:
    return len(_utf8(text))


def well_formed(text: str) -> str:
    """``text`` with each lone surrogate replaced by `?`, so any later strict
    UTF-8 encode (a session write, a provider SDK) cannot raise."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return _utf8(text).decode("utf-8")
    return text


def byte_prefix(text: str, budget: int) -> str:
    return _utf8(text)[:max(0, budget)].decode("utf-8", errors="ignore")


def cap_text(text: str, budget: int, marker: str) -> str:
    """Hard cap including disclosure; use a compact marker for tiny budgets."""
    if byte_size(text) <= budget:
        return text
    if byte_size(marker) > budget:
        marker = "[truncated]" if budget >= 11 else "~"
    return byte_prefix(text, budget - byte_size(marker)) + byte_prefix(marker, budget)
