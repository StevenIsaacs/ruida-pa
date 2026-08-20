"""Client-side batching wrapper around the RPyC service root,
mirroring the RdDriver surface.

``RpcRdDriver`` wraps an RPyC service root and exposes the same
job-scripting and lifecycle-execution surface as the direct RdDriver,
but with local batching so a whole job reaches the server in a few
round trips instead of one per command. It inherits from ``GlueScript``
(``class RpcRdDriver(GlueScript)``), exactly mirroring how the direct
driver does (``class RdDriver(GlueScript)``), so the buffered authoring
methods, validation, and live-command surface are shared with the direct
driver; the RPC-specific batching, drift guard, and close semantics are
layered on top via the ``_on_action_boundary`` and ``_emit_live_lines``
hooks. The constructor opens its own TCP connection (via the module-level
``connect_rpc()`` helper) when no ``svc`` is supplied; a caller-provided
connected root (``connect_stream(...).root``, as in
tests/rpyc_poc/test_auth.py) is still accepted as the first positional
argument, exactly as before.

Token handshake
---------------
``connect_rpc()`` speaks the length-prefixed token protocol implemented
by the server authenticator (rpalib/rpyc_service.py):
- ``token=None``: send NOTHING. This pairs with the default token-less
  server (``start_rpyc_server(token=None)``), which installs no
  authenticator and never reads a handshake.
- ``token`` a non-empty str: send a 1-byte length prefix followed by
  the UTF-8 token bytes. A token longer than 255 UTF-8 bytes raises
  ``ValueError`` — the length prefix is a single byte.
- ``token=""``: send the documented empty-token prefix ``b"\\x00"``,
  accepted as the localhost path by authenticator-enabled servers.
  tests/rpyc_poc/test_auth.py's ``_client_connect`` sends ``b"\\x00"``
  for ``token=None`` — a convention predating this one, deliberately
  unchanged because that file tests the authenticator itself.

Timeout semantics
-----------------
The socket timeout set by ``connect_rpc()`` bounds the TCP connect and
the handshake send; every RPC afterward is bounded by rpyc's
``sync_request_timeout`` config, which the helper merges in as
``timeout``. A socket ``settimeout`` is never consulted by rpyc's poll
path, so it would not bound RPCs.

Close semantics
---------------
``close()`` is idempotent and closes only connections the driver opened
itself (``_owns_connection``); caller-provided roots are left to their
owner. After ``close()`` the ``_svc`` reference is swapped for a
closed-sentinel whose attribute access and calls raise
``RuntimeError("driver closed")``, and ``is_connected`` reads False.
``RuntimeError`` (not ``AttributeError``) is deliberate: ``_flush()``
treats ``AttributeError`` as "old server without the delta method", so
a closed driver must never be mistaken for an old server.

Batching semantics
------------------
- Structural calls are buffered locally and mirrored into ``_transcript``:
  ``declare_job``, ``declare_layer``, ``comment``, ``inline``, ``delay``,
  ``wait`` (until ``end_job()``), and the layer actions (``move_*_to``,
  ``cut_*_to``, ``power``, ``air_assist_*``). Each flush — at
  ``declare_layer`` or ``end_job`` — sends ONLY the newly appended lines
  (the delta) to ``stage_gluescript_delta()``, which replays the suffix
  onto the server's existing state WITHOUT reset (O(Δ) per flush instead
  of O(L·N)).
- ``new_gluescript`` and post-``end_job()`` ``comment``/``inline``/
  ``delay``/``wait`` are forwarded immediately: ``new_gluescript`` resets
  the server so its transcript length returns to 0 (matching
  ``_flushed_count``), and the post-``end_job`` epilogue is the only way
  lines reach the server after the last flush boundary (forwarded
  epilogue lines break ``len(server) == _flushed_count``; ``sync()`` or a
  new ``declare_job()`` restores the invariant).
- ``add_layer_action``, ``update_position``, jogs, and homing stay
  forwarded-only as today.
- Validation now happens at call time, exactly as on the direct driver:
  ``declare_layer`` (mode/overscan/min_power) and ``power_range``
  (layer/min>max/min<8%) raise ``ValueError`` immediately; ``power``
  warns and drops on a wrong layer mode or a ``None`` percentage. The
  ``_job_complete`` fail-fast guards on ``declare_layer`` and
  ``power_range`` (post-``end_job``) are preserved.

Drift guard
-----------
Every flush compares the SHA-256 signature returned by the server with a
locally computed digest of the full transcript; ``get_gluescript()`` is
read back via the getters ONLY when the signatures differ, to identify
the point of drift. A match proves the server's state equals the
client's belief without transferring the transcript back over the wire.
Any mismatch raises ``RuntimeError`` naming the first differing index —
fail fast, fail loud.

Delta contiguity
----------------
``stage_gluescript_delta()`` requires the server transcript length to
equal the client's ``_flushed_count``. When contiguity is broken (e.g.
an external ``stage_gluescript()`` call or mid-job driver recreation on
the server, or a post-``end_job()`` epilogue followed by another flush),
the server raises ``GlueScriptDeltaMismatchError`` and the client falls
back to a full ``stage_gluescript()`` re-stage, which re-baselines
``_flushed_count`` on success. A server without the delta method (older
version) triggers the same full-stage fallback via ``AttributeError``.

Public ``stage_gluescript()`` passthrough
-----------------------------------------
``stage_gluescript()`` forwards the call unchanged (including
``gluescript=None`` for finalizing the current job), returns the SHA-256
signature string from the server, and does not touch the local transcript
or run the drift check.

Getter semantics
----------------
``gluescript`` and ``job_complete`` are now LIVE LOCAL state, consistent
with the direct driver: ``gluescript`` returns the client's buffered
transcript (``_transcript``, including unflushed lines) and
``job_complete`` reads the local ``_job_complete`` flag set by
``end_job()``. ``rpascript`` remains a server snapshot of the last-flushed
assembled rpascript. The retained method aliases ``get_gluescript()`` and
``get_rpascript()`` return the server's last-flushed state (so the
``gluescript`` property and ``get_gluescript()`` diverge: the property is
live local, the method is the server snapshot). ``sync()`` re-baselines
the transcript, ``_flushed_count``, ``_current_layer_mode`` and
``_job_complete`` from that last-flushed state.

The attribute forms (``gluescript``, ``rpascript``, ``job_complete``,
``machine_status``, ``protect_enabled``, ``is_connected``), the
lifecycle/execution passthroughs (``start``, ``stop``, ``run``,
``run_job``, ``cancel_script``, ``set_protect``, and the head/tail
script setters and getters), the listener registration surface
(``register_status_listener``, ``register_error_listener``,
``register_reply_listener`` and their ``unregister_*`` counterparts),
the format utilities (``format_reply_value``, ``format_reply``,
``format_reply_list``, ``decode_status_value``), and the staging
passthroughs (``stage_gluescript``, ``stage_gluescript_delta``) mirror
the direct RdDriver surface, so an app adapter needs no separate
direct-vs-RPC path. The z/u move and cut stubs (``move_z_to``,
``move_u_to``, ``cut_z_to``, ``cut_u_to``) raise ``NotImplementedError``
exactly as the direct driver does.
"""

