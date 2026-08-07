"""
RPyC service for remote TuiAdapter control.

Exposes the full TuiAdapter remote surface as callable methods with
authentication and TLS support:
- lifecycle: start, stop, run, run_job, cancel_script
- head/tail script management: set/get_head_script, set/get_tail_script
- listeners: register_status_listener, register_error_listener,
  register_reply_listener
- properties: is_connected, machine_status
- static format utilities: format_reply_value, format_reply,
  format_reply_list
- GlueScript job authoring, live commands, and getters (40 methods)

Clients call these without the ``exposed_`` prefix
(e.g. svc.declare_job, svc.jog_xy_to, svc.get_gluescript).
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import socket
import threading
import weakref
from typing import Any, Callable

import rpyc
from rpyc.utils.authenticators import AuthenticationError
from rpyc.utils.factory import connect_stream
from rpyc.utils.server import ThreadedServer

from rpascript.tui_adapter import TuiAdapter

_log = logging.getLogger(__name__)


class RpycTuiService(rpyc.Service):
    """RPyC service wrapping TuiAdapter for remote access."""

    def __init__(self, tui_adapter: TuiAdapter | None = None):
        # Keep a strong reference for the fallback (owned) adapter case.
        # When an external adapter is provided, the caller is responsible for
        # keeping it alive. The weakref breaks the reference cycle through
        # `self` (RpycTuiService) → _adapter → TuiAdapter → driver → listeners
        # → wrapper closures → self.
        self._owned_adapter: TuiAdapter | None = None
        if tui_adapter is None:
            self._owned_adapter = TuiAdapter()
            tui_adapter = self._owned_adapter
        self._adapter_ref = weakref.ref(tui_adapter)
        self._lock = threading.Lock()
        self._client_peer = threading.local()
        self._registered_wrappers = threading.local()
        # Shared callback queue — single thread prevents lock contention on RPyC netrefs
        self._callback_queue: queue.Queue = queue.Queue(maxsize=100)
        self._callback_thread = threading.Thread(target=self._callback_loop, daemon=True)
        self._callback_thread.start()

        # Initialize logging state (disabled by default - Early Exit pattern)
        self._logging_enabled = False

    @property
    def _adapter(self) -> TuiAdapter:
        """Resolve the weakref to the TuiAdapter.

        Raises RuntimeError if the adapter has been garbage collected
        (should never happen for owned-adapter or well-managed external-adapter cases).
        """
        adapter = self._adapter_ref()
        if adapter is None:
            raise RuntimeError("RpycTuiService adapter has been garbage collected")
        return adapter

    def _ensure_callback_thread_started(self) -> None:
        """Callback thread is started eagerly in __init__.

        If the daemon thread dies (e.g., due to an unhandled exception),
        callbacks will backlog in the queue until overflow drops them.
        No automatic restart — the service is designed to fail closed.
        """

    def _should_log(self) -> bool:
        """Check if logging should occur based on current state.
        
        Atomic Predictability: Pure function that returns boolean state."""
        return self._logging_enabled

    def enable_logging(self) -> None:
        """Enable verbose RPC logging for debugging."""
        self._logging_enabled = True
        # Log this change since it's a configuration change we want to track
        self._adapter._log_info("[RPC] Logging enabled")

    def disable_logging(self) -> None:
        """Disable verbose RPC logging (default state)."""
        self._logging_enabled = False
        # Log this change since it's a configuration change we want to track
        self._adapter._log_info("[RPC] Logging disabled")

    def logging_enabled(self) -> bool:
        """Return whether verbose RPC logging is currently enabled."""
        return self._should_log()

    def _rpc_info(self, message: str) -> None:
        """Log an RPC line only when verbose RPC logging is enabled."""
        if self._should_log():
            self._adapter._log_info(message)

    def _callback_loop(self) -> None:
        """Process queued callbacks one at a time on a single background thread.

        Uses rpyc.async_() for non-blocking netref calls — sends the request
        and returns immediately. Prevents backpressure on the callback queue
        when the client is slow to serve the connection.

        Terminates if the TuiAdapter has been garbage collected — continuing
        to process callbacks with a dead adapter would cause silent failures.
        """
        while True:
            try:
                listener, arg = self._callback_queue.get(timeout=1.0)
                try:
                    # Non-blocking netref call — fire and forget
                    async_listener = rpyc.async_(listener)
                    async_listener(arg)
                except RuntimeError:
                    # Netref RuntimeError = client disconnected, not adapter-GC.
                    # Log through adapter (best-effort) and continue processing
                    # callbacks for other connected clients.
                    try:
                        self._adapter._log_warning(
                            "[RPC] callback skipped: client disconnected"
                        )
                    except RuntimeError:
                        raise  # Adapter GC'd — terminate
                    continue
                except Exception as e:
                    try:
                        self._adapter._log_warning(f"[RPC] callback: {e}")
                    except RuntimeError:
                        raise
            except queue.Empty:
                continue
            except RuntimeError:
                raise  # Re-raise RuntimeError (adapter GC'd) — let daemon thread die
            except Exception as e:
                try:
                    self._adapter._log_warning(f"[RPC] callback loop: {e}")
                except RuntimeError:
                    raise

    def _fire_async(self, listener: Callable, arg: Any, label: str) -> None:
        """Queue a callback for async delivery on the shared callback thread.

        The callback thread uses rpyc.async_() for truly non-blocking
        netref calls, so queued events are processed rapidly without
        waiting for client responses. Queue overflow drains oldest events.
        """
        self._ensure_callback_thread_started()
        try:
            self._callback_queue.put_nowait((listener, arg))
        except queue.Full:
            # Drop the oldest event to make room for the newest
            try:
                self._callback_queue.get_nowait()
                self._callback_queue.put_nowait((listener, arg))
            except queue.Empty:
                pass  # raced with consumer — event was already processed
            try:
                self._adapter._log_warning(f"[RPC] {label} overflow: oldest callback dropped (queue full)")
            except RuntimeError:
                pass  # Adapter GC'd — nothing actionable

    def on_connect(self, conn):
        """Log when a client connects."""
        try:
            host, port = conn._channel.stream.sock.getpeername()[:2]
        except Exception as exc:
            self._adapter._log_warning(f"RPC client connect - failed to get peer: {exc}")
            host, port = "unknown", 0
        self._client_peer.value = f"{host}:{port}"
        self._rpc_info(f"RPC client connected from {host}:{port}")

    def on_disconnect(self, conn):
        """Clean up per-connection state when a client disconnects.

        Runs cleanup in a background thread with a 5-second timeout to
        prevent blocking the RPyC handler thread if the driver lock is
        contended. Logs wrapper counts before/after for observability.
        """
        peer = getattr(self._client_peer, 'value', 'unknown:0')
        try:
            self._rpc_info(f"RPC client disconnected ({peer})")
        except RuntimeError:
            pass  # Adapter already GC'd — proceed with cleanup anyway

        # Unregister all stored wrappers to prevent stale-callback warnings
        wrappers = self._registered_wrappers

        # Capture listener lists on the handler thread BEFORE submitting to
        # executor — threading.local() attributes are thread-specific and would
        # be invisible from the executor thread.
        captured: dict[str, list] = {}
        for key in ('status', 'error', 'reply'):
            listeners = getattr(wrappers, key, [])
            captured[key] = list(listeners)  # Shallow copy for executor thread
            listeners.clear()  # Clear handler-thread originals immediately

        counts = {k: len(v) for k, v in captured.items()}

        # Early exit if nothing to clean up
        if not counts['status'] and not counts['error'] and not counts['reply']:
            return

        def _do_cleanup():
            for key, unregister_name in [
                ('status', 'unregister_status_listener'),
                ('error', 'unregister_error_listener'),
                ('reply', 'unregister_reply_listener'),
            ]:
                listeners = captured[key]
                if not listeners:
                    continue
                # Resolve adapter fresh for each key — may raise if GC'd
                try:
                    unregister = getattr(self._adapter, unregister_name, None)
                except RuntimeError:
                    unregister = None
                if unregister is None:
                    continue
                for listener in listeners:
                    try:
                        unregister(listener)
                    except Exception as exc:
                        try:
                            self._adapter._log_warning(
                                f"RPC disconnect cleanup ({unregister_name}): {exc}"
                            )
                        except RuntimeError:
                            pass  # Adapter GC'd mid-cleanup — nothing actionable
                        except Exception:
                            pass  # Cleanup path — nothing actionable if logging fails

        # Run cleanup with 5-second timeout
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_do_cleanup)
            try:
                future.result(timeout=5.0)
                try:
                    self._adapter._log_info(
                        f"disconnect cleanup: unregistered "
                        f"{counts['status']} status, "
                        f"{counts['error']} error, "
                        f"{counts['reply']} reply wrappers"
                    )
                except RuntimeError:
                    pass
            except concurrent.futures.TimeoutError:
                try:
                    self._adapter._log_warning(
                        "RPC disconnect cleanup timed out after 5s"
                    )
                except RuntimeError:
                    pass
        finally:
            # Use shutdown(wait=False) to avoid blocking the handler thread
            # on cleanup that hasn't completed within the timeout.
            executor.shutdown(wait=False)

    # --- Lifecycle ---

    def exposed_start(self, udp_host: str | None = None, usb_device: str | None = None) -> bool:
        self._rpc_info(f"[RPC] RPC start(udp_host={udp_host!r}, usb_device={usb_device!r})")
        return self._adapter.start(udp_host=udp_host, usb_device=usb_device)

    def exposed_stop(self) -> None:
        self._rpc_info("[RPC] RPC stop()")
        self._adapter.stop()

    def exposed_run(self, script: list[str], auto_checksum: bool = False) -> Any:
        # Convert netref to local list on the handler thread, where the RPyC
        # connection is alive. RPyC passes lists by reference (netref), not by
        # value — only tuples and simple types are brine-dumpable. Iterating a
        # netref from a background thread or after the handler returns is fragile.
        local_script = list(script)
        self._rpc_info(
            f"[RPC] RPC run(script={len(local_script)} lines, "
            f"auto_checksum={auto_checksum})"
        )
        # The adapter's run_script() internally uses call_from_thread() to
        # bridge to the TUI event loop thread, then calls driver.run() which
        # queues the script and returns quickly. No separate background thread
        # needed — the handler thread blocks briefly via call_from_thread's
        # future.result() and the TUI thread executes the driver call.
        try:
            self._adapter.run(local_script, auto_checksum=auto_checksum)
        except Exception as e:
            # Keep error logging active even when info logging is disabled
            self._adapter._log_error("[RPC] RPC run failed: %s", e)
        return None

    # --- Head / Tail Script Management ---

    def exposed_set_head_script(self, script: list[str]) -> None:
        local_script = list(script)
        self._rpc_info(f"[RPC] RPC set_head_script({len(local_script)} lines)")
        self._adapter.set_head_script(local_script)

    def exposed_set_tail_script(self, script: list[str]) -> None:
        local_script = list(script)
        self._rpc_info(f"[RPC] RPC set_tail_script({len(local_script)} lines)")
        self._adapter.set_tail_script(local_script)

    def exposed_get_head_script(self) -> list[str]:
        result = self._adapter.get_head_script()
        self._rpc_info(f"[RPC] RPC get_head_script -> {len(result)} lines")
        return result

    def exposed_get_tail_script(self) -> list[str]:
        result = self._adapter.get_tail_script()
        self._rpc_info(f"[RPC] RPC get_tail_script -> {len(result)} lines")
        return result

    def exposed_run_job(self, job: list[str] | None = None, auto_checksum: bool = False) -> None:
        """Run a job, composing head + job + tail.

        When ``job`` is omitted (None), the staged rpascript — the script
        most recently staged by ``stage_gluescript()`` — is run instead.
        The adapter logs a RuntimeError from the driver when nothing has
        been staged.

        Args:
            job: List of rpascript-formatted command lines (job body only).
                When None, the staged rpascript is run.
            auto_checksum: If True, auto-calculate END_JOB on mismatch.
        """
        if job is not None:
            local_job = list(job)
            self._rpc_info(f"[RPC] RPC run_job({len(local_job)} lines, auto_checksum={auto_checksum})")
        else:
            local_job = None
            self._rpc_info(f"[RPC] RPC run_job(staged rpascript, auto_checksum={auto_checksum})")
        try:
            self._adapter.run_job(local_job, auto_checksum=auto_checksum)
        except Exception as e:
            # Keep error logging active even when info logging is disabled
            self._adapter._log_error("[RPC] RPC run_job failed: %s", e)

    # --- GlueScript — job authoring (session-less) ---

    def _exposed_gluescript(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Dispatch a GlueScript RPC call to the adapter's delegate layer.

        Converts netref lists to local lists on the handler thread, where
        the RPyC connection is alive (same rationale as ``exposed_run``:
        RPyC passes lists by reference, and iterating a netref from a
        background thread or after the handler returns is fragile). Covers
        the ``comment``/``inline``/``lines`` list args and the ``abs_xy``/
        ``gluescript`` list kwargs.
        """
        self._rpc_info("[RPC] gluescript %s(...)" % name)
        args = tuple(list(a) if isinstance(a, list) else a for a in args)
        kwargs = {
            k: (list(v) if isinstance(v, list) else v)
            for k, v in kwargs.items()
        }
        return getattr(self._adapter, "gluescript_" + name)(*args, **kwargs)

    def exposed_new_gluescript(self) -> None:
        self._rpc_info("[RPC] gluescript new_gluescript()")
        return self._exposed_gluescript("new_gluescript")

    def exposed_comment(self, comments: list[str]) -> None:
        self._rpc_info(f"[RPC] gluescript comment({len(comments)} lines)")
        return self._exposed_gluescript("comment", comments)

    def exposed_inline(self, commands: list[str]) -> None:
        self._rpc_info(f"[RPC] gluescript inline({len(commands)} lines)")
        return self._exposed_gluescript("inline", commands)

    def exposed_declare_job(
        self,
        label: str,
        ref_point: str = "MACHINE",
        abs_xy: list[float] | None = None,
        columns: int = 1,
        rows: int = 1,
        xstep: float = 0.0,
        ystep: float = 0.0,
    ) -> None:
        self._rpc_info(
            f"[RPC] gluescript declare_job({label}, {ref_point}, "
            f"columns={columns}, rows={rows})"
        )
        return self._exposed_gluescript(
            "declare_job", label, ref_point, abs_xy, columns, rows,
            xstep, ystep,
        )

    def exposed_end_job(self) -> None:
        self._rpc_info("[RPC] gluescript end_job()")
        return self._exposed_gluescript("end_job")

    def exposed_declare_layer(
        self,
        label: str,
        color: str,
        mode: str = "VECTOR",
        overscan: str = "NONE",
        speed: float = 100.0,
        frequency: float = 20.0,
        min_power_1: float = 8.0,
        max_power_1: float = 70.0,
    ) -> None:
        self._rpc_info(
            f"[RPC] gluescript declare_layer({label}, {color}, mode={mode}, "
            f"speed={speed})"
        )
        return self._exposed_gluescript(
            "declare_layer", label, color, mode, overscan, speed, frequency,
            min_power_1, max_power_1,
        )

    def exposed_move_xy_to(self, x: float, y: float) -> None:
        self._rpc_info(f"[RPC] gluescript move_xy_to({x}, {y})")
        return self._exposed_gluescript("move_xy_to", x, y)

    def exposed_move_x_to(self, x: float) -> None:
        self._rpc_info(f"[RPC] gluescript move_x_to({x})")
        return self._exposed_gluescript("move_x_to", x)

    def exposed_move_y_to(self, y: float) -> None:
        self._rpc_info(f"[RPC] gluescript move_y_to({y})")
        return self._exposed_gluescript("move_y_to", y)

    def exposed_cut_xy_to(self, x: float, y: float) -> None:
        self._rpc_info(f"[RPC] gluescript cut_xy_to({x}, {y})")
        return self._exposed_gluescript("cut_xy_to", x, y)

    def exposed_cut_x_to(self, x: float) -> None:
        self._rpc_info(f"[RPC] gluescript cut_x_to({x})")
        return self._exposed_gluescript("cut_x_to", x)

    def exposed_cut_y_to(self, y: float) -> None:
        self._rpc_info(f"[RPC] gluescript cut_y_to({y})")
        return self._exposed_gluescript("cut_y_to", y)

    def exposed_power(self, percent: float | None = None) -> None:
        self._rpc_info(f"[RPC] gluescript power({percent})")
        return self._exposed_gluescript("power", percent)

    def exposed_air_assist_on(self) -> None:
        self._rpc_info("[RPC] gluescript air_assist_on()")
        return self._exposed_gluescript("air_assist_on")

    def exposed_air_assist_off(self) -> None:
        self._rpc_info("[RPC] gluescript air_assist_off()")
        return self._exposed_gluescript("air_assist_off")

    def exposed_add_layer_action(self, layer: int, lines: list[str]) -> None:
        self._rpc_info(f"[RPC] gluescript add_layer_action(layer={layer}, {len(lines)} lines)")
        return self._exposed_gluescript("add_layer_action", layer, lines)

    def exposed_update_position(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        u: float | None = None,
    ) -> None:
        self._rpc_info(f"[RPC] gluescript update_position(x={x}, y={y}, z={z}, u={u})")
        return self._exposed_gluescript("update_position", x, y, z, u)

    def exposed_stage_gluescript(
        self,
        gluescript: list[str] | None = None,
        require_complete: bool = True,
    ) -> bool:
        self._rpc_info(
            f"[RPC] gluescript stage_gluescript("
            f"{len(gluescript) if gluescript is not None else 0} lines, "
            f"require_complete={require_complete})"
        )
        return self._exposed_gluescript(
            "stage_gluescript", gluescript, require_complete
        )

    # --- GlueScript — live-only commands (jogs and homing) ---

    def exposed_jog_set_xy_speed(self, speed: float) -> None:
        self._rpc_info(f"[RPC] gluescript jog_set_xy_speed({speed})")
        return self._exposed_gluescript("jog_set_xy_speed", speed)

    def exposed_jog_set_z_speed(self, speed: float) -> None:
        self._rpc_info(f"[RPC] gluescript jog_set_z_speed({speed})")
        return self._exposed_gluescript("jog_set_z_speed", speed)

    def exposed_jog_set_u_speed(self, speed: float) -> None:
        self._rpc_info(f"[RPC] gluescript jog_set_u_speed({speed})")
        return self._exposed_gluescript("jog_set_u_speed", speed)

    def exposed_jog_set_xy_rel(self, delta: float) -> None:
        self._rpc_info(f"[RPC] gluescript jog_set_xy_rel({delta})")
        return self._exposed_gluescript("jog_set_xy_rel", delta)

    def exposed_jog_set_z_rel(self, delta: float) -> None:
        self._rpc_info(f"[RPC] gluescript jog_set_z_rel({delta})")
        return self._exposed_gluescript("jog_set_z_rel", delta)

    def exposed_jog_set_u_rel(self, delta: float) -> None:
        self._rpc_info(f"[RPC] gluescript jog_set_u_rel({delta})")
        return self._exposed_gluescript("jog_set_u_rel", delta)

    def exposed_jog_xy_to(self, x: float, y: float) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_xy_to({x}, {y})")
        return self._exposed_gluescript("jog_xy_to", x, y)

    def exposed_jog_x_to(self, x: float) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_x_to({x})")
        return self._exposed_gluescript("jog_x_to", x)

    def exposed_jog_y_to(self, y: float) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_y_to({y})")
        return self._exposed_gluescript("jog_y_to", y)

    def exposed_jog_z_to(self, z: float) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_z_to({z})")
        return self._exposed_gluescript("jog_z_to", z)

    def exposed_jog_u_to(self, u: float) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_u_to({u})")
        return self._exposed_gluescript("jog_u_to", u)

    def exposed_jog_xy_rel(
        self, x: float | None = None, y: float | None = None
    ) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_xy_rel(x={x}, y={y})")
        return self._exposed_gluescript("jog_xy_rel", x, y)

    def exposed_jog_x_rel(self, x: float | None = None) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_x_rel(x={x})")
        return self._exposed_gluescript("jog_x_rel", x)

    def exposed_jog_y_rel(self, y: float | None = None) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_y_rel(y={y})")
        return self._exposed_gluescript("jog_y_rel", y)

    def exposed_jog_z_rel(self, z: float | None = None) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_z_rel(z={z})")
        return self._exposed_gluescript("jog_z_rel", z)

    def exposed_jog_u_rel(self, u: float | None = None) -> list[str] | None:
        self._rpc_info(f"[RPC] gluescript jog_u_rel(u={u})")
        return self._exposed_gluescript("jog_u_rel", u)

    def exposed_home(self) -> list[str] | None:
        self._rpc_info("[RPC] gluescript home()")
        return self._exposed_gluescript("home")

    def exposed_home_z(self) -> list[str] | None:
        self._rpc_info("[RPC] gluescript home_z()")
        return self._exposed_gluescript("home_z")

    def exposed_home_u(self) -> list[str] | None:
        self._rpc_info("[RPC] gluescript home_u()")
        return self._exposed_gluescript("home_u")

    # --- GlueScript — getters ---

    def exposed_get_gluescript(self) -> list[str]:
        self._rpc_info("[RPC] gluescript get_gluescript()")
        return self._exposed_gluescript("get_gluescript")

    def exposed_get_rpascript(self) -> list[str]:
        self._rpc_info("[RPC] gluescript get_rpascript()")
        return self._exposed_gluescript("get_rpascript")

    def exposed_job_complete(self) -> bool:
        self._rpc_info("[RPC] gluescript job_complete()")
        return self._exposed_gluescript("job_complete")

    # --- Listeners (netref callbacks) ---

    def exposed_register_status_listener(self, listener: Callable) -> None:
        self._rpc_info(f"[RPC] RPC register_status_listener({listener!r})")
        def wrapper(event):
            try:
                # Convert non-serializable types to brine-dumpable forms
                if isinstance(event, str):
                    converted = event     # RdStatusEvent.name → already a str
                elif isinstance(event, dict):
                    converted = dict(event)  # StatusDict → plain dict
                else:
                    converted = event.name   # RdStatusEvent enum → str name
                # Fire on background thread to avoid blocking status monitor
                self._fire_async(listener, converted, "status")
            except Exception as e:
                self._adapter._log_warning(f"[RPC] status callback error: {e}")
        # Store wrapper per-connection for disconnect cleanup
        if not hasattr(self._registered_wrappers, 'status'):
            self._registered_wrappers.status = []
        self._registered_wrappers.status.append(wrapper)
        self._adapter.register_status_listener(wrapper)

    def exposed_register_error_listener(self, listener: Callable) -> None:
        self._rpc_info(f"[RPC] RPC register_error_listener({listener!r})")
        def wrapper(msg):
            try:
                # Fire on background thread to avoid blocking caller
                self._fire_async(listener, msg, "error")
            except Exception as e:
                self._adapter._log_warning(f"[RPC] error callback error: {e}")
        # Store wrapper per-connection for disconnect cleanup
        if not hasattr(self._registered_wrappers, 'error'):
            self._registered_wrappers.error = []
        self._registered_wrappers.error.append(wrapper)
        self._adapter.register_error_listener(wrapper)

    def exposed_register_reply_listener(self, listener: Callable) -> None:
        self._rpc_info(f"[RPC] RPC register_reply_listener({listener!r})")
        def wrapper(replies):
            try:
                # list[str] is not brine-dumpable; tuple[str, ...] is
                converted = tuple(replies)
                # Fire on background thread to avoid blocking caller
                self._fire_async(listener, converted, "reply")
            except Exception as e:
                self._adapter._log_warning(f"[RPC] reply callback error: {e}")
        # Store wrapper per-connection for disconnect cleanup
        if not hasattr(self._registered_wrappers, 'reply'):
            self._registered_wrappers.reply = []
        self._registered_wrappers.reply.append(wrapper)
        self._adapter.register_reply_listener(wrapper)

    def exposed_cancel_script(self) -> None:
        self._rpc_info("[RPC] RPC cancel_script()")
        self._adapter.cancel_script()

    # --- Properties ---

    def exposed_is_connected(self) -> bool:
        result = self._adapter.is_connected
        self._rpc_info(f"[RPC] RPC is_connected -> {result}")
        return result

    def exposed_machine_status(self) -> dict[int, Any]:
        result = self._adapter.machine_status
        self._rpc_info(f"[RPC] RPC machine_status -> {len(result)} items")
        return result

    # --- Static format utilities ---

    def exposed_format_reply_value(self, address: int, raw_reply: bytearray) -> tuple:
        self._rpc_info(
            f"[RPC] RPC format_reply_value(addr=0x{address:04X}, "
            f"raw_len={len(raw_reply)})"
        )
        return TuiAdapter.format_reply_value(address, raw_reply)

    def exposed_format_reply(self, reply: bytearray) -> str:
        self._rpc_info(f"[RPC] RPC format_reply(len={len(reply)})")
        return TuiAdapter.format_reply(reply)

    def exposed_format_reply_list(self, replies: list[bytearray]) -> list[str]:
        self._rpc_info(f"[RPC] RPC format_reply_list(count={len(replies)})")
        return TuiAdapter.format_reply_list(replies)


