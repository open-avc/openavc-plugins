"""Each plugin that holds a login says so, so OpenAVC keeps it on the system.

A setting marked ``secret: True`` in CONFIG_SCHEMA, or a list declared in
CONFIG_SECRETS, is kept in the system's login file instead of the project, and
the plugin is handed its saved value in ``api.config``. A login left unmarked
stays in the project, in every copy of it: a backup, an export, a template.
These pin the declarations themselves; what OpenAVC does with them is tested
on the platform.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _literal(path, name):
    """A class attribute's literal value, following ``NAME = NAME`` to the
    module-level dict it names. Read from the source, so no plugin
    dependency has to be installed."""
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    module = {
        n.targets[0].id: n.value for n in tree.body
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name for t in item.targets
                ):
                    value = item.value
                    if isinstance(value, ast.Name):
                        value = module[value.id]
                    return ast.literal_eval(value)
    raise AssertionError(f"{path} has no {name}")


def test_the_mqtt_bridge_password_is_a_login():
    schema = _literal("integrations/mqtt/mqtt_bridge_plugin.py", "CONFIG_SCHEMA")
    assert schema["password"]["secret"] is True
    assert "secret" not in schema["username"]


def test_the_dante_api_key_is_a_login():
    schema = _literal("integrations/dante/dante_plugin.py", "CONFIG_SCHEMA")
    assert schema["connection"]["fields"]["api_key"]["secret"] is True


def test_present_display_and_stream_keys_are_logins_it_makes_itself():
    assert _literal("integrations/present/present_plugin.py", "CONFIG_SECRETS") == {
        "displays": {"id": "id", "fields": ["display_key", "stream_key"], "generated": True},
    }


def test_the_video_panel_stream_password_is_a_login():
    assert _literal("integrations/video_panel/video_panel_plugin.py", "CONFIG_SECRETS") == {
        "streams": {"id": "stream_id", "fields": ["password"]},
    }
