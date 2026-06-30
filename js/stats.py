"""Turn-stat aggregation + JSON/CSV emit for --bench and --stats-json/--stats-csv.

A "turn" is one user prompt run to completion; it makes one or more model calls
(extra calls when the model uses tools). run_turn fills a per-call list of dicts
(see its `call_stats` param); `summarize_calls` folds those into one row.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

# CSV column order; JSON rows carry the same keys (plus any caller extras).
ROW_FIELDS = (
    "name",
    "prompt",
    "max_tokens",
    "ok",
    "calls",
    "ttft_s",
    "wall_s",
    "stream_s",
    "output_tokens",
    "prompt_tokens",
    "cached_tokens",
    "tok_per_s",
    "finish_reason",
    "error",
)


def _round(value: float | None, places: int = 4) -> float | None:
    return round(value, places) if isinstance(value, (int, float)) else None


def summarize_calls(call_stats: list[dict], *, wall_s: float | None = None) -> dict[str, Any]:
    """Fold a turn's per-call records into one summary row.

    ttft is the first call's time-to-first-text-token (what the user waits to see
    output). stream_s/output_tokens sum across calls; tok_per_s is total output
    over total model-stream time (generation throughput, tool gaps excluded).
    """
    stream_s = sum((c.get("stream_s") or 0.0) for c in call_stats)
    output_tokens = sum(int(c.get("output_tokens") or 0) for c in call_stats)
    ttft = call_stats[0].get("ttft_s") if call_stats else None
    return {
        "calls": len(call_stats),
        "ttft_s": _round(ttft),
        "wall_s": _round(wall_s),
        "stream_s": _round(stream_s),
        "output_tokens": output_tokens,
        "prompt_tokens": int(call_stats[0].get("prompt_tokens") or 0) if call_stats else 0,
        "cached_tokens": int(call_stats[-1].get("cached_tokens") or 0) if call_stats else 0,
        "tok_per_s": _round(output_tokens / stream_s) if stream_s > 0 else 0.0,
        "finish_reason": call_stats[-1].get("finish_reason") if call_stats else None,
    }


def write_json(path: str | Path, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_csv(path: str | Path, rows: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(ROW_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in ROW_FIELDS})