def start_rpyc_server(
    tui_adapter: TuiAdapter | None = None,
    host: str = "127.0.0.1",
    port: int = 18812,
    cert_path: str | None = None,
    key_path: str | None = None,
    ca_path: str | None = None,
    token: str | None = None,
    auto_start: bool = True,
) -> ThreadedServer:
    """Start the RPyC server.

    Args:
        tui_adapter: Optional TuiAdapter instance. Creates a new one if None.
        host: Bind address (default: 127.0.0.1).
        port: Bind port (default: 18812).
        cert_path: Path to TLS certificate (enables TLS if provided).
        key_path: Path to TLS private key.
        ca_path: Path to CA certificate for client cert verification.
        token: Authentication token. Empty/None allows localhost without token.
        auto_start: Whether to call server.start() immediately (default: True).
                    Set to False to start the server manually later.

    Returns:
        The started ThreadedServer instance.
    """
    service = RpycTuiService(tui_adapter)

    # Build authenticator if token is provided
    authenticator = None
    if token is not None:
        authenticator = _make_authenticator(token)

    # Build TLS configuration if cert is provided
    if cert_path and key_path:
        import ssl

        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(cert_path, keyfile=key_path)
        if ca_path:
            ssl_ctx.load_verify_locations(ca_path)
            ssl_ctx.verify_mode = ssl.CERT_REQUIRED

        server = ThreadedServer(
            service,
            hostname=host,
            port=port,
            ssl_ctx=ssl_ctx,
            authenticator=authenticator,
        )
    else:
        server = ThreadedServer(
            service,
            hostname=host,
            port=port,
            authenticator=authenticator,
        )

    _log.info(
        "RPyC server starting on %s:%s (TLS=%s, auth=%s)",
        host,
        port,
        "yes" if cert_path else "no",
        "yes" if token else "no",
    )
    if auto_start:
        server.start()
    return server


