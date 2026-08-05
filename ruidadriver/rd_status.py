"""L5 Ruida Status Monitor — connection lifecycle and periodic status querying.

RdStatus manages a background monitor thread that handles:
- Transport connection lifecycle (connect, retry, reconnect)
- Periodic ping to verify controller connectivity
- Periodic status query commands (e.g., machine position)
- Session event notification to registered listeners
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, Optional

from ruidadriver.rd_transport import RdTransport
from ruidadriver.transport_events import TransportEvent


class RdStatusEvent(Enum):
    """Session-layer events fired by RdStatus to registered listeners."""

    TRANSPORT_UDP = "TRANSPORT_UDP"
    TRANSPORT_USB = "TRANSPORT_USB"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTED = "RECONNECTED"
    TERMINATED = "TERMINATED"
    SCRIPT_ERROR = "SCRIPT_ERROR"
    PING_SENT = "PING_SENT"
    PING_REPLIED = "PING_REPLIED"
    QUERY_SENT = "QUERY_SENT"
    QUERY_RECEIVED = "QUERY_RECEIVED"


class RdStatus:
    """Ruida Session Layer (L5) — manages connection lifecycle and status monitoring.

    Runs a background thread with a state machine for automatic connect/reconnect,
    periodic pings, and status query commands. Notifies registered listeners of
    session-level events.

    The state machine transitions:
        CONNECTING → WAIT_TO_PING → SEND_PING → PING_REPLY → WAIT_TO_POLL
            → SEND_QUERY → REPLY_PENDING → WAIT_TO_POLL (loop)
        PING_REPLY → RESYNC → WAIT_TO_PING (failure recovery)
        Any state → CONNECTING on transport DROPPED/CLOSED
    """

    # Class-level constants
    PING_RETRY_COUNT = 10  # max consecutive ping failures
    PING_RETRY_DELAY = 1.0  # seconds between ping retries
    POLL_INTERVAL = 0.5  # seconds; default query_interval if not set
    CONNECT_RETRY_DELAY = 1.0  # seconds between connect attempts

    def __init__(
        self,
        transport: RdTransport,
        ping_cmd: Optional[bytearray] = None,
        ping_interval: int = 1000,
        query_cmds: Optional[list[bytearray]] = None,
        connect_interval: int = 1000,
        query_interval: int = 1000,
    ) -> None:
        """Initialize RdStatus with required transport and optional config.

        Args:
            transport: RdTransport instance (required).
            ping_cmd: Single ping command (e.g., GET_SETTING CARD_ID).
            ping_interval: ms between pings (default 5000).
            query_cmds: Status query command list.
            connect_interval: ms between connect retry attempts (default 1000).
            query_interval: ms between query command cycles (default 1000).
        """
        self.transport = transport
        self._ping_cmd = ping_cmd
        self._ping_interval = ping_interval
        self._query_cmds = list(query_cmds) if query_cmds else []
        self._connect_interval = connect_interval
        self._query_interval = query_interval

        # Thread synchronization
        self._lock: threading.RLock = threading.RLock()
        self._shutdown: threading.Event = threading.Event()
        # First ping optimization — send immediately on fresh connection
        self._first_ping = True

        # DISCONNECTED guard — prevents double dispatch in CONNECTING state
        self._disconnect_fired = False

        # Confirmed-connection flag — True only after a ping reply is received
        self._connected: bool = False

        # Transport event mechanism
        self._transport_event: threading.Event = threading.Event()
        self._last_event: Optional[TransportEvent] = None

        # Monitor thread
        self._monitor_thread: Optional[threading.Thread] = None

        # Listeners
        self._listeners: list[Callable] = []

        # Connection logging callback — receives human-readable messages
        self._connection_log: Optional[Callable[[str], None]] = None

        # Mutable config lock
        self._config_lock: threading.Lock = threading.Lock()

    # ---- Listener Registration ----

    def register_status_listener(self, listener: Callable) -> None:
        """Register a listener for RdStatusEvent notifications.

        Thread-safe via RLock.
        """
        with self._lock:
            self._listeners.append(listener)

    def unregister_status_listener(self, listener: Callable) -> None:
        """Remove a previously registered status listener. Thread-safe via RLock."""
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def set_connection_log(self, callback: Optional[Callable[[str], None]]) -> None:
        """Set or clear the connection logging callback."""
        self._connection_log = callback

    def _log_connection(self, msg: str) -> None:
        """Fire the connection log callback if registered."""
        cb = self._connection_log
        if cb:
            try:
                cb(msg)
            except Exception:
                pass  # Never let logging crash the monitor thread

    def _notify_listeners(self, event: RdStatusEvent) -> None:
        """Notify all registered listeners of a status event.

        Each listener is wrapped in try/except to prevent one bad listener
        from crashing the monitor thread. Thread-safe via RLock.
        """
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass  # Isolate listener failures

    # ---- Mutable Config Setters ----

    def set_ping_command(self, command: bytearray) -> None:
        """Set the ping command. Thread-safe. Takes effect on next ping cycle."""
        with self._config_lock:
            self._ping_cmd = command

    def set_ping_interval(self, interval_ms: int) -> None:
        """Set ping interval in ms. Clamped to minimum 100ms. Thread-safe."""
        if interval_ms < 100:
            raise ValueError(
                f"ping_interval too small: {interval_ms}ms (minimum 100ms)"
            )
        with self._config_lock:
            self._ping_interval = interval_ms

    def set_query_commands(self, commands: list[bytearray]) -> None:
        """Set the status query command list. Thread-safe. Takes effect on next query cycle."""
        with self._config_lock:
            self._query_cmds = list(commands)

    def set_connect_interval(self, interval_ms: int) -> None:
        """Set connect retry interval in ms. Thread-safe."""
        with self._config_lock:
            self._connect_interval = interval_ms

    def set_query_interval(self, interval_ms: int) -> None:
        """Set query interval in ms. Thread-safe."""
        with self._config_lock:
            self._query_interval = interval_ms

    # ---- Lifecycle: start/stop ----

    def start(self) -> None:
        """Start the status monitor thread.

        Clears shutdown flag, registers transport listener, creates and starts
        a daemon monitor thread. No-op if monitor thread is already running.
        Re-starting after stop() is supported.
        """
        if self._monitor_thread and self._monitor_thread.is_alive():
            return  # No-op if already running

        self._shutdown.clear()
        # Register the transport listener
        self.transport.register_status_listener(self._transport_listener)
        # Create and start monitor thread
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="rdstatus-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        """Stop the status monitor thread.

        Sets shutdown flag, joins the monitor thread (2s timeout),
        deregisters transport listener, and notifies TERMINATED.
        Idempotent — safe to call multiple times.
        """
        was_running = (
            self._monitor_thread is not None and self._monitor_thread.is_alive()
        )
        self._shutdown.set()
        self._transport_event.set()  # Unblock any wait in _wait_for_event

        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)

        self._monitor_thread = None
        self._connected = False

        try:
            self.transport.unregister_status_listener(self._transport_listener)
        except (ValueError, AttributeError):
            pass  # Guard against missing method or listener not found

        if was_running:
            self._notify_listeners(RdStatusEvent.TERMINATED)

    # ---- Transport Listener ----

    def _transport_listener(self, event: TransportEvent) -> None:
        """Receive TransportEvents from RdTransport and signal the monitor thread.

        Registered with RdTransport via register_status_listener(). Stores the
        event and sets the _transport_event to unblock _wait_for_event.
        """
        self._last_event = event
        self._transport_event.set()

    # ---- Wait-for-Event Helper ----

    def _wait_for_event(
        self,
        timeout: float,
        expected_events: Optional[list[TransportEvent]] = None,
    ) -> Optional[TransportEvent]:
        """Wait for one of expected_events, or a simple delay watching for disconnect.

        Args:
            timeout: Maximum time in seconds to wait.
            expected_events: Events to wait for. If None, defaults to
                [DROPPED, CLOSED, REPLY_ERROR] for responsiveness.

        Returns:
            The TransportEvent that fired, or None if timeout expired.
            Only returns events that are in expected_events. Unexpected events
            are silently ignored and waiting continues.

        NOTE: The 0.2s inner loop interval ensures shutdown responsiveness.
        """
        if expected_events is None:
            expected_events = [
                TransportEvent.DROPPED,
                TransportEvent.CLOSED,
                TransportEvent.TIMEOUT,
                TransportEvent.REPLY_ERROR,
            ]

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._transport_event.wait(min(remaining, 0.2)):
                # Event fired — capture and clear
                event = self._last_event
                self._transport_event.clear()
                self._last_event = None
                if event in expected_events:
                    return event
                # Unexpected event: ignore and continue waiting
                continue
            # Timeout on event wait within this iteration
            if self._shutdown.is_set():
                return None
        return None  # Full timeout expired

    # ---- Properties ----

    @property
    def is_connected(self) -> bool:
        """True only after the controller confirms connectivity (a ping reply
        was received) while the transport is still open and the monitor runs.
        Liveness after confirmation is maintained by the status queries."""
        return (
            self.transport.is_open
            and self._connected
            and self._monitor_thread is not None
            and self._monitor_thread.is_alive()
        )

    # ---- Monitor Loop ----

    def _monitor_loop(self) -> None:
        """Main monitor loop: runs the state machine until shutdown.

        All states check _shutdown and exit thread if set.
        Any transport drop/close (DROPPED or CLOSED event) transitions to CONNECTING.
        """
        state = "CONNECTING"
        # State-local variables
        retries = 0

        while not self._shutdown.is_set():
            if state == "CONNECTING":
                state = self._run_connecting()
            elif state == "WAIT_TO_PING":
                retries = self.PING_RETRY_COUNT
                state = self._run_wait_to_ping()
            elif state == "SEND_PING":
                state = self._run_send_ping()
            elif state == "PING_REPLY":
                state, retries = self._run_ping_reply(retries)
            elif state == "RESYNC":
                state = self._run_resync()
            elif state == "WAIT_TO_POLL":
                state = self._run_wait_to_poll()
            elif state == "SEND_QUERY":
                state = self._run_send_query()
            elif state == "REPLY_PENDING":
                state = self._run_reply_pending()
            else:
                # Unknown state — fall back to CONNECTING
                state = "CONNECTING"

    def _run_connecting(self) -> str:
        """CONNECTING state: establish transport connection.

        Always closes any stale connection and reopens fresh.
        On success → WAIT_TO_PING with first_ping flag for immediate send.
        On timeout → retry (re-enter CONNECTING).

        Guards against double DISCONNECTED: uses _disconnect_fired flag to
        fire DISCONNECTED only once per disconnect cycle.
        Skips transport re-open when already open (UDP socket alive even
        with dead controller) — avoids unnecessary thread/socket churn.
        For USB, always reopens because pyserial's is_open lies about
        handles whose device has been physically removed.
        """
        while not self._shutdown.is_set():
            # Any entry into CONNECTING means connectivity is not confirmed
            self._connected = False

            # Notify DISCONNECTED exactly once per disconnect cycle
            if self.transport.is_open and not self._disconnect_fired:
                self._notify_listeners(RdStatusEvent.DISCONNECTED)
                self._log_connection("[STATUS] Disconnecting (transport open, no response)")
                self._disconnect_fired = True

            # If transport is already open and no alternative transport is available,
            # skip the reopen overhead and just retry the ping cycle.
            # (When USB is available, always fall through so open() can try it first.)
            if (
                self.transport.is_open
                and not self.transport.is_usb
                and not self.transport.has_usb
            ):
                # Wait reconnect interval (responds to transport drops)
                self._log_connection(
                    f"[STATUS] Reconnecting (socket alive, waiting {self._connect_interval}ms)..."
                )
                self._wait_for_event(self._connect_interval / 1000.0)
                if self._shutdown.is_set():
                    return "CONNECTING"
                self._first_ping = True
                return "WAIT_TO_PING"

            # Normal open path for closed transport (USB reconnect, etc.)
            self._log_connection("[STATUS] Reopening transport...")
            self.transport.open()
            event = self._wait_for_event(
                self._connect_interval / 1000.0,
                [TransportEvent.OPENED],
            )
            if self._shutdown.is_set():
                return "CONNECTING"
            if event is TransportEvent.OPENED:
                self._log_connection("[STATUS] Transport reopened")
                self._first_ping = True
                return "WAIT_TO_PING"
            # Timeout — retry
        return "CONNECTING"

    def _run_wait_to_ping(self) -> str:
        """WAIT_TO_PING state: wait for ping interval before sending next ping.

        Checks _shutdown before and after _wait_for_event.
        On DROPPED/CLOSED → CONNECTING.
        On timeout (full interval) → SEND_PING.

        First ping optimization: send immediately on fresh connection
        instead of waiting for the full interval.
        """
        if self._shutdown.is_set():
            return "WAIT_TO_PING"

        # Send first ping immediately for fast initial connection
        if self._first_ping:
            self._first_ping = False
            return "SEND_PING"

        event = self._wait_for_event(
            self._ping_interval / 1000.0,
        )
        if self._shutdown.is_set():
            return "WAIT_TO_PING"
        if event is TransportEvent.DROPPED or event is TransportEvent.CLOSED:
            return "CONNECTING"
        # Timeout — no disconnect, proceed to send ping
        return "SEND_PING"

    def _run_send_ping(self) -> str:
        """SEND_PING state: send the ping command to the controller.

        Guard: if transport not open → CONNECTING.
        Send ping_cmd via transport.write([ping_cmd]).
        Notify PING_SENT. Transition to PING_REPLY.
        """
        if not self.transport.is_open:
            return "CONNECTING"
        if self._ping_cmd is not None:
            self.transport.write([self._ping_cmd])
        self._notify_listeners(RdStatusEvent.PING_SENT)
        return "PING_REPLY"

    def _run_ping_reply(self, retries: int) -> tuple[str, int]:
        """PING_REPLY state: wait for reply to the ping command with retry.

        retries already initialized by WAIT_TO_PING; persists across self-loops.
        On REPLY_FORWARDED → fire CONNECTED + PING_REPLIED, go to WAIT_TO_POLL.
        On DROPPED/CLOSED → CONNECTING.
        On timeout → decrement retries. If retries remain → self-loop.
        If exhausted → RESYNC.
        """
        while not self._shutdown.is_set():
            event = self._wait_for_event(
                self.PING_RETRY_DELAY,
                [
                    TransportEvent.REPLY_FORWARDED,
                    TransportEvent.DROPPED,
                    TransportEvent.CLOSED,
                ],
            )
            if self._shutdown.is_set():
                return ("CONNECTING", retries)
            if event is TransportEvent.REPLY_FORWARDED:
                self._connected = True
                self._notify_listeners(RdStatusEvent.PING_REPLIED)
                self._notify_listeners(RdStatusEvent.CONNECTED)
                self._log_connection("[STATUS] Ping OK")
                self._disconnect_fired = False
                return ("WAIT_TO_POLL", retries)
            if event is TransportEvent.DROPPED or event is TransportEvent.CLOSED:
                return ("CONNECTING", retries)
            # Timeout
            retries -= 1
            if retries > 0:
                self._log_connection(
                    f"[STATUS] Ping timeout (retries left: {retries})"
                )
                continue  # Self-loop (re-enter PING_REPLY)
            else:
                self._notify_listeners(RdStatusEvent.DISCONNECTED)
                self._log_connection(
                    f"[STATUS] Ping failed after {self.PING_RETRY_COUNT} retries"
                )
                self._disconnect_fired = True
                return ("RESYNC", retries)
        return ("CONNECTING", retries)

    def _run_resync(self) -> str:
        """RESYNC state: drain transport after ping failure.

        Call transport.drain() to clear stale data.
        No notification — ping failed silently.
        Transition to CONNECTING to enter reconnect cycle.
        """
        if not self._shutdown.is_set():
            self.transport.drain()
        return "CONNECTING"

    def _run_wait_to_poll(self) -> str:
        """WAIT_TO_POLL state: wait for query interval before sending queries.

        No connection notification — handled in PING_REPLY's REPLY_FORWARDED handler.
        On DROPPED/CLOSED → CONNECTING.
        On timeout → SEND_QUERY.
        """
        while not self._shutdown.is_set():
            event = self._wait_for_event(
                self._query_interval / 1000.0,
                [TransportEvent.DROPPED, TransportEvent.CLOSED, TransportEvent.TIMEOUT],
            )
            if self._shutdown.is_set():
                return "WAIT_TO_POLL"
            if event is TransportEvent.DROPPED or event is TransportEvent.CLOSED:
                return "CONNECTING"
            if event is TransportEvent.TIMEOUT:
                return "CONNECTING"
            # Timeout — proceed to send queries
            return "SEND_QUERY"
        return "WAIT_TO_POLL"

    def _run_send_query(self) -> str:
        """SEND_QUERY state: send all status query commands.

        Guard: if transport not open → CONNECTING.
        Send query_cmds via transport.write(query_cmds). Notify QUERY_SENT.
        Transition to REPLY_PENDING.
        """
        if not self.transport.is_open:
            return "CONNECTING"
        if self._query_cmds:
            self.transport.write(self._query_cmds)
            self._notify_listeners(RdStatusEvent.QUERY_SENT)
        return "REPLY_PENDING"

    def _run_reply_pending(self) -> str:
        """REPLY_PENDING state: wait for replies to status query commands.

        If query_cmds is empty → WAIT_TO_POLL immediately.
        On REPLY_FORWARDED → notify QUERY_RECEIVED, go to WAIT_TO_POLL.
        On DROPPED/CLOSED → CONNECTING.
        On timeout → notify DISCONNECTED, go to CONNECTING.
        """
        if not self._query_cmds:
            return "WAIT_TO_POLL"

        while not self._shutdown.is_set():
            event = self._wait_for_event(
                self.PING_RETRY_DELAY,
                [
                    TransportEvent.REPLY_FORWARDED,
                    TransportEvent.DROPPED,
                    TransportEvent.CLOSED,
                ],
            )
            if self._shutdown.is_set():
                return "WAIT_TO_POLL"
            if event is TransportEvent.REPLY_FORWARDED:
                self._notify_listeners(RdStatusEvent.QUERY_RECEIVED)
                self._log_connection("[STATUS] Query reply received")
                return "WAIT_TO_POLL"
            if event is TransportEvent.DROPPED or event is TransportEvent.CLOSED:
                return "CONNECTING"
            # Timeout
            self._connected = False
            self._notify_listeners(RdStatusEvent.DISCONNECTED)
            self._log_connection("[STATUS] Query timeout — disconnecting")
            self._disconnect_fired = True
            return "CONNECTING"
        return "WAIT_TO_POLL"
