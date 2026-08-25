"""
GlueScript — High-level job scripting for Ruida laser controllers.

Provides a command-registry-based method for generating rpascript
and gluescript representations of laser jobs. Intended to be used
as a mixin for RdDriver: class RdDriver(GlueScript).
"""

import ast
import logging
import re
import shlex
from typing import Any, Callable

from rpalib.gluescript_signature import (
    GlueScriptDeltaMismatchError,
    gluescript_signature,
    is_valid_time_value,
)
from rpalib.version import __version__

logger = logging.getLogger(__name__)


def _strip_inline_comment(line: str) -> str:
    """Remove inline # comments, respecting quoted strings and \\# escapes."""
    in_quote = False
    quote_char = None
    i = 0
    while i < len(line):
        ch = line[i]
        # Track escaped hash: \# — skip it as a literal hash
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "#" and not in_quote:
            i += 2  # skip both \ and #
            continue
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
        elif ch == quote_char and in_quote:
            in_quote = False
            quote_char = None
        elif ch == "#" and not in_quote:
            return line[:i].rstrip()
        i += 1
    return line


def _format_time_token(value: str | int | float) -> str:
    """Normalize a time value into a rpascript DELAY/WAIT time token.

    Numeric seconds (int/float, excluding bool) become a unit-suffixed
    token (e.g. 30 -> "30s", 0.5 -> "0.5s"). Strings must carry a unit
    suffix accepted by the runner (e.g. "500ms", "30s") and are emitted
    as a single whitespace-free token (e.g. "30 s" -> "30s"). Anything
    else, and any non-positive or non-finite value, is invalid.

    Raises:
        ValueError: If value cannot be represented as a time token.
    """
    if not is_valid_time_value(value):
        raise ValueError(
            f"time value must be a positive finite number or a "
            f"unit-suffixed string, got {value!r}"
        )
    if isinstance(value, str):
        # Emit the whitespace-free token ("30 s" -> "30s"); the shared
        # predicate already verified the compacted form.
        return "".join(value.split())
    return f"{value:g}s"


