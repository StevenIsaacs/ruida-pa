from abc import ABC, abstractmethod
from typing import Optional


class Transport(ABC):
    """Abstract base for transport implementations (UDP, TCP or Serial/USB)."""

    @abstractmethod
    def open(self, **kwargs) -> bool:
        """Open a device for communications. Returns True on success."""

    @abstractmethod
    def close(self) -> None:
        """Close the opened transport."""

    @abstractmethod
    def write(self, packet: bytearray) -> None:
        """Write a single packaged packet to the interface."""

    @abstractmethod
    def read(self, length: int) -> Optional[bytes]:
        """Non-blocking read. Returns None if no data available."""

    @abstractmethod
    def drain(self) -> None:
        """Discard all pending inbound data (for resync)."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @property
    @abstractmethod
    def is_usb(self) -> bool: ...

    @property
    @abstractmethod
    def is_udp(self) -> bool: ...

    @property
    def is_tcp(self) -> bool:
        """True for stream transports (TCP). Defaults to False."""
        return False