from __future__ import annotations

import ast
import logging
import socket
import struct
from typing import Any, Callable

from rpyc.core.stream import SocketStream
from rpyc.utils.factory import connect_stream

from rpalib.gluescript_signature import (
    GlueScriptDeltaMismatchError,
    gluescript_signature,
)
from ruidadriver.rd_gluescript import GlueScript

logger = logging.getLogger(__name__)


def connect_rpc(
    host: str = "127.0.0.1",
    port: int = 18812,
    token: str | None = None,
    config: dict[str, Any] | None = None,
    timeout: float = 5,
) -> Any:
    """Open a TCP connection to an RPyC server and return the connection.

    The caller owns the returned connection and must close it;
    ``RpcRdDriver`` does so when constructed without an explicit
    ``svc``.

    Args:
        host: Server hostname.
        port: Server TCP port.
        token: Authentication token. ``None`` sends no handshake (the
            token-less server default); a non-empty str sends a 1-byte
            length prefix plus the UTF-8 bytes; ``""`` sends the
            documented empty-token ``b"\\x00"`` prefix accepted on
            localhost by authenticator-enabled servers. Tokens longer
            than 255 UTF-8 bytes raise ``ValueError`` (single-byte
            prefix).
        config: Extra rpyc client config merged over the defaults
            (``import_custom_exceptions`` and
            ``instantiate_custom_exceptions`` so a server-raised
            ``GlueScriptDeltaMismatchError`` round-trips instead of
            degrading to ``GenericException``). The
            ``sync_request_timeout`` key is always forced to
            ``timeout``.
        timeout: Bounds the TCP connect and the handshake send (the
            socket timeout), and is merged into rpyc's
            ``sync_request_timeout`` — the only real bound on every
            RPC, since rpyc's poll path never consults a socket
            ``settimeout``.

    Returns:
        The connected ``rpyc.core.protocol.Connection``; its ``.root``
        is the service root netref.

    Raises:
        ValueError: If ``token`` is longer than 255 UTF-8 bytes.
        The connect/handshake/GETROOT error otherwise, with the socket
        closed before re-raising.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if token is not None:
            token_bytes = token.encode()
            if len(token_bytes) > 255:
                raise ValueError("token longer than 255 UTF-8 bytes")
            sock.sendall(struct.pack("B", len(token_bytes)) + token_bytes)
        # RPyC's poll path ignores socket timeouts, so the stream needs
        # a fully blocking socket.
        sock.setblocking(True)
        cfg = {
            "import_custom_exceptions": True,
            "instantiate_custom_exceptions": True,
        }
        if config is not None:
            cfg.update(config)
        cfg["sync_request_timeout"] = timeout
        conn = connect_stream(SocketStream(sock), config=cfg)
        # Force GETROOT inside the guard so any failure closes the
        # socket explicitly instead of relying on refcount cleanup.
        conn.root
        return conn
    except BaseException:
        sock.close()
        raise


class _ClosedService:
    """Sentinel swapped into ``RpcRdDriver._svc`` by ``close()``.

    Attribute access and calls raise ``RuntimeError("driver closed")``.
    ``RuntimeError``, not ``AttributeError``, is deliberate: the
    ``_flush()`` fallback treats ``AttributeError`` as "old server
    without the delta method", so a closed driver must never look like
    an old server.
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("driver closed")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("driver closed")


