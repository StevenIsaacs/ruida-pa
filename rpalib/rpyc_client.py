"""Client-side batching wrapper around an RPyC GlueScript service root.

``RpcGlueScript`` wraps the already-connected RPyC service root (the object
returned by ``connect_stream(...).root``, as in tests/rpyc_poc/test_auth.py)
and exposes the same GlueScript surface as the driver, but with local
batching so a whole job reaches the server in a few round trips instead of
one per command. It does NOT open the socket itself — the caller passes the
connected ``svc`` root into the constructor.

Batching semantics
------------------
- Structural, non-replayable calls are forwarded to the server immediately:
  ``new_gluescript``, ``comment``, ``inline``, ``declare_job``,
  ``declare_layer``, ``add_layer_action``, ``update_position``, all jogs and
  homing, and the getters. Calls that append a gluescript line on the driver
  side (``comment``, ``inline``, ``declare_job``, ``declare_layer``) are
  also mirrored into a local ``_transcript``.
- Layer actions — ``move_*_to``, ``cut_*_to``, ``power``, ``air_assist_*``
  — are buffered locally and flushed in ONE ``stage_rpascript()`` call at
  the next boundary (``declare_layer`` or ``end_job``).

Drift guard
-----------
Every flush re-reads the server transcript via ``get_gluescript()`` and
compares it byte-for-byte with the local ``_transcript``. The server
re-stages the transcript through the same command registry as the driver,
so a match proves the server's state equals the client's belief. Any
mismatch raises ``RuntimeError`` naming the first differing index — fail
fast, fail loud.

Replay tradeoff
---------------
Each flush re-stages the FULL transcript, which the server replays through
its command registry line by line (O(L) per line, O(L*N) overall for L
lines and N flushes). Replaying is deliberately idempotent — the
transcript, not the server, is the source of truth — at the cost of
re-appending every line per flush. Incremental append of only the new lines
is deferred as a future optimization.

Public ``stage_rpascript()`` passthrough
----------------------------------------
``stage_rpascript()`` forwards the call unchanged (including
``gluescript=None`` for finalizing the current job) and does not touch the
local transcript or run the drift check.

Getter semantics
----------------
``get_gluescript()``, ``get_rpascript()`` and ``job_complete()`` report the
server's LAST-FLUSHED state. Between flushes, buffered actions exist only in
the client's ``_transcript`` and are invisible to the getters until the next
boundary flush.
"""

from __future__ import annotations

from typing import Any


