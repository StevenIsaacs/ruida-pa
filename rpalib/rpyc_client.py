"""Client-side batching wrapper around the RPyC service root,
mirroring the RdDriver surface.

``RpcRdDriver`` wraps the already-connected RPyC service root (the object
returned by ``connect_stream(...).root``, as in tests/rpyc_poc/test_auth.py)
and exposes the same job-scripting and lifecycle-execution surface as the
direct RdDriver, but with local batching so a whole job reaches the server
in a few round trips instead of one per command. It does NOT open the
socket itself — the caller passes the connected ``svc`` root into the
constructor.

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
- Validation errors (bad ``declare_layer`` mode, etc.) surface at flush
  time, wrapped in ``RuntimeError`` ("Error re-staging command ...")
  rather than at call time.

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
The ``gluescript``, ``rpascript`` and ``job_complete`` attributes report the
server's LAST-FLUSHED state; the retained method aliases ``get_gluescript()``
and ``get_rpascript()`` return the same values. Between flushes, buffered
actions exist only in the client's ``_transcript`` and are invisible to the
getters until the next boundary flush. ``sync()`` re-baselines the
transcript, ``_flushed_count``, ``_current_mode`` and ``_job_complete`` from
that last-flushed state.

The attribute forms (``gluescript``, ``rpascript``, ``job_complete``,
``is_connected``) and the lifecycle/execution passthroughs (``start``,
``stop``, ``run``, ``run_job``, and the head/tail script setters and
getters) mirror the direct RdDriver surface, so an app adapter needs no
separate direct-vs-RPC path.
"""

from __future__ import annotations

import ast
import logging
from typing import Any

from rpalib.gluescript_signature import (
    GlueScriptDeltaMismatchError,
    gluescript_signature,
    is_valid_time_value,
)

logger = logging.getLogger(__name__)


