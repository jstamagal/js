from types import SimpleNamespace

from js import paths, turn_settings
from js.toolkit.core import ToolContext
from js.toolkit.toolbox import toolbox


def test_registered_setting_can_disable_example_seeding(tmp_path):
    context = ToolContext(cwd=tmp_path)
    turn_settings.install(context, SimpleNamespace(**turn_settings.project({"PYTHON_KERNEL": True, "TOOLBOX_SEED_EXAMPLES": False})))
    toolbox(action="list", context=context)
    assert not paths.global_toolbox_dir().exists()
