import logging
import socket
from typing import Optional

from .base import Transport

logger = logging.getLogger(__name__)


class TcpTransport(Transport):
    """Transport implementation for TCP network communication.

    Newer controllers (e.g. RDC8445S) accept the swizzled Ruida command
    stream on TCP port 50200 instead of UDP. Packets carry no checksum
    prefix; the controller still answers every write with a single ACK
    byte, followed by any GET_SETTING replies.

    This class is a raw byte-stream transport. It performs no protocol
    framing, swizzling or message parsing — TCP is a stream, so the
    caller (``RdTransport``, L4) is responsible for reassembling the
    stream into the ACK and reply units the handshake expects.
    """

    def __init__(self) -> None:
        self._socket: Optional[socket.socket] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None

    def open(self, host: str, port: int = 50200, timeout: float = 3.0, **kwargs) -> bool:
        self.close()
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            logger.warning("TCP connect to %s:%d failed: %s", host, port, exc)
            return False
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        self._socket = sock
        self._host = host
        self._port = port
        return True

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def write(self, packet: bytearray) -> None:
        if self._socket is None:
            raise OSError("Socket is not open")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("TCP tx: %s", bytes(packet).hex(" "))
        self._socket.settimeout(2.0)
        try:
            self._socket.sendall(bytes(packet))
        except OSError:
            self.close()
            raise
        finally:
            if self._socket is not None:
                self._socket.setblocking(False)

    def read(self, length: int) -> Optional[bytes]:
        """Non-blocking read of raw bytes from the stream.

        Returns up to ``length`` bytes, or None when none are available.
        Raises ConnectionResetError (and closes the socket) when the
        controller has closed the connection, so the status monitor can
        reconnect.
        """
        if self._socket is None:
            return None
        try:
            data = self._socket.recv(length)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError:
            self.close()
            raise
        if not data:
            self.close()
            raise ConnectionResetError("Controller closed the TCP connection")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("TCP rx: %s", data.hex(" "))
        return data

    def drain(self) -> None:
        if self._socket is None:
            return
        while True:
            try:
                if not self._socket.recv(65536):
                    self.close()
                    return
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.close()
                return

    @property
    def is_open(self) -> bool:
        return self._socket is not None

    @property
    def is_usb(self) -> bool:
        return False

    @property
    def is_udp(self) -> bool:
        return False

    @property
    def is_tcp(self) -> bool:
        return True
