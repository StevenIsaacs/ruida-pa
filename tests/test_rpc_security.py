"""Security tests for the production RPC paths (issue #15).

Everything goes through the production entry points —
``start_rpyc_server()`` on the server side and ``connect_rpc()`` /
``RpcRdDriver`` on the client side — with real sockets, real TLS, and
failing assertions. Covers:

- the bind-address policy (loopback optional auth, non-loopback
  requires token + TLS),
- token enforcement for every client including loopback (no local
  bypass),
- TLS success, TLS verification failure, plain clients rejected by TLS
  servers, and mutual TLS (client certificates),
- token-over-TLS composition.

TLS tests generate throwaway self-signed certificates with the
``openssl`` CLI and skip when it is unavailable.
"""

import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from ruida_pa.rpalib.rpyc_client import RpcRdDriver, connect_rpc
from ruida_pa.rpalib.rpyc_service import start_rpyc_server
from ruida_pa.rpalib.version import __version__

# What a client sees when the server rejects it during the
# authenticator handshake: the server closes the socket, so the client
# surfaces EOF, a connection reset, or an SSL error depending on where
# the handshake failed.
AUTH_FAIL = (EOFError, ConnectionError, ssl.SSLError, OSError)


class _RpcServer:
    """A production ``start_rpyc_server()`` running in this process."""

    def __init__(self, **kwargs):
        kwargs.setdefault("host", "127.0.0.1")
        kwargs["port"] = 0  # ephemeral port per test
        kwargs["auto_start"] = False
        self.server = start_rpyc_server(**kwargs)
        self.port = self.server.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.server.start, daemon=True)
        self.thread.start()
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                time.sleep(0.05)
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("RPC server did not come up")

    def stop(self):
        self.server.close()
        self.thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


_CERTS = None
_CERT_DIR = None


