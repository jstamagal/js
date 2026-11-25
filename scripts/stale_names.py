"""Uses of a js module's names that do not exist on it: `display.LIVE_LINES`
after LIVE_LINES became a setting. A local variable that shadows a module
name is not counted. Run: uv run python scripts/stale_names.py"""
import ast
import importlib
import importlib.util
import pathlib
import sys


def _bound(fn) -> set[str]:
    names = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    names |= {x.arg for x in (fn.args.vararg, fn.args.kwarg) if x}
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
    return names


def stale_names(root: pathlib.Path) -> list[str]:
    """`path:line: alias.NAME (module)` for each such use under ``root``/js."""
    bad: set[str] = set()
    for path in sorted((root / "js").rglob("*.py")):
        rel = path.relative_to(root).with_suffix("")
        package = ".".join(rel.parts[:-1])
        tree = ast.parse(path.read_text())
        modules = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = (importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                        if node.level else (node.module or ""))
                for alias in node.names:
                    target = f"{base}.{alias.name}"
                    if target.startswith("js"):
                        try:
                            modules[alias.asname or alias.name] = importlib.import_module(target)
                        except ImportError:
                            pass

        def visit(node, shadow):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                shadow = shadow | _bound(node)
            elif isinstance(node, ast.Lambda):
                shadow = shadow | {a.arg for a in node.args.args}
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and isinstance(node.ctx, ast.Load)):
                name = node.value.id
                if name in modules and name not in shadow and not hasattr(modules[name], node.attr):
                    bad.add(f"{path.relative_to(root)}:{node.lineno}: {name}.{node.attr} ({modules[name].__name__})")
            for child in ast.iter_child_nodes(node):
                visit(child, shadow)

        visit(tree, set())
    return sorted(bad)


if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    found = stale_names(root)
    print("\n".join(found) or "none")
    sys.exit(1 if found else 0)
