"""Tests for TcpTransport (raw byte stream) and RdTransport TCP framing.

A localhost socket stands in for the controller. Byte sequences mirror
traffic captured from an RDC8445S on TCP port 50200 (magic 0x88).

TcpTransport is a raw byte-stream transport: it returns raw interface bytes
and does no protocol framing. Stream reassembly into ACK/reply units is owned
by RdTransport (L4), exercised through the reply listener below.
"""

import socket
import threading
import time
from unittest.mock import Mock

import pytest

from protocols.ruida.ruida_protocol import ACK
from rpalib.rpa_swizzler import RpaSwizzler
from ruidadriver.rd_status import RdStatus
from ruidadriver.rd_transport import RdTransport
from ruidadriver.transport import TcpTransport
from ruidadriver.transport_events import TransportEvent

MAGIC = 0x88
CARD_ID_QUERY = bytes([0xDA, 0x00, 0x05, 0x7E])
CARD_ID_REPLY = bytes([0xDA, 0x01, 0x05, 0x7E, 0x09, 0x00, 0x42, 0x20, 0x10])
X_POS_REPLY = bytes([0xDA, 0x01, 0x04, 0x21, 0x00, 0x00, 0x00, 0x4E, 0x0F])
# A variable-length reply (> 9 bytes) with 7-bit value bytes, as produced for
# C-string values (e.g. MEM_MAINBOARD_VERSION).
VAR_REPLY = bytes(
    [0xDA, 0x01, 0x05, 0x7E, 0x09, 0x00, 0x42, 0x20, 0x10, 0x11, 0x12, 0x13]
)


def swz(data: bytes) -> bytes:
    return bytes(RpaSwizzler.swizzle_byte(b, MAGIC) for b in data)


@pytest.fixture
def server():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    yield listener
    listener.close()


def connect(server):
    transport = TcpTransport()
    port = server.getsockname()[1]
    assert transport.open("127.0.0.1", port)
    peer, _ = server.accept()
    return transport, peer


