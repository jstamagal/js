"""Every module under js/ imports first, in a fresh interpreter.

An import cycle only shows when the modules in it load in a particular order;
js.cli's own order can hide one that `import js.headless` alone trips. A
module whose import fails only for a missing optional package (an extra that
is not installed) is skipped with that package named.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = """
import importlib, sys
try:
    importlib.import_module(sys.argv[1])
except ModuleNotFoundError as exc:
    if exc.name and not exc.name.startswith("js"):
        print(exc.name)
        sys.exit(3)
    raise
"""


def _modules() -> list[str]:
    out = []
    for path in sorted((ROOT / "js").rglob("*.py")):
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts[-1] == "__main__":
            continue  # runs the CLI when imported
        out.append(".".join(parts))
    return out


def _import(module: str) -> tuple[str, int, str]:
    done = subprocess.run([sys.executable, "-I", "-c", PROBE, module], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    return module, done.returncode, (done.stdout + done.stderr).strip()


def test_every_module_imports_first_in_a_fresh_interpreter():
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_import, _modules()))
    failures = [f"{module}: {text.splitlines()[-1] if text else code}"
                for module, code, text in results if code not in (0, 3)]
    assert not failures, "\n".join(failures)
    missing = sorted({text.splitlines()[-1] for _, code, text in results if code == 3})
    if missing:
        pytest.skip(f"modules skipped for optional packages not installed: {', '.join(missing)}")
