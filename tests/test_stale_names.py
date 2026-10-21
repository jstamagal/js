import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_js_module_name_is_used_that_does_not_exist():
    spec = importlib.util.spec_from_file_location("stale_names", ROOT / "scripts" / "stale_names.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.stale_names(ROOT) == []