class RpcRdDriver:
    """Client-side batching wrapper for the RPyC service root,
    mirroring the RdDriver surface.
    """

    _svc: Any
    _transcript: list[str]
    _flushed_count: int
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
        self._flushed_count: int = 0
        self._current_mode: str = "VECTOR"
        self._job_complete: bool = False
        self._job_declared: bool = False

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _derive_mode_from_transcript(transcript: list[str]) -> str:
        """Return the mode of the LAST declare_layer line in a transcript.

        Labels and colors are repr-quoted and may contain commas, so the
        argument list is parsed with ``ast.literal_eval`` (the driver's
        own gluescript parse boundary) rather than a naive split.
        Defaults to "VECTOR" when no layer has been declared.
        """
        mode = "VECTOR"
        for line in transcript:
            stripped = line.strip()
            if not stripped.startswith("declare_layer("):
                continue
            # Mirrors the driver's parse boundary (_parse_gluescript_line)
            # and is safe for client-generated mirror lines.
            args_str = stripped[len("declare_layer("):].rstrip(")")
            try:
                args = ast.literal_eval(f"({args_str})")
            except (ValueError, SyntaxError):
                continue
            if len(args) >= 3:
                mode = args[2]
        return mode

    def _append(self, line: str) -> None:
        """Buffer one mirrored gluescript line on the client transcript."""
        self._transcript.append(line)

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

        Re-baselines the transcript, ``_flushed_count``, ``_current_mode``
        and ``_job_complete`` from server state. ``_current_mode`` is
        derived from the last ``declare_layer(...)`` line in the
        transcript (defaulting to "VECTOR" when no layer was declared).
        ``_job_declared`` is NOT refreshed because it cannot be reliably
        derived from the server transcript (and the buffered value is
        client-authoritative for unflushed state).
        """
        self._transcript = list(self._svc.get_gluescript())
        self._flushed_count = len(self._transcript)
        self._current_mode = self._derive_mode_from_transcript(
            self._transcript
        )
        self._job_complete = bool(self._svc.job_complete())

    # ------------------------------------------------------------------ #
    #  Job authoring — forwarded, mirrored into the local transcript
    # ------------------------------------------------------------------ #

    def new_gluescript(self) -> None:
        """Reset all script data for a new job (forwarded immediately).

        Identical strictness to the driver's ``new_gluescript()``: both
        this client and the driver reset ``_job_declared`` and
        ``_job_complete``, so ``end_job()`` after a bare
        ``new_gluescript()`` fails fast on either side. Also re-zeros
        ``_flushed_count``: the forwarded server reset empties the server
        transcript, restoring ``len(server) == _flushed_count == 0``.
        """
        self._svc.new_gluescript()
        self._transcript = []
        self._current_mode = "VECTOR"
        self._job_complete = False
        self._job_declared = False
        self._flushed_count = 0

    def comment(self, comments: list[str]) -> None:
        """Append comment lines (mirrored; forwarded only after end_job).

        Before ``end_job()`` the line is mirrored into the local
        transcript and reaches the server with the next boundary flush.
        After ``end_job()`` no flush boundary exists, so the epilogue is
        forwarded immediately — the only way post-``end_job`` lines reach
        the server.

        An empty list is a no-op, mirroring the driver.
        """
        if not comments:
            return
        if self._job_complete:
            self._svc.comment(comments)
        for line in comments:
            self._append(f"comment({[line]!r})")

    def inline(self, commands: list[str]) -> None:
        """Append raw rpascript commands (mirrored; forwarded after end_job).

        Before ``end_job()`` each command is mirrored into the local
        transcript and replayed on the server with the next boundary
        flush. After ``end_job()`` no flush boundary exists, so the
        epilogue is forwarded immediately — the only way post-``end_job``
        lines reach the server.

        An empty list is a no-op, mirroring the driver.
        """
        if not commands:
            return
        if self._job_complete:
            self._svc.inline(commands)
        for command in commands:
            self._append(f"inline({[command]!r})")

    def delay(self, time: str | int | float) -> None:
        """Append a runner-directive DELAY (mirrored; forwarded after end_job).

        Emits a runner-directive DELAY line: the runner sleeps for the
        given time inline during script execution — the command is never
        encoded or sent to the controller. Unlike jog/home live commands,
        DELAY is part of a saved job and is replayed from a persisted
        gluescript.

        Before ``end_job()`` the line is mirrored into the local
        transcript and reaches the server with the next boundary flush.
        After ``end_job()`` no flush boundary exists, so the epilogue is
        forwarded immediately — the only way post-``end_job`` lines reach
        the server.

        Invalid values are rejected locally via the shared
        ``is_valid_time_value`` predicate (mirroring the driver's
        warn-and-no-op) so the mirrored line stays byte-identical with
        what the server appends; otherwise the SHA-256 drift check would
        break.
        """
        if not is_valid_time_value(time):
            logger.warning(
                "delay() requires a time value (e.g. 5s, 500ms) — got %r", time
            )
            return
        if self._job_complete:
            self._svc.delay(time)
        self._append(f"delay({time!r})")

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
        transcript and reaches the server with the next boundary flush.
        After ``end_job()`` no flush boundary exists, so the epilogue is
        forwarded immediately — the only way post-``end_job`` lines reach
        the server.

        Invalid values are rejected locally via the shared
        ``is_valid_time_value`` predicate (mirroring the driver's
        warn-and-no-op) so the mirrored line stays byte-identical with
        what the server appends; otherwise the SHA-256 drift check would
        break.
        """
        if not isinstance(status, str) or not status.strip():
            logger.warning(
                "wait() requires a MACHINE_STATUS_* name — got %r", status
            )
            return
        if to is not None and not is_valid_time_value(to):
            logger.warning(
                "wait() to= requires a time value (e.g. 30s) — got %r", to
            )
            return
        if self._job_complete:
            self._svc.wait(status, to)
        if to is None:
            self._append(f"wait({status!r})")
        else:
            self._append(f"wait({status!r}, {to!r})")

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
        """Declare a new job (server reset; mirrored for the boundary flush).

        Forwards ``new_gluescript()`` so the server resets to an empty
        transcript (length 0 == the local ``_flushed_count``), then
        mirrors the ``declare_job`` line locally; the line itself reaches
        the server with the next boundary flush. Validation (e.g. an
        invalid ``ref_point``) now surfaces at that flush, wrapped in
        ``RuntimeError``, rather than at call time.

        Mirrors the driver's 7-arg line, resolving ``abs_xy=None`` to
        ``[0.0, 0.0]`` exactly as the driver does before formatting.

        Local state is reset BEFORE the forwarded server reset so a
        server-side failure cannot leave the previous job's lines in
        ``_transcript``.
        """
        self._transcript = []
        self._job_declared = True
        self._job_complete = False
        self._current_mode = "VECTOR"
        self._flushed_count = 0
        self._svc.new_gluescript()
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
        """Declare a new layer (mirrored; applied at the boundary flush).

        Sets ``_current_mode`` from the declared layer mode, mirrors the
        line locally, then stages the accumulated delta with
        ``require_complete=False`` (the job may still be in progress). The
        server applies the layer — including validation of mode, overscan
        and power range — at that flush; validation errors surface
        wrapped in ``RuntimeError`` ("Error re-staging command ...").

        Raises:
            RuntimeError: If declare_layer() is called after end_job().
        """
        if self._job_complete:
            raise RuntimeError("declare_layer() called after end_job()")
        self._current_mode = mode
        self._append(
            f"declare_layer({label!r}, {color!r}, {mode!r}, "
            f"{overscan!r}, {speed!r}, {frequency!r}, "
            f"{min_power_1!r}, {max_power_1!r})"
        )
        self._flush(require_complete=False)

    def end_job(self) -> None:
        """End the job, flush the buffered transcript, and mark complete.

        Raises:
            RuntimeError: If end_job() is called twice or no job was declared.

        Returns:
            None: The staged rpascript can be retrieved via get_rpascript()
            and the staged gluescript via get_gluescript().
        """
        if self._job_complete:
            raise RuntimeError("end_job() called twice")
        if not self._job_declared:
            raise RuntimeError("declare_job() must be called before end_job()")
        self._append("end_job()")
        self._flush(require_complete=True)
        self._job_complete = True

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

        Buffered when valid for the current layer mode; otherwise the
        call is dropped entirely and the driver's guard warning is
        emitted locally — nothing reaches the server and no line is
        mirrored, mirroring the driver's behavior for invalid power
        calls. This includes ``power(None)`` in an IMAGE/DEPTHMAP layer,
        which warns and drops.
        """
        if self._current_mode not in ("IMAGE", "DEPTHMAP"):
            logger.warning(
                "power() called in %s mode layer — only valid for "
                "IMAGE/DEPTHMAP layers",
                self._current_mode,
            )
            return
        if percent is None:
            logger.warning("power() called without a percentage value")
            return
        self._append(f"power({percent!r})")

    def air_assist_on(self) -> None:
        """Buffer air assist enable (flushed at the next boundary)."""
        self._append("air_assist_on()")

    def air_assist_off(self) -> None:
        """Buffer air assist disable (flushed at the next boundary)."""
        self._append("air_assist_off()")

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

    def get_gluescript(self) -> list[str]:
        """Return the server's gluescript transcript (last-flushed state).

        Method alias for the ``gluescript`` property; both mirror the
        direct driver's ``gluescript`` attribute.
        """
        return list(self._svc.get_gluescript())

    def get_rpascript(self) -> list[str]:
        """Return the server's assembled rpascript (last-flushed state).

        Method alias for the ``rpascript`` property; both mirror the
        direct driver's ``rpascript`` attribute.
        """
        return list(self._svc.get_rpascript())

    @property
    def gluescript(self) -> list[str]:
        """Server-side gluescript transcript (last-flushed state).

        Mirrors the direct driver's ``gluescript`` attribute.
        """
        return list(self.get_gluescript())

    @property
    def rpascript(self) -> list[str]:
        """Server-side assembled rpascript (last-flushed state).

        Mirrors the direct driver's ``rpascript`` attribute.
        """
        return list(self.get_rpascript())

    @property
    def job_complete(self) -> bool:
        """True once the server-side job has been finalized (last-flushed).

        Mirrors the direct driver's ``job_complete`` attribute.
        """
        return bool(self._svc.job_complete())

    @property
    def is_connected(self) -> bool:
        """True when the server-side session is connected.

        Mirrors the direct driver's ``is_connected`` attribute.
        """
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