def read_until(transport, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = transport.read(65536)
        if data:
            return data
        time.sleep(0.005)
    return None


def _open_rd_tcp(server, monkeypatch, timeout=1000):
    """Return an RdTransport opened over the localhost server's TCP port."""
    port = server.getsockname()[1]
    original_open = TcpTransport.open
    monkeypatch.setattr(
        TcpTransport,
        "open",
        lambda self, host, _port=50200, **kw: original_open(self, host, port),
    )
    rd = RdTransport()
    rd.configure(magic=MAGIC, timeout=timeout)
    return rd


def _handshake(server, monkeypatch, controller_payload, timeout=1000):
    """Run a CARD_ID query against a controller sending ``payload``.

    Returns (received, replies): the bytes the controller read from us, and
    the list of reply lists the RdTransport reply listener received.
    """
    rd = _open_rd_tcp(server, monkeypatch, timeout=timeout)
    replies = []
    rd.register_reply_listener(replies.append)
    received = []

    def controller():
        peer, _ = server.accept()
        peer.settimeout(2.0)
        received.append(peer.recv(64))
        peer.sendall(controller_payload)
        time.sleep(0.5)
        peer.close()

    thread = threading.Thread(target=controller, daemon=True)
    thread.start()
    assert rd.open(network_host="127.0.0.1", protocol="tcp")
    rd.write([bytearray(CARD_ID_QUERY)])

    deadline = time.monotonic() + 3.0
    while not replies and time.monotonic() < deadline:
        time.sleep(0.01)
    rd.close()
    thread.join(timeout=2.0)
    return received, replies


def test_tcp_read_returns_raw_bytes(server):
    transport, peer = connect(server)
    peer.sendall(swz(bytes([ACK]) + CARD_ID_REPLY))
    data = read_until(transport)
    assert data == swz(bytes([ACK]) + CARD_ID_REPLY)
    transport.close()
    peer.close()


def test_peer_close_raises_and_marks_closed(server):
    transport, peer = connect(server)
    peer.close()
    with pytest.raises(OSError):
        read_until(transport)
    assert not transport.is_open


def test_write_sends_raw_swizzled_bytes(server):
    transport, peer = connect(server)
    transport.write(bytearray(swz(CARD_ID_QUERY)))
    peer.settimeout(2.0)
    assert peer.recv(64) == swz(CARD_ID_QUERY)
    transport.close()
    peer.close()


def test_ack_and_reply_in_one_segment_are_split(server, monkeypatch):
    received, replies = _handshake(
        server, monkeypatch, swz(bytes([ACK]) + CARD_ID_REPLY)
    )
    assert received == [swz(CARD_ID_QUERY)]
    assert replies and bytes(replies[0][0]) == CARD_ID_REPLY


def test_consecutive_replies_are_returned_together(server, monkeypatch):
    received, replies = _handshake(
        server, monkeypatch, swz(bytes([ACK]) + CARD_ID_REPLY + X_POS_REPLY)
    )
    assert received == [swz(CARD_ID_QUERY)]
    assert replies and len(replies[0]) == 2
    assert bytes(replies[0][0]) == CARD_ID_REPLY
    assert bytes(replies[0][1]) == X_POS_REPLY


def test_variable_length_reply_is_forwarded_intact(server, monkeypatch):
    received, replies = _handshake(
        server, monkeypatch, swz(bytes([ACK]) + VAR_REPLY)
    )
    assert received == [swz(CARD_ID_QUERY)]
    assert replies and bytes(replies[0][0]) == VAR_REPLY


def test_partial_reply_is_reassembled_at_l4(server, monkeypatch):
    rd = _open_rd_tcp(server, monkeypatch, timeout=1000)
    replies = []
    rd.register_reply_listener(replies.append)
    received = []

    def controller():
        peer, _ = server.accept()
        peer.settimeout(2.0)
        received.append(peer.recv(64))
        peer.sendall(swz(bytes([ACK]) + CARD_ID_REPLY[:4]))
        time.sleep(0.1)
        peer.sendall(swz(CARD_ID_REPLY[4:]))
        time.sleep(0.5)
        peer.close()

    thread = threading.Thread(target=controller, daemon=True)
    thread.start()
    assert rd.open(network_host="127.0.0.1", protocol="tcp")
    rd.write([bytearray(CARD_ID_QUERY)])

    deadline = time.monotonic() + 3.0
    while not replies and time.monotonic() < deadline:
        time.sleep(0.01)
    rd.close()
    thread.join(timeout=2.0)

    assert received == [swz(CARD_ID_QUERY)]
    assert replies and bytes(replies[0][0]) == CARD_ID_REPLY


def test_rd_transport_tcp_handshake_without_checksum(server, monkeypatch):
    port = server.getsockname()[1]
    received = []

    def controller():
        peer, _ = server.accept()
        peer.settimeout(2.0)
        received.append(peer.recv(64))
        peer.sendall(swz(bytes([ACK]) + CARD_ID_REPLY))
        time.sleep(0.5)
        peer.close()

    thread = threading.Thread(target=controller, daemon=True)
    thread.start()

    original_open = TcpTransport.open
    monkeypatch.setattr(
        TcpTransport,
        "open",
        lambda self, host, _port=50200, **kw: original_open(self, host, port),
    )

    rd = RdTransport()
    rd.configure(magic=MAGIC, timeout=1000)
    replies = []
    rd.register_reply_listener(replies.append)
    assert rd.open(network_host="127.0.0.1", protocol="tcp")
    assert rd.is_tcp and not rd.is_udp
    rd.write([bytearray(CARD_ID_QUERY)])

    deadline = time.monotonic() + 3.0
    while not replies and time.monotonic() < deadline:
        time.sleep(0.01)
    rd.close()
    thread.join(timeout=2.0)

    assert received == [swz(CARD_ID_QUERY)]
    assert replies and bytes(replies[0][0]) == CARD_ID_REPLY


def test_rd_transport_rejects_unknown_protocol():
    with pytest.raises(ValueError):
        RdTransport().open(udp_host="127.0.0.1", protocol="sctp")


def test_close_stream_closes_tcp_so_open_reconnects(server, monkeypatch):
    port = server.getsockname()[1]
    original_open = TcpTransport.open
    monkeypatch.setattr(
        TcpTransport,
        "open",
        lambda self, host, _port=50200, **kw: original_open(self, host, port),
    )
    rd = RdTransport()
    rd.configure(magic=MAGIC)
    assert rd.open(network_host="127.0.0.1", protocol="tcp")
    first, _ = server.accept()

    rd.close_stream()
    assert not rd.is_open

    assert rd.open()
    second, _ = server.accept()
    assert rd.is_open and rd.is_tcp
    rd.close()
    first.close()
    second.close()


def test_close_stream_leaves_udp_open():
    rd = RdTransport()
    udp = Mock(is_tcp=False)
    rd._transport = udp

    rd.close_stream()

    udp.close.assert_not_called()


def test_resync_after_ping_failure_closes_stream():
    transport = Mock(spec=RdTransport)
    status = RdStatus(transport)

    assert status._run_resync() == "CONNECTING"

    transport.drain.assert_called_once()
    transport.close_stream.assert_called_once()


def test_ack_without_reply_fires_busy(server, monkeypatch):
    port = server.getsockname()[1]
    received = []

    def controller():
        peer, _ = server.accept()
        peer.settimeout(2.0)
        received.append(peer.recv(64))
        peer.sendall(swz(bytes([ACK])))  # ACK only, no reply
        time.sleep(0.5)
        peer.close()

    thread = threading.Thread(target=controller, daemon=True)
    thread.start()

    original_open = TcpTransport.open
    monkeypatch.setattr(
        TcpTransport,
        "open",
        lambda self, host, _port=50200, **kw: original_open(self, host, port),
    )

    rd = RdTransport()
    rd.configure(magic=MAGIC, timeout=100)
    events = []
    rd.register_status_listener(events.append)
    assert rd.open(network_host="127.0.0.1", protocol="tcp")
    rd.write([bytearray(CARD_ID_QUERY)])

    deadline = time.monotonic() + 3.0
    while TransportEvent.BUSY not in events and time.monotonic() < deadline:
        time.sleep(0.01)
    rd.close()
    thread.join(timeout=2.0)

    assert received == [swz(CARD_ID_QUERY)]
    assert TransportEvent.ACK_RECEIVED in events
    assert TransportEvent.BUSY in events
    assert TransportEvent.TIMEOUT not in events
