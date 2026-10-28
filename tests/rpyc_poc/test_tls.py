"""Test RPyC TLS connection with self-signed certs."""
import os
import ssl as ssl_mod
import sys
import tempfile
import threading
import time
import datetime

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import cryptography.hazmat.primitives.serialization as serialization

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from rpyc.utils.authenticators import SSLAuthenticator
from rpyc.utils.factory import ssl_connect
from rpyc.utils.server import ThreadedServer
from ruida_pa.rpalib.rpyc_service import RpycTuiService


def _gen_self_signed_certs(tempdir):
    """Generate self-signed CA + server certs."""
    ca_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    ca_subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Test CA"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject).issuer_name(issuer)
        .public_key(ca_key.public_key())
        .serial_number(1000)
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    server_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    server_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_subject).issuer_name(ca_subject)
        .public_key(server_key.public_key())
        .serial_number(1001)
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    ca_cert_path = os.path.join(tempdir, "ca-cert.pem")
    server_cert_path = os.path.join(tempdir, "server-cert.pem")
    server_key_path = os.path.join(tempdir, "server-key.pem")
    with open(ca_cert_path, "wb") as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    with open(server_cert_path, "wb") as f:
        f.write(server_cert.public_bytes(serialization.Encoding.PEM))
    with open(server_key_path, "wb") as f:
        f.write(server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return ca_cert_path, server_cert_path, server_key_path


def test_tls_connection():
    """Start RPC server with TLS, connect and call a method."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ca_cert, server_cert, server_key = _gen_self_signed_certs(tmpdir)
        # Server: SSLAuthenticator wraps accepted sockets in TLS
        # Use CERT_NONE — the client has no client cert and we are testing
        # basic TLS connectivity, not mutual TLS.
        authenticator = SSLAuthenticator(
            server_key, server_cert, ca_cert,
            cert_reqs=ssl_mod.CERT_NONE,
        )
        service = RpycTuiService()
        server = ThreadedServer(
            service, hostname="127.0.0.1", port=19994,
            authenticator=authenticator,
        )
        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.5)
        try:
            # Client: use ssl_connect (standard RPyC TLS client pattern)
            conn = ssl_connect(
                "127.0.0.1", 19994,
                keyfile=None, certfile=None,
                ca_certs=ca_cert,
            )
            svc = conn.root
            result = svc.exposed_is_connected()
            print(f"PASS: TLS connection works, is_connected={result}")
            conn.close()
        except Exception as e:
            print(f"FAIL: TLS connection failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            server.close()


if __name__ == "__main__":
    print("=== TLS Connection Test ===\n")
    test_tls_connection()
    print("\n=== TLS test complete ===")
