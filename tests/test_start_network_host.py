"""Tests for the ``network_host`` rename of ``connect()``'s ``udp_host``
argument (deprecated alias retained), and the deprecated ``start()``.
``stop()`` lifecycle aliases."""

from unittest.mock import Mock

import pytest

import ruidadriver.ruida_driver as ruida_driver
from ruidadriver.rd_transport import _resolve_network_host
from ruidadriver.ruida_driver import RdDriver


def test_resolve_network_host_neither():
    assert _resolve_network_host(None, None) is None


def test_resolve_network_host_primary():
    assert _resolve_network_host("192.168.1.100", None) == "192.168.1.100"


def test_resolve_network_host_alias_warns():
    with pytest.warns(DeprecationWarning):
        assert _resolve_network_host(None, "192.168.1.100") == "192.168.1.100"


def test_resolve_network_host_both_raises():
    with pytest.raises(ValueError):
        _resolve_network_host("192.168.1.1", "192.168.1.2")


def _fake_driver(monkeypatch):
    fake_transport = Mock()
    fake_transport.configure = Mock()
    fake_transport.open = Mock(return_value=True)
    fake_session = Mock()
    fake_session.transport = fake_transport
    monkeypatch.setattr(ruida_driver, "RdSession", lambda: fake_session)
    driver = RdDriver()
    monkeypatch.setattr(driver, "_start_script_runner", lambda: None)
    return driver, fake_transport


def test_connect_forwards_network_host(monkeypatch):
    driver, fake_transport = _fake_driver(monkeypatch)
    assert driver.connect(network_host="192.168.1.100")
    assert fake_transport.open.call_args.kwargs["network_host"] == "192.168.1.100"


def test_connect_accepts_deprecated_udp_host(monkeypatch):
    driver, fake_transport = _fake_driver(monkeypatch)
    with pytest.warns(DeprecationWarning):
        assert driver.connect(udp_host="192.168.1.100")
    assert fake_transport.open.call_args.kwargs["network_host"] == "192.168.1.100"


def test_connect_rejects_both_host_args(monkeypatch):
    driver, _ = _fake_driver(monkeypatch)
    with pytest.raises(ValueError):
        driver.connect(network_host="192.168.1.1", udp_host="192.168.1.2")


def test_start_deprecated_alias_delegates_to_connect(monkeypatch):
    driver, fake_transport = _fake_driver(monkeypatch)
    with pytest.warns(DeprecationWarning):
        assert driver.start(network_host="192.168.1.100")
    assert fake_transport.open.call_args.kwargs["network_host"] == "192.168.1.100"


def test_stop_deprecated_alias_delegates_to_disconnect(monkeypatch):
    driver, _ = _fake_driver(monkeypatch)
    driver.connect(network_host="192.168.1.100")
    session = driver._session
    with pytest.warns(DeprecationWarning):
        driver.stop()
    session.disconnect.assert_called_once()
    assert driver._session is None


def test_disconnect_tears_down_session(monkeypatch):
    driver, _ = _fake_driver(monkeypatch)
    driver.connect(network_host="192.168.1.100")
    session = driver._session
    driver.disconnect()
    session.disconnect.assert_called_once()
    assert driver._session is None


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
