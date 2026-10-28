"""Test RPyC token authenticator logic in isolation."""
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


def _client_connect(host, port, token=None):
    """Connect to the RPyC server with optional token.

    Token protocol:
      - token is not None: send 1-byte length prefix + token bytes
      - token is None: send empty length prefix (1 byte: \\x00) — treated
        as empty token by the server authenticator
    """
    sock = socket.create_connection((host, port), timeout=5)
    if token is not None:
        token_bytes = token.encode("utf-8")
        sock.sendall(bytes([len(token_bytes)]) + token_bytes)
    else:
        sock.sendall(b"\x00")
    conn = connect_stream(SocketStream(sock))
    return conn.root


def test_correct_token():
    server = rpyc.utils.server.ThreadedServer(
        rpyc.Service, hostname="127.0.0.1", port=19991,
        authenticator=_make_authenticator("s3cret!t0k3n"),
    )
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        svc = _client_connect("127.0.0.1", 19991, token="s3cret!t0k3n")
        name = svc.exposed_get_service_name()
        print(f"PASS: Correct token authenticated (service={name})")
    except Exception as e:
        print(f"FAIL: Correct token rejected: {e}")
    finally:
        server.close()


def test_wrong_token():
    server = rpyc.utils.server.ThreadedServer(
        rpyc.Service, hostname="127.0.0.1", port=19992,
        authenticator=_make_authenticator("correct-token"),
    )
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        svc = _client_connect("127.0.0.1", 19992, token="wrong-token")
        try:
            _ = svc.exposed_get_service_name()
            print("FAIL: Wrong token was accepted")
        except Exception:
            print("PASS: Wrong token rejected on RPC call")
    except Exception as e:
        print(f"PASS: Wrong token rejected on connect: {e}")
    finally:
        server.close()


def test_localhost_no_token():
    server = rpyc.utils.server.ThreadedServer(
        rpyc.Service, hostname="127.0.0.1", port=19993,
        authenticator=_make_authenticator("secret"),
    )
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        svc = _client_connect("127.0.0.1", 19993, token=None)
        name = svc.exposed_get_service_name()
        print(f"PASS: Localhost no-token allowed (service={name})")
    except Exception as e:
        print(f"FAIL: Localhost no-token rejected: {e}")
    finally:
        server.close()


if __name__ == "__main__":
    print("=== Auth Token Tests ===\n")
    test_correct_token()
    test_wrong_token()
    test_localhost_no_token()
    print("\n=== All auth tests complete ===")
