"""Tests for TRANSPORT_* event emission from RdStatus.

Covers:
- RdStatus fires TRANSPORT_UDP / TRANSPORT_TCP / TRANSPORT_USB when the
  transport layer emits TransportEvent.OPENED.
- Warning events (MALFORMED_REPLY, REPLY_ERROR, UNEXPECTED_REPLY) are
  still re-surfaced alongside transport events.
- Transport events fire on every OPENED, not just the first.
"""

from unittest.mock import Mock, PropertyMock

from ruidadriver.rd_status import RdStatus, RdStatusEvent
from ruidadriver.rd_transport import RdTransport
from ruidadriver.transport_events import TransportEvent


def _make_status(monkeypatch, *, is_usb=False, is_tcp=False, is_udp=True):
    """Create an RdStatus with a mock transport of the given type."""
    transport = Mock(spec=RdTransport)
    type(transport).is_usb = PropertyMock(return_value=is_usb)
    type(transport).is_tcp = PropertyMock(return_value=is_tcp)
    type(transport).is_udp = PropertyMock(return_value=is_udp)
    transport.register_status_listener = Mock()
    transport.unregister_status_listener = Mock()
    return RdStatus(transport), transport


def _fire_open(status, transport):
    """Simulate the transport firing OPENED to its listeners.

    RdStatus registers its _transport_listener with the transport during
    start(); in these tests we invoke it directly.
    """
    status._transport_listener(TransportEvent.OPENED)


def test_transport_udp_emitted_on_open(monkeypatch):
    status, transport = _make_status(monkeypatch, is_udp=True)
    events = []
    status.register_status_listener(events.append)
    _fire_open(status, transport)
    assert RdStatusEvent.TRANSPORT_UDP in events


def test_transport_tcp_emitted_on_open(monkeypatch):
    status, transport = _make_status(monkeypatch, is_tcp=True)
    events = []
    status.register_status_listener(events.append)
    _fire_open(status, transport)
    assert RdStatusEvent.TRANSPORT_TCP in events


def test_transport_usb_emitted_on_open(monkeypatch):
    status, transport = _make_status(monkeypatch, is_usb=True)
    events = []
    status.register_status_listener(events.append)
    _fire_open(status, transport)
    assert RdStatusEvent.TRANSPORT_USB in events


def test_transport_event_fires_on_reconnect(monkeypatch):
    status, transport = _make_status(monkeypatch, is_udp=True)
    events = []
    status.register_status_listener(events.append)
    _fire_open(status, transport)
    _fire_open(status, transport)
    assert events.count(RdStatusEvent.TRANSPORT_UDP) == 2


def test_warning_events_still_emitted(monkeypatch):
    status, transport = _make_status(monkeypatch, is_udp=True)
    events = []
    status.register_status_listener(events.append)
    status._transport_listener(TransportEvent.MALFORMED_REPLY)
    assert RdStatusEvent.TRANSPORT_MALFORMED_REPLY in events


def _make_status_with_query(monkeypatch):
    status, transport = _make_status(monkeypatch, is_udp=True)
    status._query_cmds = [bytearray(b"\x00")]
    return status, transport


def test_reply_pending_busy_emits_busy_and_retries(monkeypatch):
    status, transport = _make_status_with_query(monkeypatch)
    events = []
    status.register_status_listener(events.append)
    status._connected = True
    status._transport_listener(TransportEvent.BUSY)

    state, retries = status._run_reply_pending(3)

    assert state == "SEND_QUERY"
    assert retries == 3
    assert RdStatusEvent.BUSY in events
    assert RdStatusEvent.DISCONNECTED not in events
    assert status._connected is True


def test_reply_pending_busy_emitted_once_per_episode(monkeypatch):
    status, transport = _make_status_with_query(monkeypatch)
    events = []
    status.register_status_listener(events.append)

    status._transport_listener(TransportEvent.BUSY)
    status._run_reply_pending(3)
    status._transport_listener(TransportEvent.BUSY)
    status._run_reply_pending(3)

    assert events.count(RdStatusEvent.BUSY) == 1


def test_reply_pending_busy_cleared_on_reply(monkeypatch):
    status, transport = _make_status_with_query(monkeypatch)
    events = []
    status.register_status_listener(events.append)

    status._transport_listener(TransportEvent.BUSY)
    status._run_reply_pending(3)
    assert status._busy is True

    status._transport_listener(TransportEvent.REPLY_FORWARDED)
    state, retries = status._run_reply_pending(3)

    assert state == "WAIT_TO_POLL"
    assert status._busy is False
    assert RdStatusEvent.QUERY_RECEIVED in events


def test_reply_pending_timeout_exhaustion_still_disconnects(monkeypatch):
    status, transport = _make_status_with_query(monkeypatch)
    events = []
    status.register_status_listener(events.append)
    status._connected = True
    status.QUERY_RETRY_DELAY = 0

    state, retries = status._run_reply_pending(1)

    assert state == "CONNECTING"
    assert retries == 0
    assert RdStatusEvent.DISCONNECTED in events
    assert status._connected is False


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
