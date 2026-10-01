"""Tests for TcpTransport stream framing and RdTransport TCP handshake.

A localhost socket stands in for the controller. Byte sequences mirror
traffic captured from an RDC8445S on TCP port 50200 (magic 0x88).
"""

import socket
import threading
import time

import pytest

from protocols.ruida.ruida_protocol import ACK
from rpalib.rpa_swizzler import RpaSwizzler
from ruidadriver.rd_transport import RdTransport
from ruidadriver.transport import TcpTransport

MAGIC = 0x88
CARD_ID_QUERY = bytes([0xDA, 0x00, 0x05, 0x7E])
CARD_ID_REPLY = bytes([0xDA, 0x01, 0x05, 0x7E, 0x09, 0x00, 0x42, 0x20, 0x10])
X_POS_REPLY = bytes([0xDA, 0x01, 0x04, 0x21, 0x00, 0x00, 0x00, 0x4E, 0x0F])


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
    swizzler = RpaSwizzler()
    swizzler.set_magic(MAGIC)
    transport = TcpTransport(swizzler)
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


def test_ack_and_reply_in_one_segment_are_split(server):
    transport, peer = connect(server)
    peer.sendall(swz(bytes([ACK]) + CARD_ID_REPLY))
    assert read_until(transport) == swz(bytes([ACK]))
    assert read_until(transport) == swz(CARD_ID_REPLY)
    transport.close()
    peer.close()


def test_consecutive_replies_are_returned_together(server):
    transport, peer = connect(server)
    peer.sendall(swz(CARD_ID_REPLY + X_POS_REPLY))
    assert read_until(transport) == swz(CARD_ID_REPLY + X_POS_REPLY)
    transport.close()
    peer.close()


def test_partial_reply_waits_for_remaining_bytes(server):
    transport, peer = connect(server)
    peer.sendall(swz(CARD_ID_REPLY[:4]))
    time.sleep(0.05)
    assert transport.read(65536) is None
    peer.sendall(swz(CARD_ID_REPLY[4:]))
    assert read_until(transport) == swz(CARD_ID_REPLY)
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
    assert rd.open(udp_host="127.0.0.1", protocol="tcp")
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