def _certs():
    """Return ((server_cert, server_key), (other_cert, other_key)), cached.

    Skips the calling test when the openssl CLI is unavailable.
    """
    global _CERTS, _CERT_DIR
    if _CERTS is not None:
        return _CERTS
    try:
        subprocess.run(
            ["openssl", "version"], check=True, capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("openssl CLI not available for certificate generation")
    tmpdir = Path(__file__).resolve().parent.parent / "tmp"
    tmpdir.mkdir(exist_ok=True)
    # Module-level holder keeps the directory alive for the process
    # duration (a TemporaryDirectory deletes itself when collected);
    # tmp/ is purged regularly.
    _CERT_DIR = tempfile.TemporaryDirectory(prefix="rpc-tls-", dir=tmpdir)
    server = _generate_cert(_CERT_DIR.name, "server")
    other = _generate_cert(_CERT_DIR.name, "other")
    _CERTS = (server, other)
    return _CERTS


def _generate_cert(directory, name):
    """Generate a self-signed cert/key pair valid for localhost/127.0.0.1."""
    cert = os.path.join(directory, f"{name}-cert.pem")
    key = os.path.join(directory, f"{name}-key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key, "-out", cert,
            "-days", "2", "-nodes",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True, capture_output=True, timeout=30,
    )
    return cert, key


def _assert_version(conn):
    assert conn.root.get_version() == __version__


# --- Bind-address policy ---------------------------------------------------


def test_non_loopback_bind_requires_token_and_tls():
    # No authentication at all.
    with pytest.raises(PermissionError):
        start_rpyc_server(host="0.0.0.0", port=0, auto_start=False)
    # Token without TLS.
    with pytest.raises(PermissionError):
        start_rpyc_server(host="0.0.0.0", port=0, token="t", auto_start=False)
    # TLS without a token.
    with pytest.raises(PermissionError):
        start_rpyc_server(
            host="0.0.0.0", port=0,
            cert_path="unused-cert.pem", key_path="unused-key.pem",
            auto_start=False,
        )
    # A LAN hostname is non-loopback too.
    with pytest.raises(PermissionError):
        start_rpyc_server(host="machine.example", port=0, auto_start=False)


def test_loopback_bind_is_the_default_and_needs_no_auth():
    with _RpcServer() as srv:
        conn = connect_rpc("127.0.0.1", srv.port)
        _assert_version(conn)
        conn.close()


# --- Token authentication --------------------------------------------------


def test_loopback_token_enforced_for_every_client():
    """A token configured on a loopback bind is required from local clients.

    This is the regression test for the removed localhost bypass: an
    explicitly requested token must never be silently discarded.
    """
    with _RpcServer(token="s3cret") as srv:
        conn = connect_rpc("127.0.0.1", srv.port, token="s3cret")
        _assert_version(conn)
        conn.close()

        with pytest.raises(AUTH_FAIL):
            connect_rpc("127.0.0.1", srv.port)  # no token sent
        with pytest.raises(AUTH_FAIL):
            connect_rpc("127.0.0.1", srv.port, token="")  # empty-token attempt
        with pytest.raises(AUTH_FAIL):
            connect_rpc("127.0.0.1", srv.port, token="wrong")


def test_rpc_driver_token_passthrough():
    with _RpcServer(token="s3cret") as srv:
        driver = RpcRdDriver(port=srv.port, token="s3cret")
        try:
            assert driver.get_version() == __version__
        finally:
            driver.close()
        with pytest.raises(AUTH_FAIL):
            RpcRdDriver(port=srv.port, token="bad")


# --- TLS -------------------------------------------------------------------


def test_tls_success_and_token_over_tls():
    (server_cert, server_key), _ = _certs()
    with _RpcServer(cert_path=server_cert, key_path=server_key,
                    token="s3cret") as srv:
        conn = connect_rpc("localhost", srv.port, token="s3cret",
                           ca_path=server_cert)
        _assert_version(conn)
        conn.close()

        # Wrong token over an otherwise valid TLS session.
        with pytest.raises(AUTH_FAIL):
            connect_rpc("localhost", srv.port, token="nope",
                        ca_path=server_cert)


def test_tls_without_token_accepts_verified_client():
    (server_cert, server_key), _ = _certs()
    with _RpcServer(cert_path=server_cert, key_path=server_key) as srv:
        conn = connect_rpc("localhost", srv.port, ca_path=server_cert)
        _assert_version(conn)
        conn.close()


def test_tls_client_rejects_untrusted_server():
    (server_cert, server_key), (other_cert, _other_key) = _certs()
    with _RpcServer(cert_path=server_cert, key_path=server_key) as srv:
        with pytest.raises(ssl.SSLCertVerificationError):
            connect_rpc("localhost", srv.port, ca_path=other_cert)


def test_tls_server_rejects_plain_client():
    (server_cert, server_key), _ = _certs()
    with _RpcServer(cert_path=server_cert, key_path=server_key) as srv:
        with pytest.raises(AUTH_FAIL):
            connect_rpc("127.0.0.1", srv.port)  # no TLS on the client side


def test_mtls_client_certificate_required():
    (server_cert, server_key), (client_cert, client_key) = _certs()
    # ca_path on the server requires client certificates signed by it.
    with _RpcServer(cert_path=server_cert, key_path=server_key,
                    ca_path=client_cert) as srv:
        conn = connect_rpc("localhost", srv.port, ca_path=server_cert,
                           cert_path=client_cert, key_path=client_key)
        _assert_version(conn)
        conn.close()

        # Verified TLS but no client certificate -> rejected.
        with pytest.raises(AUTH_FAIL):
            connect_rpc("localhost", srv.port, ca_path=server_cert)


def test_rpc_driver_tls_passthrough():
    (server_cert, server_key), _ = _certs()
    with _RpcServer(cert_path=server_cert, key_path=server_key,
                    token="s3cret") as srv:
        driver = RpcRdDriver(port=srv.port, token="s3cret",
                             ca_path=server_cert)
        try:
            assert driver.get_version() == __version__
        finally:
            driver.close()


def test_client_tls_argument_validation():
    (server_cert, server_key), (client_cert, client_key) = _certs()
    # Client certificate without the CA that verifies the server.
    with pytest.raises(ValueError):
        connect_rpc("127.0.0.1", 1, cert_path=client_cert, key_path=client_key)
    # cert without key.
    with pytest.raises(ValueError):
        connect_rpc("127.0.0.1", 1, cert_path=client_cert, ca_path=server_cert)


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
