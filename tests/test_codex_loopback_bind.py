from __future__ import annotations

import errno
import socket
from unittest.mock import Mock

import pytest

from js import codex_auth


@pytest.mark.parametrize("family, address", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_occupied_callback_family_fails_and_releases_other_listener(monkeypatch, family, address):
    busy = socket.socket(family)
    try:
        try:
            busy.bind((address, 0))
        except OSError as exc:
            pytest.skip(f"loopback family unavailable: {exc}")
        busy.listen()
        port = busy.getsockname()[1]
        monkeypatch.setattr(codex_auth, "CALLBACK_PORT", port)
        servers = []
        try:
            with pytest.raises(OSError) as caught:
                servers = codex_auth._bind_callback_servers(codex_auth._CallbackHandler)
            assert caught.value.errno == errno.EADDRINUSE
            if family == socket.AF_INET6:
                with socket.socket(socket.AF_INET) as probe:
                    probe.bind(("127.0.0.1", port))
        finally:
            for server in servers:
                server.server_close()
    finally:
        busy.close()


@pytest.mark.parametrize("unavailable_family", ["v4", "v6", "both"])
def test_unavailable_callback_family_still_falls_back(monkeypatch, unavailable_family):
    v4 = Mock()
    v6 = Mock()
    create_v4 = Mock(return_value=v4)
    create_v6 = Mock(return_value=v6)
    if unavailable_family in {"v4", "both"}:
        create_v4.side_effect = OSError(errno.EAFNOSUPPORT, "fixture unsupported family")
    if unavailable_family in {"v6", "both"}:
        create_v6.side_effect = OSError(errno.EADDRNOTAVAIL, "fixture unavailable address")
    monkeypatch.setattr(codex_auth, "_CallbackServer", create_v4)
    monkeypatch.setattr(codex_auth, "_CallbackServerV6", create_v6)

    servers = codex_auth._bind_callback_servers(codex_auth._CallbackHandler)

    assert servers == {"v4": [v6], "v6": [v4], "both": []}[unavailable_family]
    v4.server_close.assert_not_called()
    v6.server_close.assert_not_called()


def test_later_address_collision_closes_previously_bound_server(monkeypatch):
    v4 = Mock()
    monkeypatch.setattr(codex_auth, "_CallbackServer", Mock(return_value=v4))
    error = OSError(errno.EADDRINUSE, "fixture occupied IPv6")
    monkeypatch.setattr(codex_auth, "_CallbackServerV6", Mock(side_effect=error))

    with pytest.raises(OSError) as caught:
        codex_auth._bind_callback_servers(codex_auth._CallbackHandler)

    assert caught.value is error
    v4.server_close.assert_called_once_with()
