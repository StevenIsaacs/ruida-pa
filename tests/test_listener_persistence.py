"""Tests for listener lifecycle: register before connect and active after disconnect.

Covers:
- RdDriver.register_status_listener replays DISCONNECTED when not connected.
- RdDriver.disconnect() emits a terminal DISCONNECTED to status listeners.
- RdDriver listeners persist across disconnect -> reconnect cycles.
- TuiAdapter buffers register_* calls while no driver exists and wires them
  onto the next driver; unregister removes from the buffer.
"""

from unittest.mock import Mock

import ruidadriver.ruida_driver as ruida_driver
from ruidadriver.rd_status import RdStatusEvent
from ruidadriver.ruida_driver import RdDriver
from rpascript.tui_adapter import TuiAdapter


def _fake_session(monkeypatch):
    """Monkeypatch RdSession with fakes that support the real runner lifecycle."""
    status = Mock()
    status.start = Mock()
    status.stop = Mock()
    status.is_connected = False
    transport = Mock()
    transport.configure = Mock()
    transport.open = Mock(return_value=True)
    transport.is_open = True
    session = Mock()
    session.transport = transport
    session.status = status
    session.is_connected = False
    session.is_usb = False
    session.disconnect = Mock()
    session.shutdown = Mock()
    monkeypatch.setattr(ruida_driver, "RdSession", lambda: session)
    return session


# ------------------------------------------------------------------ #
#  RdDriver — replay and terminal events
# ------------------------------------------------------------------ #

def test_register_before_connect_replays_disconnected():
    driver = RdDriver()
    events = []
    driver.register_status_listener(events.append)
    assert events == [RdStatusEvent.DISCONNECTED]


def test_register_after_disconnect_replays_disconnected(monkeypatch):
    _fake_session(monkeypatch)
    driver = RdDriver()
    driver._start_script_runner = lambda: None
    driver.connect(network_host="192.168.1.100")
    driver.disconnect()
    events = []
    driver.register_status_listener(events.append)
    assert events == [RdStatusEvent.DISCONNECTED]


def test_disconnect_emits_terminal_disconnected(monkeypatch):
    _fake_session(monkeypatch)
    driver = RdDriver()
    events = []
    driver.register_status_listener(events.append)
    events.clear()
    driver.connect(network_host="192.168.1.100")
    driver.disconnect()
    assert events == [RdStatusEvent.DISCONNECTED]


def test_disconnect_is_idempotent_for_terminal_event(monkeypatch):
    _fake_session(monkeypatch)
    driver = RdDriver()
    events = []
    driver.register_status_listener(events.append)
    events.clear()
    driver.connect(network_host="192.168.1.100")
    driver.disconnect()
    driver.disconnect()
    assert events == [RdStatusEvent.DISCONNECTED]


def test_listeners_persist_across_disconnect_and_reconnect(monkeypatch):
    _fake_session(monkeypatch)
    driver = RdDriver()
    events = []
    driver.register_status_listener(events.append)
    events.clear()
    driver.connect(network_host="192.168.1.100")
    driver.disconnect()
    assert events == [RdStatusEvent.DISCONNECTED]
    assert driver._status_listeners == [events.append]

    driver.connect(network_host="192.168.1.100")
    driver._on_status_event(RdStatusEvent.CONNECTED)
    assert events[-1] == RdStatusEvent.CONNECTED
    driver.disconnect()


def test_error_and_reply_listeners_persist_across_disconnect(monkeypatch):
    _fake_session(monkeypatch)
    driver = RdDriver()
    error_cb = Mock()
    reply_cb = Mock()
    driver.register_error_listener(error_cb)
    driver.register_reply_listener(reply_cb)
    driver.connect(network_host="192.168.1.100")
    driver.disconnect()
    assert error_cb in driver._error_listeners
    assert reply_cb in driver._reply_listeners


# ------------------------------------------------------------------ #
#  TuiAdapter — buffered registration while no driver exists
# ------------------------------------------------------------------ #

def test_tui_register_before_connect_buffers_status_listener():
    adapter = TuiAdapter()
    cb = Mock()
    adapter.register_status_listener(cb)
    assert adapter._ruida_driver is None
    assert cb in adapter._pending_status_listeners