def _make_authenticator(token: str) -> Callable:
    """Create a token authenticator.

    Returns an authenticator function suitable for ThreadedServer.

    Protocol:
    - Client connects TCP socket
    - Client sends: 1 byte length + N bytes token (UTF-8)
    - Server validates with constant-time comparison

    Localhost connections with empty/no token are allowed:
    - No data sent at all (recv returns empty) → allowed for localhost
    - Empty token sent (1-byte length prefix with value 0) → allowed for localhost
    """
    import hmac

    token_bytes = token.encode("utf-8")

    def authenticator(sock: socket.socket) -> tuple[socket.socket, object]:
        """Authenticate a client connection.

        Returns (socket, credentials) on success.
        Raises AuthenticationError on failure.
        """
        peername = sock.getpeername()
        is_local = peername and peername[0] in ("127.0.0.1", "::1", "localhost")

        # Read token length (1 byte)
        raw_len = sock.recv(1)
        if not raw_len:
            if is_local:
                # Localhost with no token — allow
                return sock, {"user": "local", "authenticated": False}
            raise AuthenticationError("No token provided by non-localhost client")

        token_len = raw_len[0]
        client_token = sock.recv(token_len)

        if len(client_token) != token_len:
            raise AuthenticationError("Token truncated")

        # Empty token from localhost is allowed
        if is_local and token_len == 0 and client_token == b"":
            return sock, {"user": "local", "authenticated": False}

        if not hmac.compare_digest(client_token, token_bytes):
            raise AuthenticationError("Invalid token")

        return sock, {"user": "token-auth", "authenticated": True}

    return authenticator
