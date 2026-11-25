"""The console's session usage figures."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .usage import UsageTotals


# --- display -----------------------------------------------------------------------

def format_dollars(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def status_text(live: UsageTotals) -> str | None:
    """The status bar's session figure: the dollar total when any call was
    priced (with `+` when some were not), else the token total, else None
    before the first call."""
    if live.calls == 0:
        return None
    if live.priced:
        return format_dollars(live.cost) + ("+" if live.unpriced_calls else "")
    return f"{format_tokens(live.input_tokens + live.output_tokens)} tok"
