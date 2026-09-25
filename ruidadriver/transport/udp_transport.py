import logging
import socket
from typing import Optional

from .base import Transport

logger = logging.getLogger(__name__)


class UdpTransport(Transport):
    """Transport implementation for UDP network communication."""

    def __init__(self) -> None:
        self._socket: Optional[socket.socket] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._logged_reset = False

    def open(self, host: str, port: int = 50200, **kwargs) -> bool:
        # Close any stale socket before reopening
        self.close()
        # A fresh socket may hit a fresh WSAECONNRESET, so re-arm the
        # one-time debug log for this connection.
        self._logged_reset = False

        # Determine local IP that routes to the controller
        # (UDP connect() sets the route without sending data)
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            try:
                temp_sock.connect((host, port))
                local_ip = temp_sock.getsockname()[0]
            except OSError as exc:
                logger.warning("UDP connect failed: %s", exc)
                return False
        finally:
            temp_sock.close()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setblocking(False)
        try:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)  # 128KB receive buffer
        except OSError:
            pass  # Fall back to OS default if platform rejects the requested size
        try:
            self._socket.bind((local_ip, 40200))
        except OSError:
            self._socket.close()
            self._socket = None
            return False
        self._host = host
        self._port = port
        return True

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def write(self, packet: bytearray) -> None:
        if self._socket is None:
            raise OSError("Socket is not open")
        self._socket.sendto(bytes(packet), (self._host, self._port))

    def read(self, length: int) -> Optional[bytes]:
        if self._socket is None:
            return None
        try:
            return self._socket.recv(length, socket.MSG_DONTWAIT)
        except (BlockingIOError, ConnectionResetError) as exc:
            if isinstance(exc, ConnectionResetError) and not self._logged_reset:
                logger.debug("UDP read reset (WSAECONNRESET); treating as transient")
                self._logged_reset = True
            return None

    def drain(self) -> None:
        if self._socket is None:
            return
        while self.read(65536) is not None:
            pass

    @property
    def is_open(self) -> bool:
        return self._socket is not None

    @property
    def is_usb(self) -> bool:
        return False

    @property
    def is_udp(self) -> bool:
        return True
