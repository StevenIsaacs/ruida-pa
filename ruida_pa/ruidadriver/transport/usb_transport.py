import logging
import os
from typing import Optional

from .base import Transport

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None  # type: ignore

logger = logging.getLogger(__name__)


class UsbTransport(Transport):
    """Transport implementation for USB (serial) communication using pyserial."""

    def __init__(self) -> None:
        self._serial: Optional["serial.Serial"] = None

    def open(self, device: str, **kwargs) -> bool:
        if serial is None:
            return False

        # Close any stale connection before reopening
        self.close()

        try:
            resolved = device

            # If vid:pid format, resolve via port enumeration
            if ":" in device:
                ports = list(serial.tools.list_ports.grep(device))
                if not ports:
                    return False
                resolved = ports[0].device
            elif "/" not in device and os.name == "posix":
                resolved = f"/dev/{device}"

            self._serial = serial.Serial(
                port=resolved,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0,
            )
            return True
        except OSError as exc:
            # comports() can raise ctypes.WinError (OSError) on Windows; a
            # transient failure must not kill the reconnect monitor thread.
            logger.warning("USB open failed: %s", exc)
            self._serial = None
            return False

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def write(self, packet: bytearray) -> None:
        self._serial.write(bytes(packet))

    def read(self, length: int) -> Optional[bytes]:
        data = self._serial.read(length)
        return data if data else None

    def drain(self) -> None:
        while self._serial.read(4096):
            pass

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def is_usb(self) -> bool:
        return True

    @property
    def is_udp(self) -> bool:
        return False
