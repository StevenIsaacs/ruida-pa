"""
L6 Ruida Driver — command encoding, queued script execution, and status monitoring integration.

RdDriver provides:
- Script interpretation via Background Script Runner (queue-based daemon thread)
- Encoded command transmission through the Session (L5)
- Status and reply listener infrastructure forwarding to application callbacks
- Internal machine status tracking (position, status bits, card ID)
"""

from __future__ import annotations

import queue
import threading
import logging
import time
from typing import Any, Callable, TypedDict

import protocols.ruida.ruida_protocol as rdap
from rpalib.ruida_transcoder import RdDecoder, RdEncoder
from rpascript.encoding import (
    encode_command,
    is_end_job,
    parse_value,
    should_include_in_checksum,
)
from rpascript.interpreter import ScriptParser
from ruidadriver.rd_session import RdSession
from ruidadriver.rd_status import RdStatusEvent

_UNSET = object()  # Sentinel for "never seen before" in status change detection


class StatusDict(TypedDict, total=False):
    """Status update dict sent from RdDriver to status listeners.

    All fields are optional — only keys that changed are present.
    Non-bool values are (raw_value, formatted_string) tuples.
    Machine status bits remain simple bools.
    """

    MEM_CURRENT_POSITION_X: tuple[float, str]
    MEM_CURRENT_POSITION_Y: tuple[float, str]
    MEM_CURRENT_POSITION_Z: tuple[float, str]
    MEM_CURRENT_POSITION_U: tuple[float, str]
    MEM_CARD_ID: tuple[int, str]
    MEM_BED_SIZE_X: tuple[float, str]
    MEM_BED_SIZE_Y: tuple[float, str]
    MEM_MACHINE_STATUS: tuple[int, str]
    MACHINE_STATUS_MOVING: bool
    MACHINE_STATUS_LAYER_END: bool
    MACHINE_STATUS_JOB_RUNNING: bool