class RpcGlueScript:
    """Client-side batching wrapper for the RPyC GlueScript service root."""

    _svc: Any
    _transcript: list[str]
    _current_mode: str
    _job_complete: bool
    _job_declared: bool

    def __init__(self, svc: Any) -> None:
        """Wrap a connected RPyC service root.

        Args:
            svc: The already-connected service root (``conn.root``) exposing
                the GlueScript methods without the ``exposed_`` prefix.
        """
        self._svc = svc
        self._transcript: list[str] = []
        self._current_mode: str = "VECTOR"
        self._job_complete: bool = False
        self._job_declared: bool = False

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _append(self, line: str) -> None:
        """Buffer one mirrored gluescript line on the client transcript."""
        self._transcript.append(line)

    def _flush(self, require_complete: bool) -> list[str]:
        """Stage the buffered transcript and verify server parity.

        Sends the full local transcript to ``stage_rpascript()``, then
        re-reads the server transcript. Any byte difference raises a
        descriptive ``RuntimeError``.

        Args:
            require_complete: Passed through to ``stage_rpascript()``.

        Returns:
            list[str]: The staged rpascript lines.

        Raises:
            RuntimeError: If the server transcript drifts from the local one.
        """
        staged = list(
            self._svc.stage_rpascript(
                list(self._transcript), require_complete=require_complete
            )
        )
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
        return staged

    def sync(self) -> None:
        """Re-baseline local state from the server's last-flushed state.

        Re-baselines the transcript and ``_job_complete`` from server state.
        ``_current_mode`` and ``_job_declared`` are NOT refreshed because
        they cannot be reliably derived from the server transcript (and the
        buffered ``_current_mode`` is client-authoritative for unflushed
        state).
        """
        self._transcript = list(self._svc.get_gluescript())
        self._job_complete = bool(self._svc.job_complete())

    # ------------------------------------------------------------------ #
    #  Job authoring — forwarded, mirrored into the local transcript
    # ------------------------------------------------------------------ #

    def new_gluescript(self) -> None:
        """Reset all script data for a new job (forwarded immediately).

        Deliberately stricter than the driver's ``new_gluescript()``: the
        driver resets ``_job_complete`` but NOT ``_job_declared``, whereas
        this client clears both so that ``end_job()`` after a bare
        ``new_gluescript()`` fails fast locally.
        """
        self._svc.new_gluescript()
        self._transcript = []
        self._current_mode = "VECTOR"
        self._job_complete = False
        self._job_declared = False

    def comment(self, comments: list[str]) -> None:
        """Append comment lines (forwarded; mirrored per line).

        An empty list is a no-op, mirroring the driver.
        """
        if not comments:
            return
        self._svc.comment(comments)
        for line in comments:
            self._append(f"comment({[line]!r})")

    def inline(self, commands: list[str]) -> None:
        """Append raw rpascript commands (forwarded; mirrored per command).

        An empty list is a no-op, mirroring the driver.
        """
        if not commands:
            return
        self._svc.inline(commands)
        for command in commands:
            self._append(f"inline({[command]!r})")

    def declare_job(
        self,
        label: str,
        ref_point: str = "MACHINE",
        abs_xy: list[float] | None = None,
        columns: int = 1,
        rows: int = 1,
        xstep: float = 0.0,
        ystep: float = 0.0,
    ) -> None:
        """Declare a new job (forwarded; local transcript reset + mirrored).

        Mirrors the driver's 7-arg line, resolving ``abs_xy=None`` to
        ``[0.0, 0.0]`` exactly as the driver does before formatting.

        Local state is reset BEFORE the forwarded RPC so a server-side
        failure (e.g. an invalid ``ref_point``) cannot leave the previous
        job's lines in ``_transcript``.
        """
        self._transcript = []
        self._job_declared = True
        self._job_complete = False
        self._current_mode = "VECTOR"
        self._svc.declare_job(
            label, ref_point, abs_xy, columns, rows, xstep, ystep
        )
        resolved_abs_xy = [0.0, 0.0] if abs_xy is None else abs_xy
        self._append(
            f"declare_job({label!r}, {ref_point!r}, {resolved_abs_xy!r}, "
            f"{columns!r}, {rows!r}, {xstep!r}, {ystep!r})"
        )

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
        """Declare a new layer (forwarded; mirrored) and flush buffered moves.

        Sets ``_current_mode`` from the declared layer mode, then stages the
        accumulated transcript with ``require_complete=False`` (the job may
        still be in progress).

        Raises:
            RuntimeError: If declare_layer() is called after end_job().
        """
        if self._job_complete:
            raise RuntimeError("declare_layer() called after end_job()")
        self._svc.declare_layer(
            label, color, mode, overscan, speed, frequency, min_power_1,
            max_power_1,
        )
        self._current_mode = mode
        self._append(
            f"declare_layer({label!r}, {color!r}, {mode!r}, "
            f"{overscan!r}, {speed!r}, {frequency!r}, "
            f"{min_power_1!r}, {max_power_1!r})"
        )
        self._flush(require_complete=False)

    def end_job(self) -> list[str]:
        """End the job, flush the buffered transcript, and mark complete.

        Raises:
            RuntimeError: If end_job() is called twice or no job was declared.

        Returns:
            list[str]: The staged rpascript lines.
        """
        if self._job_complete:
            raise RuntimeError("end_job() called twice")
        if not self._job_declared:
            raise RuntimeError("declare_job() must be called before end_job()")
        self._append("end_job()")
        staged = self._flush(require_complete=True)
        self._job_complete = True
        return staged

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
    # ------------------------------------------------------------------ #

    def move_xy_to(self, x: float, y: float) -> None:
        """Buffer a move to absolute XY (flushed at the next boundary)."""
        self._append(f"move_xy_to({x!r}, {y!r})")

    def move_x_to(self, x: float) -> None:
        """Buffer a move to absolute X (flushed at the next boundary)."""
        self._append(f"move_x_to({x!r})")

    def move_y_to(self, y: float) -> None:
        """Buffer a move to absolute Y (flushed at the next boundary)."""
        self._append(f"move_y_to({y!r})")

    def cut_xy_to(self, x: float, y: float) -> None:
        """Buffer a cut to absolute XY (flushed at the next boundary)."""
        self._append(f"cut_xy_to({x!r}, {y!r})")

    def cut_x_to(self, x: float) -> None:
        """Buffer a cut to absolute X (flushed at the next boundary)."""
        self._append(f"cut_x_to({x!r})")

    def cut_y_to(self, y: float) -> None:
        """Buffer a cut to absolute Y (flushed at the next boundary)."""
        self._append(f"cut_y_to({y!r})")

    def power(self, percent: float | None = None) -> None:
        """Set laser power for IMAGE/DEPTHMAP layers.

        Buffered when valid for the current layer mode; otherwise forwarded
        so the server emits the same warning the driver guard produces.
        """
        if self._current_mode in ("IMAGE", "DEPTHMAP") and percent is not None:
            self._append(f"power({percent!r})")
            return
        self._svc.power(percent)

    def air_assist_on(self) -> None:
        """Buffer air assist enable (flushed at the next boundary)."""
        self._append("air_assist_on()")

    def air_assist_off(self) -> None:
        """Buffer air assist disable (flushed at the next boundary)."""
        self._append("air_assist_off()")

    # ------------------------------------------------------------------ #
    #  Staging passthrough and getters — last-flushed server state only
    # ------------------------------------------------------------------ #

    def stage_rpascript(
        self,
        gluescript: list[str] | None = None,
        require_complete: bool = True,
    ) -> list[str]:
        """Public passthrough to the server's stage_rpascript().

        Forwarded unchanged (including ``gluescript=None``); no local flush
        and no drift check.

        Returns:
            list[str]: The staged rpascript lines.
        """
        return list(self._svc.stage_rpascript(gluescript, require_complete))

    def get_gluescript(self) -> list[str]:
        """Return the server's gluescript transcript (last-flushed state)."""
        return list(self._svc.get_gluescript())

    def get_rpascript(self) -> list[str]:
        """Return the server's assembled rpascript (last-flushed state)."""
        return list(self._svc.get_rpascript())

    def job_complete(self) -> bool:
        """Return whether the server job was finalized (last-flushed state)."""
        return bool(self._svc.job_complete())

    # ------------------------------------------------------------------ #
    #  Live-only commands — jogs, homing, config setters (forwarded)
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
        """Home X and Y axes (forwarded immediately)."""
        return self._svc.home()

    def home_z(self) -> list[str] | None:
        """Home Z axis (forwarded immediately)."""
        return self._svc.home_z()

    def home_u(self) -> list[str] | None:
        """Home U axis / rotary (forwarded immediately)."""
        return self._svc.home_u()