class GlueScript:
    """High-level job scripting methods for Ruida laser controllers.
    
    Generates gluescript (high-level) and rpascript (low-level) 
    representations simultaneously. The rpascript can be passed to
    RdDriver.run_job() for encoding and execution.
    
    Attributes:
        gluescript: list[str] — The generated high-level script commands.
        rpascript: list[str] — The generated low-level rpascript commands.
        jog_xy_speed: float — Jog speed for XY axes in mm/s.
        jog_z_speed: float — Jog speed for Z axis in mm/s.
        jog_u_speed: float — Jog speed for U axis in mm/s.
    """

    _version = __version__

    # Jog commands are live-only: movement jogs execute live on the
    # controller and are never part of a saved job (they also mutate
    # _current_x/_current_y/_current_z/_current_u as a side effect, so they
    # must not be invoked during re-stage); jog_set_* methods configure the
    # live jog session (speeds + relative defaults) without producing script
    # lines. Tracked position (X/Y/Z/U) is also synced from controller
    # replies during a live session between jobs.
    JOG_COMMANDS: frozenset[str] = frozenset({
        "jog_xy_to",
        "jog_x_to",
        "jog_y_to",
        "jog_z_to",
        "jog_u_to",
        "jog_xy_rel",
        "jog_x_rel",
        "jog_y_rel",
        "jog_z_rel",
        "jog_u_rel",
        "jog_set_xy_speed",
        "jog_set_z_speed",
        "jog_set_u_speed",
        "jog_set_xy_rel",
        "jog_set_z_rel",
        "jog_set_u_rel",
    })
    # Home commands are live-only machine actions: they home the axes on the
    # live session and are never part of a saved job. They require a
    # connected session (unlike jog_set_* setters).
    HOME_COMMANDS: frozenset[str] = frozenset({
        "home",
        "home_z",
        "home_u",
    })
    # Job-control commands act immediately on the controller regardless
    # of job-running state.
    JOB_CONTROL_COMMANDS: frozenset[str] = frozenset({
        "pause",
        "resume",
        "stop_job",
        "reset",
    })
    # Jog, home, and job-control commands form the three live-only command
    # groups. Any FUTURE live-only command that belongs to none of these
    # three groups must be added to LIVE_ONLY_COMMANDS separately (e.g.
    # LIVE_ONLY_COMMANDS = JOG_COMMANDS | {...}). Every live-only command —
    # including the job-control commands — must be registered in
    # registry_methods as well: the re-stage check consults the registry
    # first, so a missing registry entry would surface as "Unknown
    # gluescript command" instead of the live-only skip.
    LIVE_ONLY_COMMANDS: frozenset[str] = (
        JOG_COMMANDS | HOME_COMMANDS | JOB_CONTROL_COMMANDS
    )

    def __init__(self) -> None:
        """Initialize GlueScript with empty scripts and default state."""
        # Script storage
        self.gluescript: list[str] = []
        
        # Job management state
        self._job_complete: bool = False
        self._job_header: list[str] = []    # Lines from declare_job()
        self._layer_attributes: dict[int, list[str]] = {}  # Attributes per layer
        self._layer_actions: dict[int, list[str]] = {}     # Actions per layer
        self._layer_overscan: dict[int, list[str]] = {}   # Overscan lines per layer (emitted after SELECT_LAYER)
        self._job_declared: bool = False
        # True while a job is being assembled or re-staged — position sync
        # from controller replies is suspended.
        self._assembling: bool = False
        
        # Bounding boxes (doc-level)
        self.doc_tr_x: float = float('inf')
        self.doc_tr_y: float = float('inf')
        self.doc_bl_x: float = -float('inf')
        self.doc_bl_y: float = -float('inf')
        
        # Current position tracking
        self._current_x: float = 0.0
        self._current_y: float = 0.0
        self._current_z: float = 0.0
        self._current_u: float = 0.0
        self._abs_xy: list[float] = [0.0, 0.0]
        
        # Layer counter and current mode
        self._layer: int = 0
        self._current_layer_mode: str = "VECTOR"
        
        # Per-layer bounding box (reset each declare_layer)
        self._layer_trx: float = float('inf')
        self._layer_try: float = float('inf')
        self._layer_blx: float = -float('inf')
        self._layer_bly: float = -float('inf')
        self._last_layer_has_content: bool = False
        # Per-layer power fallbacks: power_range() resolves omitted args
        # from these declared powers. Defaults mirror the declare_layer()
        # arguments and are reset per layer.
        self._current_layer_min_power: float = 8.0
        self._current_layer_max_power: float = 70.0

        # Script output (assembled by stage_gluescript)
        self.rpascript: list[str] = []
        self._inline_used: bool = False
        # Caller-set toggle (per-instance): when False, the staging warning
        # for inline() use is suppressed (the TUI validates a throwaway
        # instance before applying to the real driver). Never reset by
        # new_gluescript() or the re-stage reset block — re-stage replays
        # declare_job(), which calls new_gluescript(); resetting here would
        # re-arm the flag mid-replay and defeat the suppression.
        self._warn_inline: bool = True
        # Per-job flag: set when a comment-only layer action
        # (move_speed/frequency/pwm) is used; cleared by
        # new_gluescript() and the re-stage reset block.
        self._comment_only_used: bool = False
        # Caller-set toggle (per-instance): when False, the staging warning
        # for comment-only layer actions is suppressed (the TUI validates a
        # throwaway instance before applying to the real driver). Never
        # reset by new_gluescript() or the re-stage reset block — re-stage
        # replays declare_job(), which calls new_gluescript(); resetting
        # here would re-arm the flag mid-replay and defeat the suppression.
        self._warn_comment_only: bool = True
        self._inline_prelude: list[str] = []    # inline() before first layer
        self._inline_epilogue: list[str] = []   # inline() after end_job()
        self._stage_complete: bool = False
        
        # Jog speed defaults (mm/s)
        self.jog_xy_speed: float = 100.0
        self.jog_z_speed: float = 100.0
        self.jog_u_speed: float = 100.0
        
        # Relative jog distance defaults (mm)
        self.x_rel: float = 10.0
        self.y_rel: float = 10.0
        self.z_rel: float = 10.0
        self.u_rel: float = 10.0
        
        # Reference point and overscan dictionaries
        self._ref_points: dict[str, list[str]] = {
            "MACHINE": [
                "REF_POINT_MACHINE",
                "SET_ABSOLUTE",
            ],
            "ABSOLUTE": [
                "JOG_XY Rel=MACHINE X={self._abs_xy[0]} Y={self._abs_xy[1]}",
                "REF_POINT_CURRENT",
            ],
            "CURRENT": [
                "REF_POINT_CURRENT",
            ],
            "SET_POINT": [
                "REF_POINT_ORIGIN",
            ],
        }
        
        self._layer_modes: dict[str, str] = {
            "VECTOR": "NONE",
            "RASTER": "",
            "DITHER": "",
            "IMAGE": "",
            "DEPTHMAP": "",
        }
        
        self._overscan_modes: dict[str, list[str]] = {
            "NONE": ["OVERSCAN_OFF"],
            "X": ["OVERSCAN_H_UNI"],
            "X_BI": ["OVERSCAN_H_BI"],
            "Y": ["OVERSCAN_V_UNI"],
            "Y_BI": ["OVERSCAN_V_BI"],
            "XY": [
                "# Diagonal overscan is not supported.",
                "OVERSCAN_OFF",
            ],
        }
        
        # Command registry for re-staging
        self._command_registry: dict[str, Callable[..., None]] = {}
        self._build_command_registry()

    # ------------------------------------------------------------------ #
    #  Registry
    # ------------------------------------------------------------------ #

    def _build_command_registry(self) -> None:
        """Build the command registry mapping method names to bound methods.
        
        This registry is used during re-staging to call the appropriate
        method for each gluescript command line.
        """
        registry_methods = [
            "new_gluescript",
            "comment",
            "inline",
            "delay",
            "wait",
            "declare_job",
            "end_job",
            "declare_layer",
            "move_xy_to",
            "move_x_to",
            "move_y_to",
            "cut_xy_to",
            "cut_x_to",
            "cut_y_to",
            "power",
            "power_range",
            "air_assist_on",
            "air_assist_off",
            "cut_speed",
            "move_speed",
            "frequency",
            "pwm",
            "select_laser",
            "jog_set_xy_speed",
            "jog_set_z_speed",
            "jog_set_u_speed",
            "jog_set_xy_rel",
            "jog_set_z_rel",
            "jog_set_u_rel",
            "jog_xy_to",
            "jog_x_to",
            "jog_y_to",
            "jog_z_to",
            "jog_u_to",
            "jog_xy_rel",
            "jog_x_rel",
            "jog_y_rel",
            "jog_z_rel",
            "jog_u_rel",
            "home",
            "home_z",
            "home_u",
            "pause",
            "resume",
            "stop_job",
            "reset",
        ]
        for name in registry_methods:
            self._command_registry[name] = getattr(self, name)

        # Contract: subclasses may override the job-control commands
        # (pause, resume, stop_job, reset) — e.g. to forward them over
        # RPC — but any override must preserve interrupt semantics: it
        # must forward to the controller/server so a running job can
        # still be paused, resumed, stopped, or reset.

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _count_top_level_commas(s: str) -> int:
        """Count commas at the top level (not inside brackets/parens/quotes)."""
        depth = 0
        in_quote = False
        quote_char: str | None = None
        count = 0
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if in_quote:
                if c == "\\":
                    i += 2
                    continue
                if c == quote_char:
                    in_quote = False
            else:
                if c in ("'", '"'):
                    in_quote = True
                    quote_char = c
                elif c in ("[", "(", "{"):
                    depth += 1
                elif c in ("]", ")", "}"):
                    depth -= 1
                elif c == "," and depth == 0:
                    count += 1
            i += 1
        return count

    def _parse_gluescript_line(self, line: str) -> tuple[str, list[Any]]:
        """Parse a gluescript line into method name and positional args.
        
        Format: method_name(arg1, arg2, ...)
        Each arg is a Python literal (via repr) parsed by ast.literal_eval.
        """
        line = line.strip()
        line = _strip_inline_comment(line)
        if "(" not in line:
            raise ValueError(
                f"Missing '(' in gluescript line: {line!r}. "
                f"Expected format: method_name(arg1, arg2, ...)"
            )
        idx = line.index("(")
        name = line[:idx].strip()
        args_str = line[idx + 1 :].rstrip(")").strip()
        if not args_str:
            return name, []
        num_commas = self._count_top_level_commas(args_str)
        if num_commas >= 1:
            # Multiple comma-separated args — wrap in tuple for literal_eval
            parsed = ast.literal_eval(f"({args_str})")
            return name, list(parsed)
        else:
            # Single arg
            parsed = ast.literal_eval(args_str)
            return name, [parsed]

    def _expand_deferred(self, rpascript: list[str]) -> list[str]:
        """Expand deferred {self.<var>} references in rpascript lines."""
        expanded: list[str] = []
        for line in rpascript:
            if "{self." in line:

                def _replace_var(m: re.Match) -> str:
                    var_path = m.group(1)
                    parts = var_path.split(".")
                    obj: Any = self
                    for part in parts:
                        if "[" in part and part.endswith("]"):
                            attr, rest = part.split("[", 1)
                            idx_str = rest.rstrip("]")
                            try:
                                idx = ast.literal_eval(idx_str)
                            except (ValueError, SyntaxError) as exc:
                                raise RuntimeError(
                                    f"Invalid index in deferred variable: {var_path}"
                                ) from exc
                            obj = getattr(obj, attr)
                            obj = obj[idx]
                        else:
                            try:
                                obj = getattr(obj, part)
                            except AttributeError as exc:
                                raise RuntimeError(
                                    f"Unknown deferred variable: self.{var_path}"
                                ) from exc
                    # Format with appropriate precision
                    if isinstance(obj, float):
                        return f"{obj:.3f}"
                    return str(obj)

                line = re.sub(r"\{self\.([^}]+)\}", _replace_var, line)
            expanded.append(line)
        return expanded

    # ------------------------------------------------------------------ #
    #  Phase 1: Foundation
    # ------------------------------------------------------------------ #

    def new_gluescript(self) -> None:
        """Reset all script data for a new job."""
        self.gluescript = []
        self._job_header = []
        self._layer_attributes = {}
        self._layer_actions = {}
        self._layer_overscan = {}
        self._job_complete = False
        self._job_declared = False
        self._assembling = False
        self._inline_used = False
        self._comment_only_used = False
        self._inline_prelude = []
        self._inline_epilogue = []
        self._stage_complete = False
        self.doc_tr_x = float('inf')
        self.doc_tr_y = float('inf')
        self.doc_bl_x = -float('inf')
        self.doc_bl_y = -float('inf')
        self._layer = 0
        self._current_layer_mode = "VECTOR"
        self._layer_trx = float('inf')
        self._layer_try = float('inf')
        self._layer_blx = -float('inf')
        self._layer_bly = -float('inf')
        self._last_layer_has_content = False
        # Power-range fallbacks — defaults mirror the declare_layer() args.
        self._current_layer_min_power = 8.0
        self._current_layer_max_power = 70.0
        # rpascript is assembled by stage_gluescript() — clear to empty
        self.rpascript = []

    def comment(self, comments: list[str]) -> None:
        """Append comment lines to the generated rpascript.
        
        Args:
            comments: List of comment lines to append.
        """
        if not comments:
            return
        for line in comments:
            self.gluescript.append(f"comment({[line]!r})")
            if not line.startswith("#"):
                line = "# " + line
            self._job_header.append(line)

    def _route_positional(self, line: str) -> None:
        """Route a generated line to the position-aware buffer.

        Matches inline(): after end_job() the line lands just before
        END_JOB; inside a declared layer it lands in that layer's action
        block at the call position; otherwise it lands right after the
        job header.
        """
        if self._job_complete:
            # After end_job() — lands just before END_JOB.
            self._inline_epilogue.append(line)
        elif self._layer >= 1:
            # Inside a declared layer — lands in that layer's action
            # block at the call position. Never route to layer 0.
            self._layer_actions.setdefault(self._layer, []).append(line)
        else:
            # Job declared but no layer yet — lands right after the
            # job header.
            self._inline_prelude.append(line)

    def inline(self, commands: list[str]) -> None:
        """Append raw rpascript commands at the call point.

        Commands are inserted positionally into the assembled rpascript,
        exactly where they are called:
          - before any layer is declared: right after the job header
          - inside a declared layer: in that layer's action block, between
            the surrounding actions
          - after end_job(): just before the closing END_JOB line

        Note: inline() called before declare_job() is discarded by the job
        reset — declare_job() calls new_gluescript(), which wipes both the
        transcript and the prelude buffer.

        This method should only be used for working around issues or
        experimentation. A need to use inline() suggests a new GlueScript
        method may be needed.

        Args:
            commands: List of raw rpascript command lines.

        Raises:
            TypeError: If any command entry is not a string.
        """
        if not commands:
            return
        self._inline_used = True
        for cmd in commands:
            if not isinstance(cmd, str):
                raise TypeError(
                    f"inline() commands must be strings, got {type(cmd).__name__}: {cmd!r}"
                )
            self.gluescript.append(f"inline({[cmd]!r})")
            self._route_positional(cmd)

    # ------------------------------------------------------------------ #
    #  Phase 1 Flow Control (runner directives)
    # ------------------------------------------------------------------ #

    def delay(self, time: str | int | float) -> None:
        """Append a pause between rpascript commands at the call point.

        Emits a runner-directive DELAY line: the runner sleeps for the
        given time inline during script execution — the command is never
        encoded or sent to the controller. Unlike jog/home live commands,
        DELAY is part of a saved job and is replayed from a persisted
        gluescript.

        The time is accepted as numeric seconds (e.g. 30, 0.5) or a
        unit-suffixed string (e.g. '500ms', '30s') and normalized into
        the rpascript directive (e.g. ``delay 30s``). Invalid values log
        a warning and no-op.

        Positional routing matches inline(): before any layer is declared
        the DELAY lands right after the job header; inside a declared
        layer it lands in that layer's action block at the call position;
        after end_job() it lands just before END_JOB.

        Note: delay() called before declare_job() is discarded by the job
        reset — declare_job() calls new_gluescript(), which wipes both the
        transcript and the prelude buffer.

        Args:
            time: Seconds as a number, or a unit-suffixed string
                (e.g. '500ms', '30s').
        """
        try:
            token = _format_time_token(time)
        except ValueError:
            logger.warning(
                "delay() requires a time value (e.g. 5s, 500ms) — got %r", time
            )
            return
        self.gluescript.append(f"delay({time!r})")
        # The interpreter matches the flow-control mnemonic case-
        # sensitively in lowercase (rpascript/interpreter.py); the
        # runner's DELAY directive type is derived from that token.
        line = f"delay {token}"
        self._route_positional(line)

    def wait(self, status: str, to: str | int | float | None = None) -> None:
        """Wait for a machine status bit at the call point.

        Emits a runner-directive WAIT line: the runner polls the live
        machine status during script execution and blocks until the
        status matches — the command is never encoded or sent to the
        controller. Unlike jog/home live commands, WAIT is part of a
        saved job and is replayed from a persisted gluescript.

        ``status`` is a MACHINE_STATUS_* name passed through verbatim; a
        leading '!' waits for the full active→inactive lifecycle (the
        status must first become active, then clear — with the optional
        timeout). The name is validated at run time by the runner, not
        here (mirrors how move/cut methods do not validate coordinates).
        The optional ``to=`` timeout accepts numeric seconds or a
        unit-suffixed string and is normalized into the rpascript token
        (e.g. ``to=30s``).

        Positional routing matches inline(): before any layer is declared
        the WAIT lands right after the job header; inside a declared
        layer it lands in that layer's action block at the call position;
        after end_job() it lands just before END_JOB.

        Note: wait() called before declare_job() is discarded by the job
        reset — declare_job() calls new_gluescript(), which wipes both the
        transcript and the prelude buffer.

        Args:
            status: MACHINE_STATUS_* name, optionally prefixed with '!'.
            to: Optional timeout in seconds (number) or as a
                unit-suffixed string (e.g. '30s', '500ms').
        """
        if not isinstance(status, str) or not status.strip():
            logger.warning(
                "wait() requires a MACHINE_STATUS_* name — got %r", status
            )
            return
        token = None
        if to is not None:
            try:
                token = _format_time_token(to)
            except ValueError:
                logger.warning(
                    "wait() to= requires a time value (e.g. 30s) — got %r", to
                )
                return
        if token is None:
            self.gluescript.append(f"wait({status!r})")
            line = f"wait {status}"
        else:
            # RAW to in transcript — the client-side RPC mirror is
            # byte-identical and the SHA-256 drift check depends on it.
            self.gluescript.append(f"wait({status!r}, {to!r})")
            line = f"wait {status} to={token}"
        self._route_positional(line)

    # ------------------------------------------------------------------ #
    #  Phase 2: Reference Points & Job Management
    # ------------------------------------------------------------------ #

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
        """Declare a new job.
        
        Resets all script data, validates the reference point, and appends
        job header rpascript lines.
        
        Args:
            label: Label to identify the job.
            ref_point: Reference point type (MACHINE, ABSOLUTE, CURRENT, SET_POINT).
            abs_xy: Absolute XY coordinate (required only for ABSOLUTE ref_point).
            columns: Number of columns for job copies.
            rows: Number of rows for job copies.
            xstep: X step distance in mm between job copies.
            ystep: Y step distance in mm between job copies.
        
        Raises:
            ValueError: If ref_point is invalid.
        """
        if abs_xy is None:
            abs_xy = [0.0, 0.0]

        # Always reset
        self.new_gluescript()

        # Validate ref_point
        if ref_point not in self._ref_points:
            raise ValueError(
                f"Invalid reference point: {ref_point!r}. "
                f"Valid options: {', '.join(self._ref_points)}"
            )

        # Warn if abs_xy provided but ref_point is not ABSOLUTE
        if ref_point != "ABSOLUTE" and (abs_xy[0] != 0.0 or abs_xy[1] != 0.0):
            logger.warning(
                "abs_xy=%s provided but ref_point=%r; value will be ignored",
                abs_xy, ref_point,
            )

        # Update state
        self._job_declared = True
        # _assembling window: new_gluescript reset it, so a reply-driven
        # update_position may reflect the live head position — harmless.
        self._assembling = True
        self._abs_xy = abs_xy

        # gluescript
        self.gluescript.append(
            f"declare_job({label!r}, {ref_point!r}, {abs_xy!r}, "
            f"{columns!r}, {rows!r}, {xstep!r}, {ystep!r})"
        )

        # rpascript — job header (assembled later by stage_gluescript)
        self._job_header.append(f"# Job: {label}")
        self._job_header.append(f"# Generated by: GlueScript {self._version}")
        self._job_header.extend(self._ref_points[ref_point])
        self._job_header.append("REF_POINT_SET")
        self._job_header.append("START_JOB")
        self._job_header.append("FEED_REPEAT 0 0")
        self._job_header.append("SET_FEED_AUTO_PAUSE State:OFF")
        self._job_header.append("# Job settings")
        self._job_header.append("JOB_TOP_RIGHT X={self.doc_tr_x}mm Y={self.doc_tr_y}mm")
        self._job_header.append("JOB_BOTTOM_LEFT X={self.doc_bl_x}mm Y={self.doc_bl_y}mm")
        self._job_header.append("DOCUMENT_TOP_RIGHT X={self.doc_tr_x}mm Y={self.doc_tr_y}mm")
        self._job_header.append("DOCUMENT_BOTTOM_LEFT X={self.doc_bl_x}mm Y={self.doc_bl_y}mm")
        self._job_header.append(f"JOB_COPIES Columns={columns} Rows={rows} XStep={xstep}mm YStep={ystep}mm")

    def end_job(self) -> None:
        """End the job and prepare it for staging.
        
        Raises:
            RuntimeError: If end_job() is called twice.
        """
        if self._job_complete:
            raise RuntimeError("end_job() called twice")
        if not self._job_declared:
            raise RuntimeError("declare_job() must be called before end_job()")

        # Write final layer's bbox with concrete values
        # (emitted layer numbers are 0-based; internal gluescript is 1-based)
        if self._layer > 0 and self._last_layer_has_content:
            self._layer_attributes[self._layer].append(
                f"LAYER_TOP_RIGHT Layer:{self._layer - 1} X={self._layer_trx:.3f}mm Y={self._layer_try:.3f}mm"
            )
            self._layer_attributes[self._layer].append(
                f"LAYER_BOTTOM_LEFT Layer:{self._layer - 1} X={self._layer_blx:.3f}mm Y={self._layer_bly:.3f}mm"
            )

        self._job_complete = True
        self._assembling = False
        self.gluescript.append("end_job()")
        self._on_action_boundary()

    @property
    def job_complete(self) -> bool:
        """True once end_job() has finalized the current job."""
        return self._job_complete

    # ------------------------------------------------------------------ #
    #  Phase 3: Layer Management
    # ------------------------------------------------------------------ #

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
        """Declare a new layer.
        
        Args:
            label: Label for the layer.
            color: Color formatted as #rrggbb.
            mode: Layer mode (VECTOR, RASTER, DITHER, IMAGE, DEPTHMAP).
            overscan: Overscan mode (NONE, X, X_BI, Y, Y_BI, XY).
            speed: Layer speed in mm/s.
            frequency: Laser PWM frequency in KHz.
            min_power_1: Minimum layer power percent for head 1.
            max_power_1: Maximum layer power percent for head 1.
        
        Raises:
            ValueError: If mode or overscan is invalid.

        Out-of-range power settings do not raise; they emit a ``# warning:``
        comment into the layer's rpascript attributes (and log a warning).
        """
        if mode not in self._layer_modes:
            raise ValueError(
                f"Invalid layer mode: {mode!r}. "
                f"Valid options: {', '.join(self._layer_modes)}"
            )
        if overscan not in self._overscan_modes:
            raise ValueError(
                f"Invalid overscan mode: {overscan!r}. "
                f"Valid options: {', '.join(self._overscan_modes)}"
            )

        # Validate power range — out-of-range values emit warning comments
        # into the rpascript output rather than raising.
        power_warnings: list[str] = []
        if min_power_1 < 8.0:
            logger.warning(
                "Minimum power %s%% is below 8%% — CO2 laser will not "
                "reliably fire below this threshold", min_power_1
            )
            power_warnings.append(
                f"# warning: min_power_1 {min_power_1}% is below the "
                f"recommended minimum of 8%"
            )
        if max_power_1 > 70.0:
            logger.warning(
                "Maximum power %s%% exceeds 70%% — CO2 laser tube life "
                "is reduced at higher power settings", max_power_1
            )
            power_warnings.append(
                f"# warning: max_power_1 {max_power_1}% exceeds the "
                f"recommended maximum of 70%"
            )

        # Resolve overscan from mode override
        resolved_overscan = overscan
        mode_override = self._layer_modes[mode]
        if mode_override:
            resolved_overscan = mode_override

        # Snapshot previous layer's bbox with concrete values. This runs
        # BEFORE self._layer += 1 and refers to the just-closed layer, which
        # is emitted 0-based (internal gluescript numbering is 1-based).
        if self._layer > 0 and self._last_layer_has_content:
            self._layer_attributes.setdefault(self._layer, []).append(
                f"LAYER_TOP_RIGHT Layer:{self._layer - 1} X={self._layer_trx:.3f}mm Y={self._layer_try:.3f}mm"
            )
            self._layer_attributes[self._layer].append(
                f"LAYER_BOTTOM_LEFT Layer:{self._layer - 1} X={self._layer_blx:.3f}mm Y={self._layer_bly:.3f}mm"
            )

        # Increment layer counter and set mode
        self._layer += 1
        self._current_layer_mode = mode

        # Reset per-layer bounding box
        self._layer_trx = float('inf')
        self._layer_try = float('inf')
        self._layer_blx = -float('inf')
        self._layer_bly = -float('inf')
        self._last_layer_has_content = False
        # Snapshot the declared power range for this layer: power_range()
        # resolves omitted args from these snapshots.
        self._current_layer_min_power = min_power_1
        self._current_layer_max_power = max_power_1

        # gluescript (positional args only — matches _parse_gluescript_line)
        self.gluescript.append(
            f"declare_layer({label!r}, {color!r}, {mode!r}, "
            f"{overscan!r}, {speed!r}, {frequency!r}, "
            f"{min_power_1!r}, {max_power_1!r})"
        )

        # rpascript — store in _layer_attributes (assembled later by
        # stage_gluescript); overscan lines go in _layer_overscan (emitted
        # after SELECT_LAYER). The controller is 0-based, so the emitted
        # layer numbers are self._layer - 1; internal gluescript numbering
        # stays 1-based (layer routing and _route_command rely on it).
        attrs: list[str] = []
        attrs.append(f"# Layer {self._layer - 1}: {label}")
        # Escape '#' so the rpascript interpreter's inline-comment stripping
        # does not eat the color value (matches rpascript/generator.py).
        attrs.append(f"LAYER_COLOR Layer:{self._layer - 1} Color:{color.replace('#', '\\#')}")
        attrs.append(
            f"CUT_SPEED_LASER_1 Layer:{self._layer - 1} Speed:{speed}mm/S"
        )
        attrs.append(
            f"LAYER_MIN_POWER_1 Layer:{self._layer - 1} Power:{min_power_1}%"
        )
        attrs.append(
            f"LAYER_MAX_POWER_1 Layer:{self._layer - 1} Power:{max_power_1}%"
        )
        attrs.extend(power_warnings)
        attrs.append(f"LAYER_ATTRIBUTES Layer:{self._layer - 1} 0")
        self._layer_attributes[self._layer] = attrs
        self._layer_overscan[self._layer] = list(self._overscan_modes[resolved_overscan])

        # Action boundary signal (for RPC batch sending)
        self._on_action_boundary()

    # ------------------------------------------------------------------ #
    #  Phase 4: Jogging & Homing Methods
    # ------------------------------------------------------------------ #

    def _emit_live_lines(self, lines: list[str]) -> list[str] | None:
        """Send generated live (jog/home) lines.

        Default (pure) implementation: returns the lines unchanged so a
        standalone GlueScript instance stays a pure generator. RdDriver
        overrides this to actually send the lines (see ruida_driver.py).
        """
        return lines

    def jog_set_xy_speed(self, speed: float) -> None:
        """Set XY jog speed in mm/s."""
        self.jog_xy_speed = speed

    def jog_set_z_speed(self, speed: float) -> None:
        """Set Z jog speed in mm/s."""
        self.jog_z_speed = speed

    def jog_set_u_speed(self, speed: float) -> None:
        """Set U jog speed in mm/s."""
        self.jog_u_speed = speed

    def jog_set_xy_rel(self, delta: float) -> None:
        """Set relative XY jog distance in mm."""
        self.x_rel = delta
        self.y_rel = delta

    def jog_set_z_rel(self, delta: float) -> None:
        """Set relative Z jog distance in mm."""
        self.z_rel = delta

    def jog_set_u_rel(self, delta: float) -> None:
        """Set relative U jog distance in mm."""
        self.u_rel = delta

    def jog_xy_to(self, x: float, y: float) -> list[str] | None:
        """Generate jog rpascript to move XY to absolute coordinate.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.

        Returns:
            list[str] | None: rpascript lines for the jog command, or
            None if nothing was sent.
        """
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_XY Rel:MACHINE X={x}mm Y={y}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_x = x
            self._current_y = y
        return sent

    def jog_x_to(self, x: float) -> list[str] | None:
        """Generate jog rpascript to move X to absolute coordinate.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.
        """
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_X Rel:MACHINE X={x}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_x = x
        return sent

    def jog_y_to(self, y: float) -> list[str] | None:
        """Generate jog rpascript to move Y to absolute coordinate.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.
        """
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_Y Rel:MACHINE Y={y}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_y = y
        return sent

    def jog_z_to(self, z: float) -> list[str] | None:
        """Generate jog rpascript to move Z to absolute coordinate.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent. Warns if current Z is above 2000mm (suggesting Z home
        needed).
        """
        if z > 2000.0:
            logger.warning(
                "Requested Z position %.3fmm is above 2000mm. "
                "A Z home may be required to establish the correct home position.",
                z,
            )
            return self._emit_live_lines([])
        lines = [
            f"SPEED_LASER_1 {self.jog_z_speed}",
            f"JOG_Z Rel:MACHINE Z={z}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_z = z
        return sent

    def jog_u_to(self, u: float) -> list[str] | None:
        """Generate jog rpascript to move U to absolute coordinate.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.
        """
        lines = [
            f"SPEED_LASER_1 {self.jog_u_speed}",
            f"JOG_U Rel:MACHINE U={u}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_u = u
        return sent

    def jog_xy_rel(self, x: float | None = None, y: float | None = None) -> list[str] | None:
        """Generate jog rpascript to move XY relative to current position.

        When x or y is None, the configured jog distance (x_rel / y_rel)
        is used. On RdDriver, sends the lines immediately via
        _emit_live_lines; on a standalone GlueScript (default hook),
        returns the generated lines unchanged. Returns the sent lines, or
        None if nothing was sent.
        """
        if x is None:
            x = self.x_rel
        if y is None:
            y = self.y_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_XY Rel:CURRENT X={x}mm Y={y}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_x += x
            self._current_y += y
        return sent

    def jog_x_rel(self, x: float | None = None) -> list[str] | None:
        """Generate jog rpascript to move X relative to current position.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.
        """
        if x is None:
            x = self.x_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_X Rel:CURRENT X={x}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_x += x
        return sent

    def jog_y_rel(self, y: float | None = None) -> list[str] | None:
        """Generate jog rpascript to move Y relative to current position.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.
        """
        if y is None:
            y = self.y_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_Y Rel:CURRENT Y={y}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_y += y
        return sent

    def jog_z_rel(self, z: float | None = None) -> list[str] | None:
        """Generate jog rpascript to move Z relative to current position.

        A positive distance moves the table down. On RdDriver, sends the
        lines immediately via _emit_live_lines; on a standalone
        GlueScript (default hook), returns the generated lines unchanged.
        Returns the sent lines, or None if nothing was sent.
        """
        if z is None:
            z = self.z_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_z_speed}",
            f"JOG_Z Rel:CURRENT Z={z}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_z += z
        return sent

    def jog_u_rel(self, u: float | None = None) -> list[str] | None:
        """Generate jog rpascript to move U relative to current position.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.
        """
        if u is None:
            u = self.u_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_u_speed}",
            f"JOG_U Rel:CURRENT U={u}mm",
        ]
        sent = self._emit_live_lines(lines)
        if sent is not None:
            self._current_u += u
        return sent

    def update_position(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        u: float | None = None,
    ) -> None:
        """Sync tracked position from controller-reported coordinates.

        Called when a live session receives position replies.
        While a job is being assembled or re-staged (_assembling), the
        position model is the trajectory cursor and must not be overwritten
        by replies — the update is then ignored.

        Args:
            x/y/z/u: New axis position in mm; None leaves that axis unchanged.
        """
        if self._assembling:
            return
        if x is not None:
            self._current_x = float(x)
        if y is not None:
            self._current_y = float(y)
        if z is not None:
            self._current_z = float(z)
        if u is not None:
            self._current_u = float(u)

    def home(self) -> list[str] | None:
        """Move the X and Y axes to the current origin reference.

        This jogs the XY axes to the controller's current (0, 0)
        reference point rather than performing a true machine home
        (which would establish the origin via limit switches). It
        deliberately avoids the controller reset that the HOME_XY
        command triggers. Note this intentionally differs from
        home_z()/home_u() (true homing) and from reset() (which still
        emits HOME_XY).

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.

        Returns:
            list[str] | None: rpascript lines for the home command.
        """
        return self.jog_xy_to(0, 0)

    def home_z(self) -> list[str] | None:
        """Generate rpascript to home the Z axis.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.

        Returns:
            list[str] | None: ["HOME_Z"] as rpascript to home the Z axis.
        """
        lines = ["HOME_Z"]
        return self._emit_live_lines(lines)

    def home_u(self) -> list[str] | None:
        """Generate rpascript to home the U axis (rotary).

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.

        Returns:
            list[str] | None: ["HOME_U"] as rpascript to home the U axis
            (rotary).
        """
        lines = ["HOME_U"]
        return self._emit_live_lines(lines)

    def pause(self) -> list[str] | None:
        """Generate rpascript to pause the current job.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.

        Returns:
            list[str] | None: ["PAUSE_JOB"] as rpascript to pause the job.
        """
        lines = ["PAUSE_JOB"]
        return self._emit_live_lines(lines)

    def resume(self) -> list[str] | None:
        """Generate rpascript to resume the current job.

        On RdDriver, sends the lines immediately via _emit_live_lines;
        on a standalone GlueScript (default hook), returns the generated
        lines unchanged. Returns the sent lines, or None if nothing was
        sent.

        Returns:
            list[str] | None: ["RESUME_JOB"] as rpascript to resume the
            job.
        """
        lines = ["RESUME_JOB"]
        return self._emit_live_lines(lines)

    def stop_job(self) -> list[str] | None:
        """Generate rpascript to stop the current job.

        Named stop_job (never stop) so this job-control command cannot
        shadow the RdDriver lifecycle stop() teardown. On RdDriver, sends
        the lines immediately via _emit_live_lines; on a standalone
        GlueScript (default hook), returns the generated lines unchanged.
        Returns the sent lines, or None if nothing was sent.

        Returns:
            list[str] | None: ["STOP_JOB"] as rpascript to stop the job.
        """
        lines = ["STOP_JOB"]
        return self._emit_live_lines(lines)

    def reset(self) -> list[str] | None:
        """Generate rpascript to reset the controller.

        Stops the current job and homes the X and Y axes. On RdDriver,
        sends the lines immediately via _emit_live_lines; on a standalone
        GlueScript (default hook), returns the generated lines unchanged.
        Returns the sent lines, or None if nothing was sent.

        Returns:
            list[str] | None: ["STOP_JOB", "HOME_XY"] as rpascript to
            reset the controller.
        """
        lines = ["STOP_JOB", "HOME_XY"]
        return self._emit_live_lines(lines)

    # ------------------------------------------------------------------ #
    #  Phase 5: Layer Actions — Moves, Cuts & Power
    # ------------------------------------------------------------------ #

    def _choose_move_form(self, delta: float) -> str:
        """Choose near or far form based on delta distance.
        
        Near form: fits in signed 14-bit / 1000 (range -8.192mm to 8.191mm).
        Far form: delta exceeds near range.
        """
        if -8.192 <= delta <= 8.191:
            return "NEAR"
        return "FAR"

    @staticmethod
    def _format_coord(
        form: str, axis: str, absolute: float, delta: float
    ) -> str:
        """Format one coordinate param for a move/cut layer action.

        NEAR commands are relative to the current position (nearX=/nearY=);
        FAR commands are absolute (X=/Y=).
        """
        if form == "NEAR":
            return f"near{axis}={delta:.3f}mm"
        return f"{axis}={absolute:.3f}mm"

    def _expand_bounding_boxes(self, x: float, y: float) -> None:
        """Expand layer and document bounding boxes to include (x, y)."""
        self._last_layer_has_content = True
        # Coerce to float so _expand_deferred applies .3f formatting
        fx, fy = float(x), float(y)
        # Layer — Ruida origin at top-right: TR = min(X,Y), BL = max(X,Y)
        self._layer_trx = min(self._layer_trx, fx)
        self._layer_try = min(self._layer_try, fy)
        self._layer_blx = max(self._layer_blx, fx)
        self._layer_bly = max(self._layer_bly, fy)
        # Document
        self.doc_tr_x = min(self.doc_tr_x, fx)
        self.doc_tr_y = min(self.doc_tr_y, fy)
        self.doc_bl_x = max(self.doc_bl_x, fx)
        self.doc_bl_y = max(self.doc_bl_y, fy)

    def power(self, percent: float | None = None) -> None:
        """Set laser power percentage. Valid only for IMAGE/DEPTHMAP layers.
        
        Args:
            percent: Power percentage.
        """
        if self._current_layer_mode not in ("IMAGE", "DEPTHMAP"):
            logger.warning(
                "power() called in %s mode layer — only valid for IMAGE/DEPTHMAP layers",
                self._current_layer_mode,
            )
            return
        if percent is None:
            logger.warning("power() called without a percentage value")
            return
        self.gluescript.append(f"power({percent!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"IMD_POWER_1 Power:{percent:.1f}%")

    def power_range(
        self, min: float | None = None, max: float | None = None
    ) -> None:
        """Set the min/max power ramp range for the current layer.

        Expands to ``MIN_POWER_1 Power:{min:.1f}%`` and
        ``MAX_POWER_1 Power:{max:.1f}%`` in the layer's action block,
        overriding the previously active ramp range from that point
        onward.

        Args:
            min: Minimum power percentage, or None to use the layer's
                declared min_power_1 (default 8.0).
            max: Maximum power percentage, or None to use the layer's
                declared max_power_1 (default 70.0).

        Constraints:
            - A layer must be declared first (raises ValueError).
            - min exceeding max emits a ``# warning:`` comment into the
              layer's action block (no longer raises).
            - min below 8% emits a ``# warning:`` comment into the layer's
              action block (no longer raises).
            - max above 70% logs a warning and emits a ``# warning:``
              comment, mirroring declare_layer().

        May be called multiple times per layer, including between jog,
        move, or cut actions: each call emits MIN_POWER_1/MAX_POWER_1 into
        the layer's action block at its call position, overriding the ramp
        range from that point onward. Omitted args always resolve from the
        layer's declared powers (not the previous power_range() call).

        Error surfaces: this method raises ValueError only when no layer
        has been declared; when re-staging wraps a replay of a persisted
        transcript, that violation surfaces as RuntimeError ("Error
        re-staging command ...") wrapping the ValueError.
        """
        if self._layer < 1:
            raise ValueError(
                "power_range() requires a declared layer — call "
                "declare_layer() first"
            )
        # Transcript must preserve the caller's own args (None falls back
        # to the declared layer powers on replay), while the rpascript
        # lines carry the resolved values.
        orig_min, orig_max = min, max
        resolved_min = self._current_layer_min_power if min is None else min
        resolved_max = self._current_layer_max_power if max is None else max
        # Out-of-range values emit warning comments into the rpascript
        # output rather than raising.
        power_warnings: list[str] = []
        if resolved_min > resolved_max:
            logger.warning(
                "Minimum power %s%% exceeds maximum power %s%%",
                resolved_min, resolved_max,
            )
            power_warnings.append(
                f"# warning: min_power_1 {resolved_min}% exceeds "
                f"max_power_1 {resolved_max}%"
            )
        if resolved_min < 8.0:
            logger.warning(
                "Minimum power %s%% is below 8%% — CO2 laser will not "
                "reliably fire below this threshold", resolved_min
            )
            power_warnings.append(
                f"# warning: min_power_1 {resolved_min}% is below the "
                f"recommended minimum of 8%"
            )
        if resolved_max > 70.0:
            logger.warning(
                "Maximum power %s%% exceeds 70%% — CO2 laser tube life "
                "is reduced at higher power settings", resolved_max
            )
            power_warnings.append(
                f"# warning: max_power_1 {resolved_max}% exceeds the "
                f"recommended maximum of 70%"
            )
        self.gluescript.append(f"power_range({orig_min!r}, {orig_max!r})")
        self._layer_actions.setdefault(self._layer, []).extend([
            f"MIN_POWER_1 Power:{resolved_min:.1f}%",
            f"MAX_POWER_1 Power:{resolved_max:.1f}%",
            *power_warnings,
        ])

    def air_assist_on(self) -> None:
        """Enable air assist for the current layer.

        Expands to AIR_ASSIST_ON in the rpascript layer actions.
        """
        self.gluescript.append("air_assist_on()")
        self._layer_actions.setdefault(self._layer, []).append("AIR_ASSIST_ON")

    def air_assist_off(self) -> None:
        """Disable air assist for the current layer.

        Expands to AIR_ASSIST_OFF in the rpascript layer actions.
        """
        self.gluescript.append("air_assist_off()")
        self._layer_actions.setdefault(self._layer, []).append("AIR_ASSIST_OFF")

    def cut_speed(self, speed: float) -> None:
        """Set cut speed for the current layer.

        Expands to a ``CUT_SPEED_LASER_1`` rpascript layer action carrying
        the speed value.
        """
        self.gluescript.append(f"cut_speed({speed!r})")
        self._layer_actions.setdefault(self._layer, []).append(
            f"CUT_SPEED_LASER_1 Layer:{self._layer - 1} Speed={speed}"
        )

    def move_speed(self, speed: float) -> None:
        """Set move speed for the current layer (comment-only for now).

        Expands to a ``# move_speed(...)`` comment in the rpascript layer
        actions — the speed command is not yet wired into rpascript, so
        the value is preserved in the transcript only.
        """
        self.gluescript.append(f"move_speed({speed!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"# move_speed({speed!r})")
        self._comment_only_used = True

    def frequency(self, frequency: float) -> None:
        """Set laser frequency for the current layer (comment-only for now).

        Expands to a ``# frequency(...)`` comment in the rpascript layer
        actions — the frequency command is not yet wired into rpascript,
        so the value is preserved in the transcript only.
        """
        self.gluescript.append(f"frequency({frequency!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"# frequency({frequency!r})")
        self._comment_only_used = True

    def pwm(self, duration: float) -> None:
        """Set laser pulse width in microseconds (comment-only for now).

        Expands to a ``# pwm(...)`` comment in the rpascript layer actions.
        Durations above 1000us (1mS) exceed the maximum laser pulse width
        and log a warning.
        """
        if duration > 1000:
            logger.warning(
                "pwm(%s) duration exceeds the 1000us (1mS) maximum laser pulse width",
                duration,
            )
        self.gluescript.append(f"pwm({duration!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"# pwm({duration!r})")
        self._comment_only_used = True

    def select_laser(self, laser: int) -> None:
        """Select a laser head for the current layer.

        Only laser head 1 is currently wired into rpascript; selecting any
        other head logs a warning and emits nothing, so the job keeps a
        single-head transcript.
        """
        self.gluescript.append(f"select_laser({laser!r})")
        if laser == 1:
            self._layer_actions.setdefault(self._layer, []).append("LASER_DEVICE_1")
        else:
            logger.warning(
                "select_laser(%s) ignored - only laser head 1 is currently wired in rpascript",
                laser,
            )

    def move_xy_to(self, x: float, y: float) -> None:
        """Move to absolute XY coordinate relative to job reference point."""
        delta_x = x - self._current_x
        delta_y = y - self._current_y
        form_x = self._choose_move_form(delta_x)
        form_y = self._choose_move_form(delta_y)
        # Use the more restrictive form
        form = "NEAR" if form_x == "NEAR" and form_y == "NEAR" else "FAR"
        self.gluescript.append(f"move_xy_to({x!r}, {y!r})")
        self._layer_actions.setdefault(self._layer, []).append(
            f"MOVE_{form}_XY {self._format_coord(form, 'X', x, delta_x)} "
            f"{self._format_coord(form, 'Y', y, delta_y)}"
        )
        self._current_x = x
        self._current_y = y
        self._expand_bounding_boxes(x, y)

    def move_x_to(self, x: float) -> None:
        """Move to absolute X coordinate relative to job reference point.

        This is a layer action: the emitted command is recorded in the
        current layer's action list.
        """
        delta_x = x - self._current_x
        form = self._choose_move_form(delta_x)
        self.gluescript.append(f"move_x_to({x!r})")
        self._layer_actions.setdefault(self._layer, []).append(
            f"MOVE_{form}_X {self._format_coord(form, 'X', x, delta_x)}"
        )
        self._current_x = x
        self._expand_bounding_boxes(x, self._current_y)

    def move_y_to(self, y: float) -> None:
        """Move to absolute Y coordinate relative to job reference point.

        This is a layer action: the emitted command is recorded in the
        current layer's action list.
        """
        delta_y = y - self._current_y
        form = self._choose_move_form(delta_y)
        self.gluescript.append(f"move_y_to({y!r})")
        self._layer_actions.setdefault(self._layer, []).append(
            f"MOVE_{form}_Y {self._format_coord(form, 'Y', y, delta_y)}"
        )
        self._current_y = y
        self._expand_bounding_boxes(self._current_x, y)

    def move_z_to(self, z: float) -> None:
        """Z-axis moves are not implemented in the initial release."""
        raise NotImplementedError("Z-axis moves not yet implemented")

    def move_u_to(self, u: float) -> None:
        """U-axis moves are not implemented in the initial release."""
        raise NotImplementedError("U-axis moves not yet implemented")

    def cut_xy_to(self, x: float, y: float) -> None:
        """Cut to absolute XY coordinate relative to job reference point."""
        delta_x = x - self._current_x
        delta_y = y - self._current_y
        form_x = self._choose_move_form(delta_x)
        form_y = self._choose_move_form(delta_y)
        form = "NEAR" if form_x == "NEAR" and form_y == "NEAR" else "FAR"
        self.gluescript.append(f"cut_xy_to({x!r}, {y!r})")
        self._layer_actions.setdefault(self._layer, []).append(
            f"CUT_{form}_XY {self._format_coord(form, 'X', x, delta_x)} "
            f"{self._format_coord(form, 'Y', y, delta_y)}"
        )
        self._current_x = x
        self._current_y = y
        self._expand_bounding_boxes(x, y)

    def cut_x_to(self, x: float) -> None:
        """Cut to absolute X coordinate relative to job reference point.

        This is a layer action: the emitted command is recorded in the
        current layer's action list.
        """
        delta_x = x - self._current_x
        form = self._choose_move_form(delta_x)
        self.gluescript.append(f"cut_x_to({x!r})")
        if form == "NEAR":
            line = f"CUT_NEAR_X {self._format_coord(form, 'X', x, delta_x)}"
        else:
            # CUT_FAR_X is not yet discovered — cut along X using the
            # two-axis FAR form, holding Y at its current position.
            line = f"CUT_FAR_XY X={x:.3f}mm Y={self._current_y:.3f}mm"
        self._layer_actions.setdefault(self._layer, []).append(line)
        self._current_x = x
        self._expand_bounding_boxes(x, self._current_y)

    def cut_y_to(self, y: float) -> None:
        """Cut to absolute Y coordinate relative to job reference point.

        This is a layer action: the emitted command is recorded in the
        current layer's action list.
        """
        delta_y = y - self._current_y
        form = self._choose_move_form(delta_y)
        self.gluescript.append(f"cut_y_to({y!r})")
        if form == "NEAR":
            line = f"CUT_NEAR_Y {self._format_coord(form, 'Y', y, delta_y)}"
        else:
            # CUT_FAR_Y is not yet discovered — cut along Y using the
            # two-axis FAR form, holding X at its current position.
            line = f"CUT_FAR_XY X={self._current_x:.3f}mm Y={y:.3f}mm"
        self._layer_actions.setdefault(self._layer, []).append(line)
        self._current_y = y
        self._expand_bounding_boxes(self._current_x, y)

    def cut_z_to(self, z: float) -> None:
        """Z-axis cuts are not implemented in the initial release."""
        raise NotImplementedError("Z-axis cuts not yet implemented")

    def cut_u_to(self, u: float) -> None:
        """U-axis cuts are not implemented in the initial release."""
        raise NotImplementedError("U-axis cuts not yet implemented")

    # ------------------------------------------------------------------ #
    #  Phase 6: Script Staging & Finalization
    # ------------------------------------------------------------------ #

    def _on_action_boundary(self) -> None:
        """Callback for RPC batch sending optimization.
        
        Called when declare_layer or end_job signals a boundary.
        Subclasses can override to send accumulated actions to a
        connected RPC client.
        """
        pass

    def add_layer_action(self, layer: int, lines: list[str]) -> None:
        """Add raw rpascript lines to a specific layer's action list.
        
        Used by external callers (e.g., TUI) to add jog commands or
        other actions to a layer. The lines will be assembled into
        the final rpascript by stage_gluescript(). `layer` is the internal
        1-based layer key (TUI/gluescript numbering, starting at 1);
        rpascript emission converts it to the controller's 0-based index
        (`layer_num - 1` at the SELECT_LAYER emission).
        """
        self._layer_actions.setdefault(layer, []).extend(lines)

    def _replay_lines(self, lines: list[str]) -> None:
        """Replay transcript lines through the command registry.

        Shared by the full re-stage path (``stage_gluescript``) and the
        incremental delta path (``stage_gluescript_delta``): blank lines
        and ``#``-comment lines are skipped, live-only commands are
        skipped with a warning, unknown commands and registry call
        errors raise ``RuntimeError``. Behavior is identical for both
        callers.
        """
        for line in lines:
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                continue
            try:
                name, args = self._parse_gluescript_line(line)
            except (ValueError, SyntaxError) as exc:
                raise RuntimeError(
                    f"Failed to parse gluescript line: {line!r}: {exc}"
                ) from exc
            if name not in self._command_registry:
                raise RuntimeError(
                    f"Unknown gluescript command: {name!r} in line {line!r}"
                )
            if name in self.LIVE_ONLY_COMMANDS:
                logger.warning(
                    "Skipping live-only command %r during re-stage "
                    "(live-only commands act on the live session and are not part of a saved job)",
                    name,
                )
                continue
            try:
                self._command_registry[name](*args)
            except Exception as exc:
                raise RuntimeError(
                    f"Error re-staging command {name!r} with args {args!r}: {exc}"
                ) from exc

    def _assemble_rpascript(self) -> None:
        """Assemble rpascript from structured storage.

        Shared by ``stage_gluescript()`` and ``stage_gluescript_delta()``:
        job header, inline prelude, all layer attributes, LAST_LAYER, all
        layer actions with SELECT_LAYER prefix, inline epilogue, END_JOB,
        EOF, then deferred-variable expansion. Sets ``_stage_complete``
        and fires the inline() staging warning when inline was used and
        the comment-only warning when move_speed/frequency/pwm
        were used.
        """
        self.rpascript = []

        # Section 1: Job header (reference point, start_job, settings)
        self.rpascript.extend(self._job_header)

        # Inline commands issued before the first layer land right after
        # the job header, preserving their call position.
        self.rpascript.extend(self._inline_prelude)

        # Section 2: All layer attributes (sorted by layer number)
        for layer_num in sorted(self._layer_attributes):
            self.rpascript.extend(self._layer_attributes[layer_num])

        # Between the layer attributes (Section 2) and the layer actions
        # (Section 3), tell the controller which layer is the last one in
        # the job. Layer keys are sequential 1-based ints, so the max key is
        # the number of layers; subtracting 1 yields the 0-based last index.
        # A job with no declared layers gets no LAST_LAYER.
        if self._layer_attributes:
            self.rpascript.append(
                f"LAST_LAYER Layer:{max(self._layer_attributes) - 1}"
            )

        # Section 3: All layer actions with SELECT_LAYER prefix
        # (SELECT_LAYER carries the same 0-based layer index as the attrs).
        # Overscan lines are emitted immediately after each SELECT_LAYER,
        # before the layer's actions.
        for layer_num in sorted(set(self._layer_actions) | set(self._layer_overscan)):
            self.rpascript.append(f"SELECT_LAYER Layer:{layer_num - 1}")
            self.rpascript.extend(self._layer_overscan.get(layer_num, []))
            self.rpascript.extend(self._layer_actions.get(layer_num, []))

        # Section 4: End of job
        # Inline commands issued after end_job() land just before END_JOB.
        self.rpascript.extend(self._inline_epilogue)
        self.rpascript.append("END_JOB")
        self.rpascript.append("EOF")

        # Expand deferred variables
        finalized = self._expand_deferred(self.rpascript)
        self.rpascript = finalized

        self._stage_complete = True
        if self._inline_used and self._warn_inline:
            logger.warning(
                "GlueScript used inline() while staging this job — inline "
                "commands are for experimentation and workarounds; a "
                "GlueScript method may be needed instead"
            )
        if self._comment_only_used and self._warn_comment_only:
            logger.warning(
                "GlueScript used move_speed/frequency/pwm - these "
                "expand to comments only; move speed/frequency will not change"
            )

    def stage_gluescript(
        self, gluescript: list[str] | None = None, require_complete: bool = True
    ) -> str:
        """Finalize the rpascript or re-stage a gluescript.

        Assembles rpascript from structured storage (see
        ``_assemble_rpascript``): job header first, then all layer
        attributes, then all layer actions with SELECT_LAYER prefix, then
        END_JOB.

        Args:
            gluescript: When provided, resets all state and replays the
                transcript through the command registry (re-staging path).
            require_complete: On the re-staging path, raises when the
                replayed transcript never called end_job(). Pass False to
                tolerate an in-progress job (used when restoring a
                preserved transcript across session teardown, where the
                user may still be editing).

        Returns:
            str: The SHA-256 signature (hex) of the staged gluescript
                transcript. Failures raise RuntimeError instead of
                returning.
        """
        # Re-staging path — skip _job_complete check; gluescript will set it
        if gluescript is not None:
            # Reset everything and replay gluescript through registry
            self._job_header = []
            self._layer_attributes = {}
            self._layer_actions = {}
            self._layer_overscan = {}
            self._job_complete = False
            self._job_declared = False
            self._layer = 0
            self._inline_used = False
            self._comment_only_used = False
            self._inline_prelude = []
            self._inline_epilogue = []
            self._layer_trx = float('inf')
            self._layer_try = float('inf')
            self._layer_blx = -float('inf')
            self._layer_bly = -float('inf')
            self._last_layer_has_content = False
            # Power-range fallbacks — defaults mirror the declare_layer() args.
            self._current_layer_min_power = 8.0
            self._current_layer_max_power = 70.0
            self._assembling = True

            try:
                self._replay_lines(gluescript)
                if require_complete and not self._job_complete:
                    raise RuntimeError(
                        "Re-staged gluescript is missing end_job() — job was not completed"
                    )
            finally:
                self._assembling = False

        # Finalization — require end_job() for non-re-staging path
        if gluescript is None and not self._job_complete:
            raise RuntimeError("end_job() must be called before stage_gluescript()")

        self._assemble_rpascript()
        return gluescript_signature(self.gluescript)

    def stage_gluescript_delta(
        self,
        flushed_count: int,
        delta_lines: list[str],
        require_complete: bool = True,
    ) -> str:
        """Replay only the newly appended transcript lines onto staged state.

        Incremental sibling of ``stage_gluescript()``: the server
        transcript already holds the first ``flushed_count`` lines
        (replayed by earlier deltas or a full stage), so only the appended
        suffix is replayed — O(Δ) per flush instead of O(L·N) over the
        whole job. No reset is performed; contiguity with the client's
        transcript is enforced by the ``flushed_count`` guard, which
        raises ``GlueScriptDeltaMismatchError`` when the server length
        differs (the client then falls back to a full
        ``stage_gluescript()`` re-stage).

        Args:
            flushed_count: The number of transcript lines the server
                already holds; must equal ``len(self.gluescript)``.
            delta_lines: The appended transcript lines to replay.
            require_complete: Raises when the replayed suffix never
                reached ``end_job()`` (mirrors the full re-stage path).

        Returns:
            str: The SHA-256 signature (hex) of the staged gluescript
                transcript. Failures raise RuntimeError instead of
                returning.
        """
        # Guard first (Law of Early Exit) — the delta is only contiguous
        # when the server already holds exactly the flushed prefix.
        if len(self.gluescript) != flushed_count:
            raise GlueScriptDeltaMismatchError(
                f"Server gluescript has {len(self.gluescript)} lines but "
                f"flushed_count is {flushed_count} — transcript out of "
                f"sync; full re-stage required"
            )
        # Re-arm the inline warning before the suffix replay: inline() in
        # the delta re-arms it, so the first delta containing inline
        # warns and later deltas do not.
        self._inline_used = False
        # Parallel to the _inline_used re-arm above: comment-only actions
        # warn once per delta over RPC.
        self._comment_only_used = False
        self._assembling = True
        try:
            self._replay_lines(delta_lines)
            if require_complete and not self._job_complete:
                raise RuntimeError(
                    "Re-staged gluescript is missing end_job() — job was not completed"
                )
        finally:
            self._assembling = False

        self._assemble_rpascript()
        return gluescript_signature(self.gluescript)
