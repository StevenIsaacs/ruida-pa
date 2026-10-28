"""Test RPyC token authenticator logic in isolation.

The authenticator requires the configured token from EVERY client,
loopback included — there is no local bypass (issue #15). The
production-path coverage (start_rpyc_server + connect_rpc) lives in
tests/test_rpc_security.py; this file exercises the authenticator
directly.
"""
import socket
import threading
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from ruida_pa.rpalib.rpyc_service import _make_authenticator
import rpyc
from rpyc.core.stream import SocketStream
from rpyc.utils.factory import connect_stream

AUTH_FAIL = (EOFError, ConnectionError, OSError)


def _client_connect(host, port, token=None):
    """Connect to an RPyC server, optionally sending the token handshake.

    Token protocol: a 1-byte length prefix followed by the UTF-8 token
    bytes. ``token=None`` sends the empty prefix ``b"\\x00"`` so the
    server's no-token/empty-token rejection path is exercised.
    """
    sock = socket.create_connection((host, port), timeout=5)
    if token is not None:
        token_bytes = token.encode("utf-8")
        sock.sendall(bytes([len(token_bytes)]) + token_bytes)
    else:
        sock.sendall(b"\x00")
    conn = connect_stream(SocketStream(sock))
    return conn.root


def _start_server(port, token):
    server = rpyc.utils.server.ThreadedServer(
        rpyc.Service, hostname="127.0.0.1", port=port,
        authenticator=_make_authenticator(token),
    )
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    time.sleep(0.2)
    return server


def test_correct_token():
    server = _start_server(19991, "s3cret!t0k3n")
    try:
        svc = _client_connect("127.0.0.1", 19991, token="s3cret!t0k3n")
        name = svc.get_service_name()  # "" for a bare rpyc.Service
        assert isinstance(name, str)
        print("PASS: Correct token authenticated")
    finally:
        server.close()


def test_wrong_token():
    server = _start_server(19992, "correct-token")
    try:
        try:
            svc = _client_connect("127.0.0.1", 19992, token="wrong-token")
            svc.get_service_name()
            raise AssertionError("Wrong token was accepted")
        except AUTH_FAIL:
            print("PASS: Wrong token rejected")
    finally:
        server.close()


def test_localhost_no_token_rejected():
    """No local bypass: a token-configured server rejects token-less locals."""
    server = _start_server(19993, "secret")
    try:
        try:
            svc = _client_connect("127.0.0.1", 19993, token=None)
            svc.get_service_name()
            raise AssertionError("Token-less localhost client was accepted")
        except AUTH_FAIL:
            print("PASS: Localhost no-token rejected")
    finally:
        server.close()


if __name__ == "__main__":
    print("=== Auth Token Tests ===\n")
    test_correct_token()
    test_wrong_token()
    test_localhost_no_token_rejected()
    print("\n=== All auth tests complete ===")
