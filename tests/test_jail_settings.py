from js import jail, settings


def test_registered_jail_settings_can_remove_implicit_hidden_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(jail, "_mounts", lambda: [])
    monkeypatch.setattr(jail, "_is_dir", lambda _path: True)
    monkeypatch.setattr(jail, "operator_homes", lambda: [tmp_path / "home"])
    settings.bind_live_settings({
        "JAIL_BASE_HIDDEN_DIRS": [], "JAIL_HIDE_OPERATOR_HOME": False,
        "JAIL_HIDE_NETWORK_FILESYSTEMS": False,
    })
    assert jail.hidden_roots() == []
