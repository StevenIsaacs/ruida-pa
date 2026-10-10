"""L5 Ruida Session — thin wrapper around RdTransport and RdStatus.

RdSession provides the top-level session API for upper layers:
- connect/disconnect/shutdown lifecycle
- is_connected, is_usb, is_udp property delegation
"""

from __future__ import annotations

import threading

from ruidadriver.rd_status import RdStatus
from ruidadriver.rd_transport import RdTransport


class RdSession:
    """Ruida Session Layer (L5) — manages a transport+status session.

    Combines transport configuration/connection (L4) with status monitoring
    (L5) into a single session API used by upper layers (L6-L7).

    Usage::
        session = RdSession()
        session.transport.open(network_host='192.168.1.100')
        session.connect(timeout=5000)
        # ... use session.transport.write(...) ...
        session.disconnect()
    """

    def __init__(self) -> None:
        self.transport = RdTransport()
        self.status = RdStatus(transport=self.transport)

        # Synchronization
        self._lock = threading.Lock()

    def connect(self, timeout: int = 1000) -> bool:
        """Open transport and start status monitoring.

        Opens the transport, starts RdStatus (async — CONNECTED event
        fires asynchronously when the controller confirms). Returns True
        if the transport was opened and status monitor started.

        The timeout parameter is accepted for API compatibility but not used
        for blocking — the status monitor handles connection health checking
        asynchronously via its ping mechanism.

        Args:
            timeout: ms (accepted for API compatibility, no longer blocking).

        Returns:
            True if transport opened and status monitor started.
        """
        if self.is_connected:
            return True

        # Open transport
        if not self.transport.open():
            return False

        # Start status monitor (fires CONNECTED asynchronously)
        self.status.start()

        return True

    def disconnect(self) -> None:
        """Stop status monitoring and close transport. Idempotent."""
        self.status.stop()
        self.transport.close()

    def shutdown(self) -> None:
        """Cleanup alias for disconnect()."""
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        """True when transport is open AND RdStatus has confirmed connection."""
        return self.status.is_connected

    @property
    def is_usb(self) -> bool:
        """Delegate to transport.is_usb."""
        return self.transport.is_usb

    @property
    def is_udp(self) -> bool:
        """Delegate to transport.is_udp."""
        return self.transport.is_udp