class RdDriver:
    """Ruida Driver Layer (L6) — script interpretation and background execution.

    Manages script execution via a background daemon thread, with queued
    command transmission, connection-aware retry, and status/reply event
    forwarding to registered application listeners.

    Usage::
        driver = RdDriver()
        driver.register_status_listener(...)
        driver.start(udp_host='192.168.1.100')
        driver.run(['GET_SETTING MEM_CARD_ID'])
        # ... script executes in background ...
        driver.stop()
    """

    # Ping command — MEM_CARD_ID reply detects controller changes
    _PING_SCRIPT = ["GET_SETTING MEM_CARD_ID"]

    # Query command segment — sent at configured query_interval
    _QUERY_SCRIPT = [
        "GET_SETTING MEM_MACHINE_STATUS",
        "GET_SETTING MEM_CURRENT_POSITION_X",
        "GET_SETTING MEM_CURRENT_POSITION_Y",
        "GET_SETTING MEM_CURRENT_POSITION_Z",
        "GET_SETTING MEM_CURRENT_POSITION_U",
    ]

    # Commands triggered on MEM_CARD_ID reply
    _BED_SIZE_SCRIPT = [
        "GET_SETTING MEM_BED_SIZE_X",
        "GET_SETTING MEM_BED_SIZE_Y",
    ]

    # Machine status bit name → mask mapping (used by _handle_wait)
    _STATUS_NAME_TO_BIT = {
        "MACHINE_STATUS_MOVING": rdap.MACHINE_STATUS_MOVING[0],
        "MACHINE_STATUS_LAYER_END": rdap.MACHINE_STATUS_LAYER_END[0],
        "MACHINE_STATUS_JOB_RUNNING": rdap.MACHINE_STATUS_JOB_RUNNING[0],
    }

    def __init__(self) -> None:
        """Initialize RdDriver. No session yet — call start() to connect."""
        self._session: RdSession | None = None
        self._script_queue: queue.Queue = queue.Queue()
        self._runner_thread: threading.Thread | None = None
        self._status_listeners: list[Callable] = []
        self._error_listeners: list[Callable[[str], None]] = []
        self._reply_listeners: list[Callable] = []
        self._lock: threading.RLock = threading.RLock()
        self._shutdown: threading.Event = threading.Event()
        self._cancel_flag: bool = False
        self._protect: bool = True
        self._start_udp_host: str = ""
        self._start_usb_device: str = ""
        self._decoded_values: dict[int, Any] = {}
        self._build_status_map()
        self._head_script: list[str] = [
            "REF_POINT_ABSOLUTE",
            "SET_ABSOLUTE",
            "REF_POINT_SET",
            "ENABLE_BLOCK_CUTTING State:OFF",
        ]
        self._tail_script: list[str] = []

    def _build_status_map(self) -> None:
        """Build address resolution maps from _PING_SCRIPT, _QUERY_SCRIPT, _BED_SIZE_SCRIPT.

        Populates:
            _handled_addresses: set[int] — fast membership check for reply filtering
            _address_to_mnemonic: dict[int, str] — for building status-dict keys
            _address_to_bit_keys: dict[int, list[tuple[str, int]]] — maps 0x0400 to status bit descriptors
        """
        from rpascript.interpreter import ScriptParser

        parser = ScriptParser(warning_callback=lambda msg, syn: logging.warning(f"{msg}  |  Syntax: {syn}"))

        self._handled_addresses: set[int] = set()
        self._address_to_mnemonic: dict[int, str] = {}
        # Map 0x0400 to (bit_key_name, bit_mask) for individual status bits
        self._address_to_bit_keys: dict[int, list[tuple[str, int]]] = {}
        self._address_to_bit_keys[0x0400] = [
            ("MACHINE_STATUS_MOVING", rdap.MACHINE_STATUS_MOVING[0]),
            ("MACHINE_STATUS_LAYER_END", rdap.MACHINE_STATUS_LAYER_END[0]),
            ("MACHINE_STATUS_JOB_RUNNING", rdap.MACHINE_STATUS_JOB_RUNNING[0]),
        ]
        self._address_to_spec: dict[int, tuple[str, str, str]] = {}

        scripts = [
            ("_PING_SCRIPT", self._PING_SCRIPT),
            ("_QUERY_SCRIPT", self._QUERY_SCRIPT),
            ("_BED_SIZE_SCRIPT", self._BED_SIZE_SCRIPT),
        ]

        for script_name, script_lines in scripts:
            parsed = parser.parse_lines(script_lines)
            for cmd in parsed:
                if cmd.get("mnemonic") == "GET_SETTING":
                    params = cmd.get("params", [])
                    if params:
                        mnemonic = params[0]
                        mt_entry = parser._mt_map.get(mnemonic)
                        if mt_entry is not None:
                            msb, lsb = mt_entry
                            address = (msb << 8) | lsb
                            self._handled_addresses.add(address)
                            self._address_to_mnemonic[address] = mnemonic
                            self._address_to_spec[address] = mt_entry[1]

    # ---- Driver Lifecycle ----

    def start(self, udp_host: str | None = None, usb_device: str | None = None) -> bool:
        """Start the driver: create session, configure transport, open, start script runner.

        Creates an RdSession, configures transport with the given parameters,
        opens the transport (non-fatal if it fails — status monitor retries),
        then starts the script runner and status monitor.

        Args:
            udp_host: UDP host address or hostname. None reuses previous value.
            usb_device: USB serial device path. None reuses previous value.

        Returns:
            True if transport opened immediately, False if it needs retry.
        """
        if udp_host is None:
            udp_host = self._start_udp_host
        if usb_device is None:
            usb_device = self._start_usb_device

        if self._session is not None:
            if (udp_host and udp_host != self._start_udp_host) or (
                usb_device and usb_device != self._start_usb_device
            ):
                self.stop()
            else:
                return True

        self._session = RdSession()
        self._session.transport.configure(timeout=500, gross_timeout=15000)
        self._start_udp_host = udp_host
        self._start_usb_device = usb_device
        opened = self._session.transport.open(
            udp_host=udp_host,
            usb_device=usb_device,
        )
        self._start_script_runner()
        return opened

    def stop(self) -> None:
        """Stop the driver: stop script runner, disconnect session, clean up.

        Idempotent — safe to call multiple times. Connection parameters
        persist for reuse on next start() call.
        """
        self._stop_script_runner()
        if self._session is not None:
            self._session.disconnect()
            self._session = None

    # ---- Listener Registration ----

    def register_status_listener(
        self, listener: Callable[[RdStatusEvent | StatusDict], None]
    ) -> None:
        """Register a listener for RdStatusEvent notifications. Thread-safe.

        If a session is already connected when the listener registers,
        the current transport type and CONNECTED event are replayed to
        the new listener so it receives up-to-date state.
        """
        with self._lock:
            self._status_listeners.append(listener)
            session = self._session

        # Replay current connection state to late-joining listener
        if session is not None and session.is_connected:
            if session.is_usb:
                transport_event = RdStatusEvent.TRANSPORT_USB
            else:
                transport_event = RdStatusEvent.TRANSPORT_UDP
            for event in (transport_event, RdStatusEvent.CONNECTED):
                try:
                    listener(event)
                except Exception:
                    pass  # Isolate bad callbacks

    def register_error_listener(self, listener: Callable[[str], None]) -> None:
        """Register a listener for error message notifications. Thread-safe."""
        with self._lock:
            self._error_listeners.append(listener)

    def register_reply_listener(self, listener: Callable[[list[str]], None]) -> None:
        """Register a listener for raw reply data notifications. Thread-safe."""
        with self._lock:
            self._reply_listeners.append(listener)

    def unregister_status_listener(
        self, listener: Callable[[RdStatusEvent | StatusDict], None]
    ) -> None:
        """Remove a previously registered status listener. Thread-safe."""
        with self._lock:
            try:
                self._status_listeners.remove(listener)
            except ValueError:
                pass

    def unregister_error_listener(self, listener: Callable[[str], None]) -> None:
        """Remove a previously registered error listener. Thread-safe."""
        with self._lock:
            try:
                self._error_listeners.remove(listener)
            except ValueError:
                pass

    def unregister_reply_listener(self, listener: Callable[[list[str]], None]) -> None:
        """Remove a previously registered reply listener. Thread-safe."""
        with self._lock:
            try:
                self._reply_listeners.remove(listener)
            except ValueError:
                pass

    # ---- Internal Callbacks ----

    def _on_status_event(self, event: RdStatusEvent | StatusDict) -> None:
        """Forward RdStatus events to registered listeners. Thread-safe via copy-on-iterate."""
        with self._lock:
            listeners = list(self._status_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                pass  # Isolate bad callbacks

    @staticmethod
    def _diff_machine_status_bits(
        address: int,
        prev: object,
        new_value: int,
        address_to_bit_keys: dict[int, list[tuple[str, int]]],
    ) -> dict[str, bool]:
        """Compare old and new machine status values and return changed bits as bool dict.

        Returns dict with changed bit names → bool value. Empty dict if address is not 0x0400.
        """
        bit_changes: dict[str, bool] = {}
        if address != 0x0400:
            return bit_changes
        bit_keys = address_to_bit_keys.get(0x0400, [])
        for bit_name, bit_mask in bit_keys:
            if prev is not _UNSET:
                prev_set = bool(prev & bit_mask)
                new_set = bool(new_value & bit_mask)
                if prev_set != new_set:
                    bit_changes[bit_name] = new_set
            else:
                bit_changes[bit_name] = bool(new_value & bit_mask)
        return bit_changes

    @staticmethod
    def _format_status_value(address: int, raw_reply: bytearray) -> str:
        """Format a decoded reply value using the MT table format spec.

        Legacy wrapper — delegates to format_reply_value.
        """
        _, formatted = RdDriver.format_reply_value(address, raw_reply)
        return formatted

    @staticmethod
    def format_reply_value(
        address: int, raw_reply: bytearray
    ) -> tuple[str | None, str]:
        """Decode a reply bytearray using the MT table into (mnemonic, formatted_value).

        Args:
            address: The memory address extracted from the reply header.
            raw_reply: The full reply bytearray (including header bytes).

        Returns:
            Tuple of (mnemonic, formatted_value_string).
            mnemonic is None if the address is not in the MT table.
            formatted_value_string is always a string (fallback on decode failure).
        """
        from protocols.ruida.ruida_protocol import MT, RD_TYPES, RDT_BYTES

        msb = (address >> 8) & 0xFF
        lsb = address & 0xFF
        mt_entry = MT.get(msb, {}).get(lsb)
        if mt_entry is None:
            # Fallback: TBD format (binary, hex, decimal)
            from protocols.ruida.ruida_protocol import TBD
            val = RdDecoder().decode_value(raw_reply)
            return (None, TBD[0].format(val))

        mnemonic = mt_entry[0]
        spec = mt_entry[1]  # (format_string, decoder_fn, raw_type)
        d = RdDecoder()
        d.format = spec[0]
        d.rd_type = spec[2]
        d.data = bytearray([])
        d.value = None
        d.cstring = d.rd_type == "cstring"
        d._length = RD_TYPES.get(d.rd_type, [0, 5])[RDT_BYTES]
        decoder_method = getattr(d, f"rd_{spec[1]}")
        try:
            decoded = decoder_method(raw_reply[4:9])
            return (mnemonic, str(decoded))
        except Exception:
            val = RdDecoder().decode_value(raw_reply)
            return (mnemonic, str(val))

    @staticmethod
    def format_reply(reply: bytearray) -> str:
        """Format a GET_SETTING reply bytearray as a human-readable string.

        Extracts the address from the reply, looks up the MT table,
        decodes the value, and returns a formatted line like:
            "MEM_CARD_ID: 12345"
        or (if address not in MT table):
            "0x057E: 12345"

        Args:
            reply: Raw reply bytearray (min 9 bytes).

        Returns:
            Formatted string suitable for display.
        """
        addr = (reply[2] << 8) | reply[3]
        mnemonic, formatted = RdDriver.format_reply_value(addr, reply)
        if mnemonic:
            return f"{mnemonic}: {formatted}"
        return f"0x{addr:04X}: {formatted}"

    @staticmethod
    def format_reply_list(replies: list[bytearray]) -> list[str]:
        """Format a list of reply bytearrays into human-readable strings.

        Args:
            replies: List of raw reply bytearrays.

        Returns:
            List of formatted strings, one per reply.
        """
        return [RdDriver.format_reply(r) for r in replies]

    def _on_reply(self, replies: list[bytearray]) -> None:
        """Internal reply handler: decode for status tracking, filter handled replies.

        For handled addresses (from _PING_SCRIPT, _QUERY_SCRIPT, _BED_SIZE_SCRIPT):
        - Decode value, compare with previous, build changes dict if changed.
        - Machine status bits are split into individual bool keys.
        - Do NOT forward to reply listeners.

        For non-handled addresses:
        - Format via format_reply_list and forward formatted strings to reply listeners.
        """
        decoder = RdDecoder()
        changes: dict[str, Any] = {}
        forward_replies_raw: list[bytearray] = []

        for raw_reply in replies:
            address = decoder.decode_address(raw_reply)

            if address in self._handled_addresses:
                new_value = decoder.decode_value(raw_reply)
                prev = self._decoded_values.get(address, _UNSET)

                if prev is _UNSET or prev != new_value:
                    mnemonic = self._address_to_mnemonic.get(
                        address, f"0x{address:04X}"
                    )
                    formatted = self._format_status_value(address, raw_reply)
                    changes[mnemonic] = (new_value, formatted)

                    changes.update(
                        self._diff_machine_status_bits(
                            address, prev, new_value, self._address_to_bit_keys
                        )
                    )

                self._decoded_values[address] = new_value

                if address == 0x057E:
                    self.run(self._BED_SIZE_SCRIPT)
            else:
                forward_replies_raw.append(raw_reply)

        if changes:
            self._on_status_event(StatusDict(**changes))

        if forward_replies_raw:
            forward_replies = RdDriver.format_reply_list(forward_replies_raw)
            with self._lock:
                listeners = list(self._reply_listeners)
            for listener in listeners:
                try:
                    listener(forward_replies)
                except Exception:
                    pass

    # ---- Script Runner Lifecycle ----

    def _start_script_runner(self) -> None:
        """Start the background script runner thread and register session listeners.

        Configures RdStatus with ping/query commands, then starts the status monitor.
        Idempotent — no-op if runner is already alive.

        Order is critical:
        1. Configure ping/query commands (harmless before status starts)
        2. Start the runner thread (so self.run() is safe when listeners fire)
        3. Register session listeners (runner is already running)
        4. Start the status monitor LAST (replies arrive to a fully-initialized driver)
        """
        if self._runner_thread and self._runner_thread.is_alive():
            return

        self._shutdown.clear()
        self._cancel_flag = False

        if self._session is None:
            raise RuntimeError("Session not created. Call start() first.")

        # 1. Configure RdStatus with ping/query commands (before starting anything)
        parser = ScriptParser(warning_callback=lambda msg, syn: logging.warning(f"{msg}  |  Syntax: {syn}"))

        ping_parsed = parser.parse_lines(self._PING_SCRIPT)
        ping_binary = encode_command(
            ping_parsed[0], parser.mnemonic_map, parser.mt_map, RdEncoder()
        )
        self._session.status.set_ping_command(ping_binary)

        query_parsed = parser.parse_lines(self._QUERY_SCRIPT)
        query_binary = [
            encode_command(cmd, parser.mnemonic_map, parser.mt_map, RdEncoder())
            for cmd in query_parsed
        ]
        self._session.status.set_query_commands(query_binary)

        # 2. Start the runner thread BEFORE registering listeners,
        #    so self.run() is safe as soon as any listener fires.
        self._runner_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._runner_thread.start()

        # 3. Register session listeners (runner is ready)
        self._session.status.register_status_listener(self._on_status_event)
        self._session.transport.register_reply_listener(self._on_reply)

        # 4. Start the status monitor LAST — from this point, replies can arrive
        #    and will be handled by a fully-initialized driver
        self._session.status.start()

    def _stop_script_runner(self) -> None:
        """Stop the background script runner thread and unregister session listeners.

        Sends shutdown sentinel, joins thread (2s timeout), and unregisters listeners.
        Idempotent — no-op if already stopped.
        """
        if self._runner_thread is None:
            return

        if self._session is None:
            self._runner_thread = None
            return

        self._shutdown.set()
        self._script_queue.put(None)  # Sentinel to unblock get()

        self._runner_thread.join(timeout=2.0)

        # Drain any accumulated scripts to free memory
        while not self._script_queue.empty():
            try:
                self._script_queue.get_nowait()
            except queue.Empty:
                break

        # Clean up listeners
        self._session.status.unregister_status_listener(self._on_status_event)
        self._session.transport.unregister_reply_listener(self._on_reply)

        self._runner_thread = None

    # ---- Background Script Runner ----

    def _run_loop(self) -> None:
        """Background script runner core loop.

        Waits for scripts on the queue, parses them, encodes commands,
        and sends via the session transport. Handles connection guard,
        error recovery, and shutdown.
        """
        encoder = RdEncoder()
        while not self._shutdown.is_set():
            try:
                item = self._script_queue.get()
                if item is None:
                    break  # Sentinel shutdown
                script, auto_checksum = item

                parser = ScriptParser(warning_callback=lambda msg, syn: logging.warning(f"{msg}  |  Syntax: {syn}"))
                parsed = parser.parse_lines(script)
                encoded = []
                file_checksum = 0
                end_job_idx: int | None = (
                    None  # index in `encoded` for the placeholder
                )
                end_job_value = None  # parsed value if present

                for cmd in parsed:
                    if cmd.get("type") == "new_packet":
                        continue
                    if cmd.get("type") == "DELAY":
                        self._handle_delay(cmd)
                        continue
                    if cmd.get("type") == "WAIT":
                        self._handle_wait(cmd)
                        continue
                    if self._protect and cmd.get("mnemonic") == "SET_SETTING":
                        logging.warning(
                            f"SET_SETTING blocked by protect mode "
                            f"(line {cmd.get('line_num', '?')}). "
                            f"Use /protect off to allow."
                        )
                        continue
                    raw = encode_command(
                        cmd, parser.mnemonic_map, parser.mt_map, encoder
                    )
                    if not raw:
                        continue

                    if is_end_job(cmd, parser.mnemonic_map):
                        if (
                            end_job_value is not None
                            or end_job_idx is not None
                        ):
                            raise ValueError(
                                "Duplicate END_JOB — at most one per file"
                            )
                        if cmd["params"]:
                            end_job_value = parse_value(
                                cmd["params"][0], "checksum", "uint_35"
                            )
                        else:
                            # Omitted: extend raw with placeholder bytes for later fill
                            raw.extend(b"\x00" * 5)
                        end_job_idx = len(encoded)
                        encoded.append(raw)
                        # DO NOT include END_JOB bytes in file_checksum
                    elif should_include_in_checksum(cmd, parser.mnemonic_map):
                        file_checksum += sum(raw)
                        encoded.append(raw)
                    else:
                        encoded.append(raw)

                # Post-loop: verify or fill END_JOB
                if end_job_value is not None:
                    if file_checksum != end_job_value:
                        logging.warning(
                            "END_JOB checksum mismatch: "
                            "expected %d, calculated %d — patching",
                            end_job_value,
                            file_checksum,
                        )
                        # Patch the encoded bytes with the correct checksum
                        encoded_sum = encoder.encode_uint35(file_checksum)
                        raw_ej = encoded[end_job_idx]
                        raw_ej[-5:] = encoded_sum
                        end_job_value = file_checksum
                elif end_job_idx is not None:
                    # Fill omitted checksum: encode value, patch the placeholder bytearray
                    encoded_sum = encoder.encode_uint35(file_checksum)
                    raw_ej = encoded[end_job_idx]
                    raw_ej[-5:] = (
                        encoded_sum  # last 5 bytes are the uint35 placeholder
                    )

                if encoded and self._session.is_connected:
                    with self._lock:
                        if self._cancel_flag:
                            self._cancel_flag = False  # Consume even on success
                    self._session.transport.write(encoded)
                elif encoded and not self._session.is_connected:
                    with self._lock:
                        if self._cancel_flag:
                            self._cancel_flag = False
                            continue  # Drop script, don't requeue
                    # Not connected: requeue script for retry, notify via status listener
                    self._script_queue.put((script, auto_checksum))
                    self._notify_script_skipped()
                    # Backoff to break tight cycle when machine is offline.
                    # Without this sleep, the immediately-available requeued item
                    # causes a 100% CPU tight loop allocating/discarding ScriptParser
                    # and encoder objects.  The 100ms yield allows Python's GC to
                    # run and reduces memory pressure.
                    self._shutdown.wait(0.1)
            except Exception as exc:
                # Log error, notify, continue to next script
                self._notify_script_error(str(exc))

    # ---- Script Execution API ----

    def run(self, script: list[str], auto_checksum: bool = False) -> None:
        """Queue a script for background execution.

        Args:
            script: List of rpascript-formatted command lines.
            auto_checksum: If True, auto-calculate END_JOB on mismatch
                with a warning instead of raising.

        Raises:
            RuntimeError: If script runner is not started.
        """
        with self._lock:
            if self._runner_thread is None or not self._runner_thread.is_alive():
                raise RuntimeError(
                    "Script runner not started. Call start() first."
                )
            if not script:
                return  # Empty script is a no-op
            self._script_queue.put((script, auto_checksum))

    def cancel_script(self) -> None:
        """Cancel all queued scripts and prevent current script from requeuing.

        Clears the script queue and sets a flag so the current _run_loop
        iteration skips requeuing the script on disconnect.
        """
        with self._lock:
            while not self._script_queue.empty():
                try:
                    self._script_queue.get_nowait()
                except queue.Empty:
                    break
            self._cancel_flag = True

    def set_protect(self, enabled: bool) -> None:
        """Enable or disable protect mode.

        When enabled, SET_SETTING commands are blocked from being sent
        to the controller to prevent accidental hardware bricking.
        Default: enabled (True).
        """
        self._protect = enabled

    @property
    def protect_enabled(self) -> bool:
        """Return True if protect mode is active (SET_SETTING blocked)."""
        return self._protect

    # ---- Error / Skip Notification ----

    def _notify_script_error(self, message: str) -> None:
        """Notify listeners that a script encountered an encoding/parsing error.

        Iterates snapshot of _status_listeners with try/except per callback.
        Also forwards the error message to registered error listeners.
        """
        with self._lock:
            listeners = list(self._status_listeners)
            error_listeners = list(self._error_listeners)
        for listener in listeners:
            try:
                listener(RdStatusEvent.SCRIPT_ERROR)
            except Exception:
                pass
        for listener in error_listeners:
            try:
                listener(message)
            except Exception:
                pass

    def _notify_script_skipped(self) -> None:
        """Notify listeners that a script was skipped due to disconnect.

        Uses existing DISCONNECTED event — no new RdStatusEvent member needed.
        """
        with self._lock:
            listeners = list(self._status_listeners)
        for listener in listeners:
            try:
                listener(RdStatusEvent.DISCONNECTED)
            except Exception:
                pass

    # ---- Flow-Control Handlers ----

    @staticmethod
    def _parse_timeout(to_str: str) -> float:
        """Parse time spec like '5s' or '5000ms' into seconds (float)."""
        s = to_str.strip()
        # Remove internal whitespace between number and unit
        s = "".join(s.split())
        if s.endswith("ms"):
            seconds = float(s[:-2]) / 1000.0
        elif s.endswith("s"):
            seconds = float(s[:-1])
        else:
            raise ValueError(f"Invalid time format: '{to_str}'. Use e.g., 5s, 500ms")
        if seconds <= 0:
            raise ValueError(f"Timeout must be positive, got '{to_str}'")
        return seconds

    def _resolve_status_bit(self, status_name: str) -> int | None:
        """Resolve a MACHINE_STATUS_* name to its bit mask.

        Only MACHINE_STATUS_* names are supported.
        """
        return self._STATUS_NAME_TO_BIT.get(status_name)

    def _handle_delay(self, cmd: dict) -> None:
        """Handle a DELAY flow-control command: sleep for specified time."""
        params = cmd.get("params", [])
        if not params:
            self._notify_script_error("DELAY requires a time argument")
            return
        try:
            seconds = self._parse_timeout(params[0])
        except ValueError as e:
            self._notify_script_error(str(e))
            return
        # Sleep with shutdown check (interruptible)
        deadline = time.monotonic() + seconds
        while not self._shutdown.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.1))

    def _handle_wait(self, cmd: dict) -> None:
        """Handle a WAIT flow-control command: poll machine status bit.

        Wait for a MACHINE_STATUS_* bit to become active (set), or if
        prefixed with ``!``, wait for the full lifecycle: active then inactive.

        Supports optional to=<timeout> parameter (e.g. '30s', '5000ms').
        """
        params = cmd.get("params", [])
        if not params:
            self._notify_script_error("WAIT requires a status argument")
            return

        status_token = params[0]
        invert = status_token.startswith("!")
        status_name = status_token[1:] if invert else status_token

        bit_mask = self._resolve_status_bit(status_name)
        if bit_mask is None:
            self._notify_script_error(
                f"Unknown machine status: '{status_name}'. "
                f"Use MACHINE_STATUS_MOVING, MACHINE_STATUS_LAYER_END, "
                f"or MACHINE_STATUS_JOB_RUNNING"
            )
            return

        # Parse optional timeout
        timeout = None
        to_str = cmd.get("to")
        if to_str is not None:
            try:
                timeout = self._parse_timeout(to_str)
            except ValueError as e:
                self._notify_script_error(str(e))
                return

        deadline = None if timeout is None else time.monotonic() + timeout

        if invert:
            # Invert mode: wait for bit to become ACTIVE, then INACTIVE
            # First check if already active — if so, skip the 'wait for set' phase
            with self._lock:
                current = self._decoded_values.get(0x0400, 0)
            if not (current & bit_mask):
                # Phase 1: wait for 0→1 transition
                while not self._shutdown.is_set():
                    if deadline and time.monotonic() >= deadline:
                        self._notify_script_error(f"Timeout waiting for {status_token}")
                        return
                    with self._lock:
                        current = self._decoded_values.get(0x0400, 0)
                    if current & bit_mask:
                        break
                    time.sleep(0.05)
            # Phase 2: wait for 1→0 transition
            while not self._shutdown.is_set():
                if deadline and time.monotonic() >= deadline:
                    # Not an error — the job had started and the deadline
                    # applies to the total lifecycle
                    return
                with self._lock:
                    current = self._decoded_values.get(0x0400, 0)
                if not (current & bit_mask):
                    break
                time.sleep(0.05)
        else:
            # Normal mode: wait for bit to become SET
            while not self._shutdown.is_set():
                if deadline and time.monotonic() >= deadline:
                    self._notify_script_error(f"Timeout waiting for {status_token}")
                    return
                with self._lock:
                    current = self._decoded_values.get(0x0400, 0)
                if current & bit_mask:
                    break
                time.sleep(0.05)

    # ---- Properties ----

    @property
    def is_connected(self) -> bool:
        """True if the session exists AND is connected to the controller."""
        return self._session is not None and self._session.is_connected

    @property
    def machine_status(self) -> dict[int, Any]:
        """Current machine status dict (address → decoded value). Read-only snapshot."""
        with self._lock:
            return dict(self._decoded_values)

    # ---- Head / Tail Script Management ----

    def set_head_script(self, script: list[str]) -> None:
        """Set the head script to prepend to every job execution. Thread-safe."""
        with self._lock:
            self._head_script = list(script)

    def set_tail_script(self, script: list[str]) -> None:
        """Set the tail script to append to every job execution. Thread-safe."""
        with self._lock:
            self._tail_script = list(script)

    def get_head_script(self) -> list[str]:
        """Return a copy of the current head script. Thread-safe."""
        with self._lock:
            return list(self._head_script)

    def get_tail_script(self) -> list[str]:
        """Return a copy of the current tail script. Thread-safe."""
        with self._lock:
            return list(self._tail_script)

    def run_job(self, job: list[str], auto_checksum: bool = False) -> None:
        """Queue a job for execution, composing head + job + tail.

        Composes the final script by concatenating any configured head script,
        the job body, and any configured tail script, then queues the result
        via the existing ``run()`` method.

        Args:
            job: List of rpascript-formatted command lines (the job body only).
            auto_checksum: If True, auto-calculate END_JOB on mismatch
                with a warning instead of raising.
        """
        with self._lock:
            composed = list(self._head_script) + list(job) + list(self._tail_script)
        self.run(composed, auto_checksum=auto_checksum)
