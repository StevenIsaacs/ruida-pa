"""Application adapter abstract base class for Ruida protocol adapters.

AppAdapter defines the interface that application-layer adapters must implement
to interact with the Ruida session/driver stack. Subclasses include TuiAdapter
(TUI) and future adapters for MeerK40t, Rayforge, etc.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from rpalib.version import __version__


class AppAdapter(ABC):
    """Abstract base class for Ruida application adapters.

    Provides the lifecycle and event interface for upper-layer adapters.
    Subclasses must implement the abstract methods; non-abstract methods
    provide sensible defaults and may be overridden.
    """

    @abstractmethod
    def create_driver_and_session(self) -> None:
        """Create and configure RdSession and RdDriver instances.

        This is the initialization boundary — after this method returns,
        the adapter must be ready to establish a connection.
        """
        ...

    @abstractmethod
    def on_status_event(self, event: Any) -> None:
        """Handle a status event from the driver's RdStatus monitor.

        Args:
            event: An RdStatusEvent enum member (CONNECTED, DISCONNECTED, etc.).
        """
        ...

    @abstractmethod
    def on_reply_data(self, replies: list[str]) -> None:
        """Handle formatted reply data from the driver.

        Args:
            replies: List of formatted reply strings from the driver.
        """
        ...

    @abstractmethod
    def on_error(self, message: str) -> None:
        """Handle an error condition.

        Args:
            message: Human-readable error description.
        """
        ...

    def run_script(self, script: list[str]) -> None:
        """Queue a script for execution through the active driver.

        Raises:
            RuntimeError: If no driver has been created (call create_driver_and_session first).

        Args:
            script: List of rpascript-formatted command lines.
        """
        raise RuntimeError(
            "Adapter not initialized. Call create_driver_and_session() first."
        )

    def get_version(self) -> str:
        """Return the ruida-pa version this adapter is running.

        The default returns the version of the ruida-pa package in this
        process. Subclasses serving a remote session (e.g. over RPC) may
        override it, but the library version is normally the correct
        answer for both the direct and server-side cases. Over RPC, an
        application adapter compares this value with its own
        ``rpalib.version.__version__`` to detect a client/server version
        mismatch.
        """
        return __version__

    def start(self) -> None:
        """Start the adapter. Default implementation is a no-op."""

    def stop(self) -> None:
        """Stop the adapter. Default implementation is a no-op."""
