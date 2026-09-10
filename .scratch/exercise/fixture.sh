#!/usr/bin/env bash
# Build a real, self-contained project for the tool-exercise run.
# Every task in tasks.tsv acts on something created here.
set -euo pipefail

ROOT="${1:?usage: fixture.sh /path/to/workdir}"
rm -rf "$ROOT" 2>/dev/null || true
mkdir -p "$ROOT/src/shapes" "$ROOT/tests" "$ROOT/notes" "$ROOT/data"
cd "$ROOT"

# ---- a real python package with a real bug ---------------------------------
cat > src/shapes/__init__.py <<'PY'
from .area import circle_area, rect_area, total_area
__all__ = ["circle_area", "rect_area", "total_area"]
PY

cat > src/shapes/area.py <<'PY'
"""Area helpers. One of these is wrong on purpose."""

import math

PRECISION = 4


def circle_area(radius: float) -> float:
    """Area of a circle."""
    if radius < 0:
        raise ValueError("radius must be non-negative")
    # BUG: uses diameter, not radius
    return round(math.pi * (radius * 2) ** 2, PRECISION)


def rect_area(width: float, height: float) -> float:
    """Area of a rectangle."""
    if width < 0 or height < 0:
        raise ValueError("sides must be non-negative")
    return round(width * height, PRECISION)


def total_area(shapes: list[dict]) -> float:
    """Sum the areas of a list of shape dicts."""
    total = 0.0
    for shape in shapes:
        kind = shape.get("kind")
        if kind == "circle":
            total += circle_area(shape["radius"])
        elif kind == "rect":
            total += rect_area(shape["width"], shape["height"])
        else:
            raise ValueError(f"unknown shape: {kind}")
    return round(total, PRECISION)
PY

cat > tests/test_area.py <<'PY'
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shapes import circle_area, rect_area, total_area


def test_circle_area():
    assert circle_area(1) == round(math.pi, 4)
    assert circle_area(2) == round(math.pi * 4, 4)


def test_rect_area():
    assert rect_area(3, 4) == 12


def test_total_area():
    shapes = [{"kind": "rect", "width": 2, "height": 5}]
    assert total_area(shapes) == 10
PY

# ---- a file that exists only to be deleted and restored ---------------------
cat > data/scratch-delete-me.txt <<'TXT'
This file exists so a delete can be undone.
Line two.
Line three.
TXT

# ---- searchable content, distinct tokens per tool --------------------------
cat > notes/inventory.md <<'MD'
# Inventory

| sku      | name            | qty |
|----------|-----------------|-----|
| AX-1180  | brass fitting   | 12  |
| AX-1181  | copper elbow    |  4  |
| ZZ-9000  | ORPHANED_TOKEN  |  0  |
| BX-2040  | steel bracket   | 31  |
MD

cat > notes/README.md <<'MD'
# Scratch project

A tiny package with an area bug, used to exercise a tool surface.
The token ORPHANED_TOKEN appears in exactly one other file.
MD

cat > data/measurements.csv <<'CSV'
sample,length_mm,width_mm
a,120.5,44.2
b,98.1,51.7
c,143.9,38.0
d,101.2,47.5
e,87.6,55.1
CSV

# ---- real git history so history-aware tools have something to chew on -----
git init -q
git -c user.email=fixture@local -c user.name=Fixture add -A
git -c user.email=fixture@local -c user.name=Fixture commit -qm "Initial scratch project"

echo "$ROOT"
