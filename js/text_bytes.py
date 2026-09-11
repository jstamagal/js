"""UTF-8 accounting for model-facing text budgets."""


def byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def byte_prefix(text: str, budget: int) -> str:
    return text.encode("utf-8")[:max(0, budget)].decode("utf-8", errors="ignore")


def cap_text(text: str, budget: int, marker: str) -> str:
    """Hard cap including disclosure; use a compact marker for tiny budgets."""
    if byte_size(text) <= budget:
        return text
    if byte_size(marker) > budget:
        marker = "[truncated]" if budget >= 11 else "~"
    return byte_prefix(text, budget - byte_size(marker)) + byte_prefix(marker, budget)