class RpcRdDriver(GlueScript):
    """Client-side batching wrapper for the RPyC service root,
    mirroring the RdDriver surface.

    Inherits from ``GlueScript`` (as ``RdDriver`` does) so the buffered
    authoring methods, validation, and live-command surface are shared
    with the direct driver. The RPC-specific batching, drift guard, and
    close semantics are layered on via the ``_on_action_boundary`` and
    ``_emit_live_lines`` hooks; see the module docstring for details.
    """

    _svc: Any
    _conn: Any
    _owns_connection: bool
    _closed: bool
    _transcript: list[str]
    _flushed_count: int
    _rpascript_local: list[str]

    def __init__(
        self,
        svc: Any = None,
        *,
        host: str = "127.0.0.1",
        port: int = 18812,
        token: str | None = None,
        config: dict[str, Any] | None = None,
        timeout: float = 5,
    ) -> None:
        """Wrap a service root, connecting to the server when none is given.

        Args:
            svc: The already-connected service root (``conn.root``)
                exposing the GlueScript methods without the ``exposed_``
                prefix. When omitted, the driver opens and owns its own
                connection via ``connect_rpc()``.
            host: Server host for the self-connecting path.
            port: Server port for the self-connecting path.
            token: Auth token for the self-connecting path; see
                ``connect_rpc()`` for the None vs str vs "" modes.
            config: Extra rpyc client config for the self-connecting
                path, merged over the ``connect_rpc()`` defaults.
            timeout: Connect timeout and rpyc ``sync_request_timeout``
                for the self-connecting path.
        """
        # Initialize the RPC attribute surface BEFORE any connect work
        # so a connect failure leaves close()/__del__ safe.
        self._svc: Any = None
        self._conn: Any = None
        self._owns_connection: bool = False
        self._closed: bool = False
        self._transcript: list[str] = []
        self._flushed_count: int = 0
        if svc is not None:
            self._svc = svc
        else:
            conn = connect_rpc(
                host=host, port=port, token=token, config=config,
                timeout=timeout,
            )
            self._conn = conn
            self._svc = conn.root
            self._owns_connection = True
        # Initialize the GlueScript base state AFTER _svc is assigned so
        # the _build_command_registry assertion runs against the real
        # subclass and any registry-bound method that touches _svc is safe.
        super().__init__()

    def close(self) -> None:
        """Close the owned connection and mark the driver closed.

        Idempotent: a second call is a no-op. Only a connection the
        driver opened itself (``_owns_connection``) is closed; a
        caller-provided ``svc`` is left to its owner. Afterwards
        ``_svc`` is the ``_ClosedService`` sentinel, so any member
        access raises ``RuntimeError("driver closed")`` and
        ``is_connected`` reads False. The buffered authoring methods
        (``comment``, ``inline``, ``delay``, ``wait``, the ``move_*_to``
        and ``cut_*_to`` actions, ``power``, ``air_assist_*``) carry
        the same guard, so post-close buffered calls fail fast instead
        of silently buffering lines that never reach a server.
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_connection and self._conn is not None:
            try:
                # Connection.close() performs a synchronous HANDLE_CLOSE
                # bounded by sync_request_timeout; swallow any teardown
                # error — the socket is released regardless.
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._svc = _ClosedService()

    def __del__(self) -> None:
        """Best-effort cleanup at interpreter shutdown.

        close() is safe here because all failure modes are swallowed;
        rpyc modules may already be partially torn down (mirrors
        rpyc's own netref __del__ pattern).
        """
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _flush(self, require_complete: bool) -> None:
        """Stage the buffered transcript and verify server parity by signature.

        Sends only the delta — the transcript lines appended since the
        last flush — to ``stage_gluescript_delta()``, which replays the
        suffix onto the server's existing state without reset. Falls back
        to a full ``stage_gluescript()`` re-stage when the server lacks
        the delta method (``AttributeError``, older server) or the delta
        guard rejects the suffix (``GlueScriptDeltaMismatchError``,
        contiguity broken); the fallback re-baselines ``_flushed_count``
        on success. Compares the server's SHA-256 signature with a
        locally computed one; only when the signatures differ is the
        server transcript read back via ``get_gluescript()`` to locate
        the drift; any difference raises a descriptive ``RuntimeError``.

        Args:
            require_complete: Passed through to the staging call.

        Returns:
            None: The staged rpascript is available via the server's
            ``get_rpascript()`` getter; the staged gluescript via
            ``get_gluescript()``.

        Raises:
            RuntimeError: If the server transcript drifts from the local
                one, or the replayed delta fails validation.
        """
        delta = self._transcript[self._flushed_count:]
        try:
            server_sig = self._svc.stage_gluescript_delta(
                self._flushed_count, delta, require_complete=require_complete
            )
        except AttributeError:
            # Old server without the delta method — full-stage fallback.
            server_sig = self._svc.stage_gluescript(
                list(self._transcript), require_complete=require_complete
            )
        except GlueScriptDeltaMismatchError:
            # Contiguity broken (e.g. mid-job driver recreation, external
            # stage_gluescript call, sync) — full re-stage re-baselines.
            server_sig = self._svc.stage_gluescript(
                list(self._transcript), require_complete=require_complete
            )
        local_sig = gluescript_signature(self._transcript)
        if server_sig == local_sig:
            self._flushed_count = len(self._transcript)
            return
        server_gs = list(self._svc.get_gluescript())
        if server_gs != self._transcript:
            for index, (client_line, server_line) in enumerate(
                zip(self._transcript, server_gs)
            ):
                if client_line != server_line:
                    raise RuntimeError(
                        f"Client/server transcript drift at index {index}: "
                        f"client {client_line!r} != server {server_line!r}"
                    )
            raise RuntimeError(
                f"Client/server transcript drift: client has "
                f"{len(self._transcript)} lines but server has "
                f"{len(server_gs)} lines"
            )
        # Signatures disagreed but the read-back transcript equals the
        # local one (non-conforming server, e.g. an old bool-returning
        # one) — the server state is provably in sync, so re-baseline
        # here instead of falling through with a stale count.
        self._flushed_count = len(self._transcript)

    def sync(self) -> None:
        """Re-baseline local state from the server's last-flushed state.

        Re-baselines the transcript, ``_flushed_count``,
        ``_current_layer_mode`` and ``_job_complete`` from server state.
        ``_current_layer_mode`` is derived from the last
        ``declare_layer(...)`` line in the transcript (defaulting to
        "VECTOR" when no layer was declared). ``_job_declared`` is NOT
        refreshed because it cannot be reliably derived from the server
        transcript (and the buffered value is client-authoritative for
        unflushed state).
        """
        self._transcript = list(self._svc.get_gluescript())
        self._flushed_count = len(self._transcript)
        # Derive the current layer mode from the LAST declare_layer line.
        # Labels and colors are repr-quoted and may contain commas, so the
        # argument list is parsed with ``ast.literal_eval`` (the driver's
        # own gluescript parse boundary) rather than a naive split.
        mode = "VECTOR"
        for line in self._transcript:
            stripped = line.strip()
            if not stripped.startswith("declare_layer("):
                continue
            args_str = stripped[len("declare_layer("):].rstrip(")")
            try:
                args = ast.literal_eval(f"({args_str})")
            except (ValueError, SyntaxError):
                continue
            if len(args) >= 3:
                mode = args[2]
        self._current_layer_mode = mode
        self._job_complete = bool(self._svc.job_complete())

    # ------------------------------------------------------------------ #
    #  Job authoring — forwarded, mirrored into the local transcript
    # ------------------------------------------------------------------ #

    def new_gluescript(self) -> None:
        """Reset all script data for a new job (forwarded immediately).

        Resets the local GlueScript state via ``super()`` (which clears
        the transcript, ``_job_declared`` and ``_job_complete``), re-zeros
        ``_flushed_count``, then forwards the reset to the server so its
        transcript length returns to 0 (matching ``_flushed_count``).
        Note the failure ordering: the local reset happens BEFORE the
        forwarded server reset, so a server-side failure leaves the local
        state already cleared (the previous job's lines are gone).
        """
        super().new_gluescript()
        self._flushed_count = 0
        self._svc.new_gluescript()

    def comment(self, comments: list[str]) -> None:
        """Append comment lines (mirrored; forwarded only after end_job).

        Before ``end_job()`` the line is mirrored into the local
        transcript (via the base method) and reaches the server with the
        next boundary flush. After ``end_job()`` no flush boundary exists,
        so the epilogue is forwarded immediately — the only way
        post-``end_job`` lines reach the server.

        An empty list is a no-op, mirroring the driver.
        """
        if not comments:
            return
        if self._job_complete:
            self._svc.comment(comments)
        super().comment(comments)

    def inline(self, commands: list[str]) -> None:
        """Append raw rpascript commands (mirrored; forwarded after end_job).

        Before ``end_job()`` each command is mirrored into the local
        transcript (via the base method) and replayed on the server with
        the next boundary flush. After ``end_job()`` no flush boundary
        exists, so the epilogue is forwarded immediately — the only way
        post-``end_job`` lines reach the server.

        An empty list is a no-op, mirroring the driver.
        """
        if not commands:
            return
        if self._job_complete:
            self._svc.inline(commands)
        super().inline(commands)

    def delay(self, time: str | int | float) -> None:
        """Append a runner-directive DELAY (mirrored; forwarded after end_job).

        Emits a runner-directive DELAY line: the runner sleeps for the
        given time inline during script execution — the command is never
        encoded or sent to the controller. Unlike jog/home live commands,
        DELAY is part of a saved job and is replayed from a persisted
        gluescript.

        Before ``end_job()`` the line is mirrored into the local
        transcript (via the base method) and reaches the server with the
        next boundary flush. After ``end_job()`` no flush boundary exists,
        so the epilogue is forwarded immediately — the only way
        post-``end_job`` lines reach the server.

        Invalid values are rejected by the base method (via the shared
        ``is_valid_time_value`` predicate) with a warn-and-no-op, so the
        mirrored line stays byte-identical with what the server appends;
        otherwise the SHA-256 drift check would break.
        """
        if self._job_complete:
            self._svc.delay(time)
        super().delay(time)

    def wait(self, status: str, to: str | int | float | None = None) -> None:
        """Append a runner-directive WAIT (mirrored; forwarded after end_job).

        Emits a runner-directive WAIT line: the runner polls the live
        machine status during script execution and blocks until the
        status matches — the command is never encoded or sent to the
        controller. Unlike jog/home live commands, WAIT is part of a
        saved job and is replayed from a persisted gluescript.

        ``status`` is a MACHINE_STATUS_* name passed through verbatim; a
        leading '!' waits for the full active→inactive lifecycle. The
        name is validated at run time by the runner, not here. The
        optional ``to=`` timeout accepts numeric seconds or a
        unit-suffixed string.

        Before ``end_job()`` the line is mirrored into the local
        transcript (via the base method) and reaches the server with the
        next boundary flush. After ``end_job()`` no flush boundary exists,
        so the epilogue is forwarded immediately — the only way
        post-``end_job`` lines reach the server.

        Invalid values are rejected by the base method (via the shared
        ``is_valid_time_value`` predicate) with a warn-and-no-op, so the
        mirrored line stays byte-identical with what the server appends;
        otherwise the SHA-256 drift check would break.
        """
        if self._job_complete:
            self._svc.wait(status, to)
        super().wait(status, to)

    def declare_layer(
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
        """Declare a new layer (mirrored; applied at the boundary flush).

        Delegates to the base method, which validates mode/overscan and
        the power range at call time (``ValueError``), sets
        ``_current_layer_mode``, mirrors the line into the local
        transcript, then fires ``_on_action_boundary()`` — which flushes
        the accumulated delta with ``require_complete=False`` (the job may
        still be in progress).

        Raises:
            RuntimeError: If declare_layer() is called after end_job().
            ValueError: If mode or overscan is invalid, or min_power_1 < 8.
        """
        if self._job_complete:
            raise RuntimeError("declare_layer() called after end_job()")
        super().declare_layer(
            label, color, mode, overscan, speed, frequency,
            min_power_1, max_power_1,
        )

    # ------------------------------------------------------------------ #
    #  Forwarded only — the driver appends no gluescript line
    # ------------------------------------------------------------------ #

    def add_layer_action(self, layer: int, lines: list[str]) -> None:
        """Add raw rpascript lines to a layer action list (forwarded only)."""
        self._svc.add_layer_action(layer, lines)

    def update_position(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        u: float | None = None,
    ) -> None:
        """Sync tracked position from controller replies (forwarded only)."""
        self._svc.update_position(x, y, z, u)

    # ------------------------------------------------------------------ #
    #  Buffered layer actions — flushed at declare_layer/end_job
    #
    #  The move/cut methods (move_xy_to/x_to/y_to, cut_xy_to/x_to/y_to),
    #  power, air_assist_on/off, cut_speed, move_speed, frequency, pwm and
    #  select_laser are INHERITED from GlueScript: they append the plain
    #  absolute-form transcript line via the ``gluescript`` property (the
    #  client mirror) and are flushed at the next boundary. The z/u stubs
    #  (move_z_to/move_u_to/cut_z_to/cut_u_to) inherit NotImplementedError.
    # ------------------------------------------------------------------ #

    def power_range(
        self, min: float | None = None, max: float | None = None
    ) -> None:
        """Buffer a min/max power ramp range (flushed at next boundary).

        Delegates to the base method, which validates at call time
        (``ValueError`` for no declared layer, min > max, or min < 8%;
        a warning for max > 70%) and mirrors the line into the local
        transcript, preserving ``None`` defaults verbatim (the server
        resolves them from the layer's declared powers when the delta is
        staged).

        Raises:
            RuntimeError: If the job is complete — after end_job() no
                flush boundary remains, so fail fast instead of silently
                dropping the action.
            ValueError: If no layer is declared, min > max, or min < 8%.
        """
        # Deliberate fail-fast asymmetry: the sibling buffered actions
        # (move_xy_to / cut_xy_to / air_assist_on) silently buffer forever
        # after end_job() with no flush boundary left to deliver them.
        # power_range() guards _job_complete so a post-end_job call cannot
        # be dropped silently — do not "fix" this by removing the guard.
        if self._job_complete:
            raise RuntimeError(
                "power_range() called after end_job() — no flush boundary "
                "remains to stage it"
            )
        super().power_range(min, max)

    # ------------------------------------------------------------------ #
    #  Staging passthrough and getters — last-flushed server state only
    # ------------------------------------------------------------------ #

    def stage_gluescript(
        self,
        gluescript: list[str] | None = None,
        require_complete: bool = True,
    ) -> str:
        """Public passthrough to the server's stage_gluescript().

        Forwarded unchanged (including ``gluescript=None``); no local flush
        and no drift check.

        Returns:
            str: The SHA-256 signature (hex) of the staged gluescript
                transcript, as returned by the server.
        """
        return self._svc.stage_gluescript(gluescript, require_complete)

    def stage_gluescript_delta(
        self,
        flushed_count: int,
        delta_lines: list[str],
        require_complete: bool = True,
    ) -> str:
        """Forward a delta-stage to the server's stage_gluescript_delta().

        Mirrors the direct driver's ``stage_gluescript_delta()``; the
        client's internal ``_flush()`` is the usual caller.

        Returns:
            str: The SHA-256 signature (hex) of the staged gluescript
                transcript, as returned by the server.
        """
        return self._svc.stage_gluescript_delta(
            flushed_count, delta_lines, require_complete=require_complete
        )

    def get_gluescript(self) -> list[str]:
        """Return the server's gluescript transcript (last-flushed state).

        Unlike the ``gluescript`` property (which is the client's live
        local transcript), this method returns the server's last-flushed
        snapshot. Mirrors the direct driver's ``gluescript`` attribute.
        """
        return list(self._svc.get_gluescript())

    def get_rpascript(self) -> list[str]:
        """Return the server's assembled rpascript (last-flushed state).

        Method alias for the ``rpascript`` property; both return the
        server's last-flushed snapshot. Mirrors the direct driver's
        ``rpascript`` attribute.
        """
        return list(self._svc.get_rpascript())

    @property
    def gluescript(self) -> list[str]:
        """The client's live local gluescript transcript.

        Returns ``_transcript`` — the buffered transcript including
        unflushed lines — consistent with the direct driver's live list.
        Raises ``RuntimeError("driver closed")`` once the driver is
        closed; this is the single chokepoint that guards every buffered
        authoring method inherited from ``GlueScript`` (they append via
        this property). The setter absorbs the base class's
        ``self.gluescript = []`` reassignment in ``new_gluescript()``.
        """
        if self._closed:
            raise RuntimeError("driver closed")
        return self._transcript

    @gluescript.setter
    def gluescript(self, value: list[str]) -> None:
        self._transcript = value

    @property
    def rpascript(self) -> list[str]:
        """Server-side assembled rpascript (last-flushed state).

        Returns a snapshot of the server's assembled rpascript. The
        setter stores to an unused backing field so the base class's
        ``self.rpascript = []`` reassignment in ``new_gluescript()`` is
        absorbed without touching the server.
        """
        return list(self._svc.get_rpascript())

    @rpascript.setter
    def rpascript(self, value: list[str]) -> None:
        self._rpascript_local = value

    @property
    def machine_status(self) -> dict[int, Any]:
        """Server-side decoded machine-status dict (read-only snapshot).

        Returns a local dict copy so the netref never leaks to callers.
        Mirrors the direct driver's ``machine_status`` attribute.
        """
        return dict(self._svc.machine_status())

    @property
    def is_connected(self) -> bool:
        """True when the server-side session is connected.

        Reads False once the driver is closed, without hitting the
        closed sentinel. Mirrors the direct driver's ``is_connected``
        attribute.
        """
        if self._closed:
            return False
        return bool(self._svc.is_connected())

    # ------------------------------------------------------------------ #
    #  Lifecycle and execution — forwarded passthroughs
    # ------------------------------------------------------------------ #

    def start(
        self,
        udp_host: str | None = None,
        usb_device: str | None = None,
        magic: int | None = None,
    ) -> bool:
        """Start the server-side driver/session. Mirrors RdDriver.start()."""
        return bool(
            self._svc.start(
                udp_host=udp_host, usb_device=usb_device, magic=magic
            )
        )

    def stop(self) -> None:
        """Stop the server-side session. Mirrors RdDriver.stop()."""
        self._svc.stop()

    def run(self, script: list[str], auto_checksum: bool = False) -> None:
        """Queue an rpascript script on the server-side driver."""
        self._svc.run(script, auto_checksum=auto_checksum)

    def run_job(
        self, job: list[str] | None = None, auto_checksum: bool = False
    ) -> None:
        """Run a job on the server, composing head + job + tail."""
        self._svc.run_job(job, auto_checksum=auto_checksum)

    def set_head_script(self, script: list[str]) -> None:
        """Set the server-side head script. Mirrors RdDriver."""
        self._svc.set_head_script(script)

    def get_head_script(self) -> list[str]:
        """Return the server-side head script. Mirrors RdDriver."""
        return list(self._svc.get_head_script())

    def set_tail_script(self, script: list[str]) -> None:
        """Set the server-side tail script. Mirrors RdDriver."""
        self._svc.set_tail_script(script)

    def get_tail_script(self) -> list[str]:
        """Return the server-side tail script. Mirrors RdDriver."""
        return list(self._svc.get_tail_script())

    # ------------------------------------------------------------------ #
    #  Listeners, cancel, protect — forwarded passthroughs
    # ------------------------------------------------------------------ #

    def register_status_listener(self, listener: Callable) -> None:
        """Register a status listener on the server-side driver.

        The listener travels over RPC as a netref callback; the server
        converts events to brine-dumpable forms before firing.
        """
        self._svc.register_status_listener(listener)

    def unregister_status_listener(self, listener: Callable) -> None:
        """Remove a previously registered status listener.

        Listeners are matched by equality over RPC; unregistering with a
        different-but-equal callable object may not match. Pass the SAME
        listener object you registered.
        """
        self._svc.unregister_status_listener(listener)

    def register_error_listener(self, listener: Callable) -> None:
        """Register an error listener on the server-side driver."""
        self._svc.register_error_listener(listener)

    def unregister_error_listener(self, listener: Callable) -> None:
        """Remove a previously registered error listener.

        Listeners are matched by equality over RPC; unregistering with a
        different-but-equal callable object may not match. Pass the SAME
        listener object you registered.
        """
        self._svc.unregister_error_listener(listener)

    def register_reply_listener(self, listener: Callable) -> None:
        """Register a raw-reply listener on the server-side driver."""
        self._svc.register_reply_listener(listener)

    def unregister_reply_listener(self, listener: Callable) -> None:
        """Remove a previously registered reply listener.

        Listeners are matched by equality over RPC; unregistering with a
        different-but-equal callable object may not match. Pass the SAME
        listener object you registered.
        """
        self._svc.unregister_reply_listener(listener)

    def cancel_script(self) -> None:
        """Cancel queued scripts on the server-side driver.

        Mirrors RdDriver.cancel_script().
        """
        self._svc.cancel_script()

    def set_protect(self, enabled: bool) -> None:
        """Enable or disable protect mode on the server-side driver.

        Mirrors RdDriver.set_protect().
        """
        self._svc.set_protect(enabled)

    @property
    def protect_enabled(self) -> bool:
        """True when protect mode is active on the server-side driver.

        Mirrors the direct driver's ``protect_enabled`` attribute.
        """
        return bool(self._svc.protect_enabled())

    # ------------------------------------------------------------------ #
    #  GlueScript hooks — batching boundary and live-line forwarding
    # ------------------------------------------------------------------ #

    def _on_action_boundary(self) -> None:
        """Flush the buffered transcript at a declare_layer/end_job boundary.

        Called by the base ``declare_layer()`` and ``end_job()`` at the
        end of each. ``end_job()`` sets ``_job_complete`` True BEFORE this
        callback, so ``require_complete`` is True on the final flush and
        False on each layer boundary.
        """
        self._flush(require_complete=self._job_complete)

    # Exact line tuples produced by the base job-control methods, mapped
    # to the server method that performs the live action. The base
    # pause/resume/stop_job/reset methods call ``_emit_live_lines`` with
    # these exact lists; forwarding to the server's method (rather than
    # queueing the lines) lets the server's RdDriver generate, send, and
    # track the action consistently.
    _LIVE_LINE_MAP: dict[tuple[str, ...], str] = {
        ("PAUSE_JOB",): "pause",
        ("RESUME_JOB",): "resume",
        ("STOP_JOB",): "stop_job",
        ("STOP_JOB", "HOME_XY"): "reset",
    }

    def _emit_live_lines(self, lines: list[str]) -> list[str] | None:
        """Forward live (job-control) lines to the server.

        Overrides the base hook so the inherited job-control methods
        (``pause``/``resume``/``stop_job``/``reset``) reach the server.
        Known line tuples are dispatched to the matching server method;
        anything else falls back to queueing the lines as a script. The
        fallback is effectively unreachable — the base only calls
        ``_emit_live_lines`` with job-control/home lines, and
        ``home``/``home_z``/``home_u`` are overridden to forward directly.
        """
        if self._closed:
            raise RuntimeError("driver closed")
        method_name = self._LIVE_LINE_MAP.get(tuple(lines))
        if method_name is not None:
            return getattr(self._svc, method_name)()
        return self._svc.run(list(lines))

    # ------------------------------------------------------------------ #
    #  Live-only commands — jogs, homing, job control, config setters (forwarded)
    # ------------------------------------------------------------------ #

    def jog_set_xy_speed(self, speed: float) -> None:
        """Set XY jog speed in mm/s (forwarded immediately)."""
        self._svc.jog_set_xy_speed(speed)

    def jog_set_z_speed(self, speed: float) -> None:
        """Set Z jog speed in mm/s (forwarded immediately)."""
        self._svc.jog_set_z_speed(speed)

    def jog_set_u_speed(self, speed: float) -> None:
        """Set U jog speed in mm/s (forwarded immediately)."""
        self._svc.jog_set_u_speed(speed)

    def jog_set_xy_rel(self, delta: float) -> None:
        """Set relative XY jog distance in mm (forwarded immediately)."""
        self._svc.jog_set_xy_rel(delta)

    def jog_set_z_rel(self, delta: float) -> None:
        """Set relative Z jog distance in mm (forwarded immediately)."""
        self._svc.jog_set_z_rel(delta)

    def jog_set_u_rel(self, delta: float) -> None:
        """Set relative U jog distance in mm (forwarded immediately)."""
        self._svc.jog_set_u_rel(delta)

    def jog_xy_to(self, x: float, y: float) -> list[str] | None:
        """Jog XY to absolute coordinates (forwarded immediately)."""
        return self._svc.jog_xy_to(x, y)

    def jog_x_to(self, x: float) -> list[str] | None:
        """Jog X to absolute coordinate (forwarded immediately)."""
        return self._svc.jog_x_to(x)

    def jog_y_to(self, y: float) -> list[str] | None:
        """Jog Y to absolute coordinate (forwarded immediately)."""
        return self._svc.jog_y_to(y)

    def jog_z_to(self, z: float) -> list[str] | None:
        """Jog Z to absolute coordinate (forwarded immediately)."""
        return self._svc.jog_z_to(z)

    def jog_u_to(self, u: float) -> list[str] | None:
        """Jog U to absolute coordinate (forwarded immediately)."""
        return self._svc.jog_u_to(u)

    def jog_xy_rel(
        self, x: float | None = None, y: float | None = None
    ) -> list[str] | None:
        """Jog XY relative to the current position (forwarded immediately)."""
        return self._svc.jog_xy_rel(x, y)

    def jog_x_rel(self, x: float | None = None) -> list[str] | None:
        """Jog X relative to the current position (forwarded immediately)."""
        return self._svc.jog_x_rel(x)

    def jog_y_rel(self, y: float | None = None) -> list[str] | None:
        """Jog Y relative to the current position (forwarded immediately)."""
        return self._svc.jog_y_rel(y)

    def jog_z_rel(self, z: float | None = None) -> list[str] | None:
        """Jog Z relative to the current position (forwarded immediately)."""
        return self._svc.jog_z_rel(z)

    def jog_u_rel(self, u: float | None = None) -> list[str] | None:
        """Jog U relative to the current position (forwarded immediately)."""
        return self._svc.jog_u_rel(u)

    def home(self) -> list[str] | None:
        """Jog X and Y axes to the origin reference (forwarded immediately)."""
        return self._svc.home()

    def home_z(self) -> list[str] | None:
        """Home Z axis (forwarded immediately)."""
        return self._svc.home_z()

    def home_u(self) -> list[str] | None:
        """Home U axis / rotary (forwarded immediately)."""
        return self._svc.home_u()

    # ------------------------------------------------------------------ #
    #  Format utilities — forwarded passthroughs
    # ------------------------------------------------------------------ #

    def format_reply_value(
        self, address: int, raw_reply: bytearray
    ) -> tuple[str | None, str]:
        """Decode a reply bytearray into (mnemonic, formatted_value).

        Mirrors RdDriver.format_reply_value().
        """
        return self._svc.format_reply_value(address, raw_reply)

    def format_reply(self, reply: bytearray) -> str:
        """Format a GET_SETTING reply bytearray as a readable string.

        Mirrors RdDriver.format_reply().
        """
        return self._svc.format_reply(reply)

    def format_reply_list(self, replies: list[bytearray]) -> list[str]:
        """Format a list of reply bytearrays into readable strings.

        Mirrors RdDriver.format_reply_list().
        """
        return self._svc.format_reply_list(replies)

    def decode_status_value(self, address: int, raw_reply: bytearray) -> Any:
        """Decode a reply into its typed value (RdDecoder.value).

        Mirrors RdDriver.decode_status_value().
        """
        return self._svc.decode_status_value(address, raw_reply)
