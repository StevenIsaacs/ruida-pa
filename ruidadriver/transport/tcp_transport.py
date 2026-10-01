import logging
import socket
from typing import Optional

from protocols.ruida.ruida_protocol import ACK
from rpalib.rpa_swizzler import RpaSwizzler

from .base import Transport

logger = logging.getLogger(__name__)

# First byte of a GET_SETTING reply (DA 01 <msb> <lsb> <5 data bytes>).
_REPLY_PREFIX = 0xDA
_REPLY_LENGTH = 9


class TcpTransport(Transport):
    """Transport implementation for TCP network communication.

    Newer controllers (e.g. RDC8445S) accept the swizzled Ruida command
    stream on TCP port 50200 instead of UDP. Packets carry no checksum
    prefix; the controller still answers every write with a single ACK
    byte, followed by any GET_SETTING replies.

    TCP is a byte stream, so an ACK and its replies can arrive in a single
    read. read() re-frames the stream into the units RdTransport expects:
    a single handshake byte, or one or more complete 9-byte replies.
    """

    def __init__(self, swizzler: RpaSwizzler) -> None:
        self._swizzler = swizzler
        self._socket: Optional[socket.socket] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._buffer = bytearray()

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
        self._buffer.clear()

    def write(self, packet: bytearray) -> None:
        if self._socket is None:
            raise OSError("Socket is not open")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("TCP tx: %s", self._unswizzled_hex(packet))
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
        if self._socket is None:
            return None
        self._fill(length)
        return self._next_message()

    def drain(self) -> None:
        if self._socket is None:
            return
        self._buffer.clear()
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

    def _fill(self, length: int) -> None:
        """Append any readable bytes to the buffer without blocking.

        Raises OSError (and closes the socket) when the controller has
        closed the connection, so the status monitor can reconnect.
        """
        try:
            data = self._socket.recv(length)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.close()
            raise
        if not data:
            self.close()
            raise ConnectionResetError("Controller closed the TCP connection")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("TCP rx: %s", self._unswizzled_hex(data))
        self._buffer.extend(data)

    def _unswizzled_hex(self, data: bytes | bytearray) -> str:
        magic = self._swizzler.magic
        return bytes(RpaSwizzler.unswizzle_byte(b, magic) for b in data).hex(" ")

    def _next_message(self) -> Optional[bytes]:
        """Pop the next complete message from the buffer, if any."""
        if not self._buffer:
            return None
        magic = self._swizzler.magic
        first = RpaSwizzler.unswizzle_byte(self._buffer[0], magic)
        if first != _REPLY_PREFIX:
            if first != ACK:
                logger.debug("TCP unexpected handshake byte 0x%02X", first)
            message = bytes(self._buffer[:1])
            del self._buffer[:1]
            return message
        count = 0
        while (count + 1) * _REPLY_LENGTH <= len(self._buffer):
            start = count * _REPLY_LENGTH
            if RpaSwizzler.unswizzle_byte(self._buffer[start], magic) != _REPLY_PREFIX:
                break
            count += 1
        if count == 0:
            return None
        end = count * _REPLY_LENGTH
        message = bytes(self._buffer[:end])
        del self._buffer[:end]
        return message

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