def test_tui_registers_all_types_buffer_when_no_driver():
    adapter = TuiAdapter()
    status_cb = Mock()
    error_cb = Mock()
    reply_cb = Mock()
    adapter.register_status_listener(status_cb)
    adapter.register_error_listener(error_cb)
    adapter.register_reply_listener(reply_cb)
    assert status_cb in adapter._pending_status_listeners
    assert error_cb in adapter._pending_error_listeners
    assert reply_cb in adapter._pending_reply_listeners


def test_tui_unregister_removes_buffered_listener():
    adapter = TuiAdapter()
    cb = Mock()
    adapter.register_status_listener(cb)
    adapter.unregister_status_listener(cb)
    assert adapter._pending_status_listeners == []


def test_tui_buffered_listeners_wired_on_driver_creation():
    adapter = TuiAdapter()
    status_cb = Mock()
    error_cb = Mock()
    reply_cb = Mock()
    adapter.register_status_listener(status_cb)
    adapter.register_error_listener(error_cb)
    adapter.register_reply_listener(reply_cb)
    driver = adapter._create_driver()
    assert status_cb in driver._status_listeners
    assert error_cb in driver._error_listeners
    assert reply_cb in driver._reply_listeners
    assert adapter._pending_status_listeners == []
    assert adapter._pending_error_listeners == []
    assert adapter._pending_reply_listeners == []


def test_tui_register_after_disconnect_buffers():
    adapter = TuiAdapter()
    adapter._ruida_driver = None
    cb = Mock()
    adapter.register_status_listener(cb)
    assert cb in adapter._pending_status_listeners


# ------------------------------------------------------------------ #
#  TuiAdapter — listeners survive driver release/recreate
# ------------------------------------------------------------------ #

def test_tui_driver_replacement_preserves_status_listener():
    adapter = TuiAdapter()
    adapter._ruida_driver = adapter._create_driver()
    cb = Mock()
    adapter.register_status_listener(cb)
    assert cb in adapter._ruida_driver._status_listeners

    adapter._release_driver()
    assert adapter._ruida_driver is None
    assert cb in adapter._pending_status_listeners

    adapter._ruida_driver = adapter._create_driver()
    assert cb in adapter._ruida_driver._status_listeners
    assert adapter._pending_status_listeners == []


def test_tui_driver_replacement_preserves_error_listener():
    adapter = TuiAdapter()
    adapter._ruida_driver = adapter._create_driver()
    cb = Mock()
    adapter.register_error_listener(cb)
    assert cb in adapter._ruida_driver._error_listeners

    adapter._release_driver()
    assert cb in adapter._pending_error_listeners

    adapter._ruida_driver = adapter._create_driver()
    assert cb in adapter._ruida_driver._error_listeners
    assert adapter._pending_error_listeners == []


def test_tui_driver_replacement_preserves_reply_listener():
    adapter = TuiAdapter()
    adapter._ruida_driver = adapter._create_driver()
    cb = Mock()
    adapter.register_reply_listener(cb)
    assert cb in adapter._ruida_driver._reply_listeners

    adapter._release_driver()
    assert cb in adapter._pending_reply_listeners

    adapter._ruida_driver = adapter._create_driver()
    assert cb in adapter._ruida_driver._reply_listeners
    assert adapter._pending_reply_listeners == []


def test_tui_driver_replacement_does_not_duplicate_tui_listeners():
    adapter = TuiAdapter()
    adapter._ruida_driver = adapter._create_driver()
    status_cb = Mock()
    adapter.register_status_listener(status_cb)

    adapter._release_driver()
    adapter._ruida_driver = adapter._create_driver()

    tui_status = adapter._ruida_driver._status_listeners.count(
        adapter.on_status_event
    )
    assert tui_status == 1
    assert status_cb in adapter._ruida_driver._status_listeners


def test_tui_driver_replacement_listener_receives_disconnected_replay():
    adapter = TuiAdapter()
    adapter._ruida_driver = adapter._create_driver()
    events = []
    adapter.register_status_listener(events.append)
    events.clear()

    adapter._release_driver()
    adapter._ruida_driver = adapter._create_driver()

    assert events == [RdStatusEvent.DISCONNECTED]


def test_tui_unregister_during_active_driver_survives_recreate():
    adapter = TuiAdapter()
    adapter._ruida_driver = adapter._create_driver()
    cb = Mock()
    adapter.register_status_listener(cb)
    adapter.unregister_status_listener(cb)
    assert cb not in adapter._ruida_driver._status_listeners

    adapter._release_driver()
    adapter._ruida_driver = adapter._create_driver()
    assert cb not in adapter._ruida_driver._status_listeners


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
