"""Every module under js/ imports first, in a fresh interpreter.

An import cycle only shows when the modules in it load in a particular order;
js.cli's own order can hide one that `import js.headless` alone trips. A
module whose import fails only for a missing optional package (an extra that
is not installed) is skipped with that package named.

The packages outside js load once in a parent interpreter, and each module
imports in a child forked from it, before any js module is loaded there.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Imports every module it is given and names the packages outside js that
# they loaded.
LISTER = """
import importlib, sys
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except BaseException:
        pass
print("\\n".join(sorted(name for name in sys.modules
                         if name != "__main__" and name != "js" and not name.startswith("js."))))
"""

# Loads the packages outside js once, then imports each js module in a child
# forked from that js-free interpreter, so every module is still the first js
# module its interpreter loads. Prints one JSON line per module.
SERVER = """
import importlib, json, os, sys, traceback
preload, out_dir, modules = sys.argv[1], sys.argv[2], sys.argv[3:]
for name in open(preload).read().split():
    try:
        importlib.import_module(name)
    except BaseException:
        pass
for name in [name for name in sys.modules if name == "js" or name.startswith("js.")]:
    del sys.modules[name]


def child(module, path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    code = 0
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name and not exc.name.startswith("js"):
            print(exc.name)
            code = 3
        else:
            traceback.print_exc()
            code = 1
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


running = {}
pending = list(enumerate(modules))
limit = min(4, os.cpu_count() or 1)
while pending or running:
    while pending and len(running) < limit:
        index, module = pending.pop(0)
        path = os.path.join(out_dir, str(index))
        pid = os.fork()
        if pid == 0:
            child(module, path)
        running[pid] = (module, path)
    pid, status = os.wait()
    module, path = running.pop(pid)
    code = os.waitstatus_to_exitcode(status)
    print(json.dumps([module, code, open(path, errors="replace").read().strip()]), flush=True)
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


def test_every_module_imports_first_in_a_fresh_interpreter(tmp_path):
    modules = _modules()
    listed = subprocess.run([sys.executable, "-I", "-c", LISTER, *modules], cwd=ROOT,
                            capture_output=True, text=True, timeout=300, check=True)
    preload = tmp_path / "preload"
    preload.write_text(listed.stdout)
    done = subprocess.run([sys.executable, "-I", "-c", SERVER, str(preload), str(tmp_path), *modules],
                          cwd=ROOT, capture_output=True, text=True, timeout=600)
    results = [json.loads(line) for line in done.stdout.splitlines()]
    assert sorted(module for module, _, _ in results) == sorted(modules), done.stderr
    failures = [f"{module}: {text.splitlines()[-1] if text else code}"
                for module, code, text in results if code not in (0, 3)]
    assert not failures, "\n".join(failures)
    missing = sorted({text.splitlines()[-1] for _, code, text in results if code == 3})
    if missing:
        pytest.skip(f"modules skipped for optional packages not installed: {', '.join(missing)}")
