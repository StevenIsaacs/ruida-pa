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
    # Home commands are the live-only non-jog commands. Any FUTURE live-only
    # command that is neither a jog nor a home must be added to
    # LIVE_ONLY_COMMANDS separately (e.g. LIVE_ONLY_COMMANDS = JOG_COMMANDS | {...}).
    # Register any future live-only command in BOTH LIVE_ONLY_COMMANDS and
    # registry_methods: the re-stage check consults the registry first, so a
    # missing registry entry would surface as "Unknown gluescript command"
    # instead of the live-only skip.
    LIVE_ONLY_COMMANDS: frozenset[str] = JOG_COMMANDS | HOME_COMMANDS

    def __init__(self) -> None:
        """Initialize GlueScript with empty scripts and default state."""
        # Script storage
        self.gluescript: list[str] = []
        
        # Job management state
        self._job_complete: bool = False
        self._job_header: list[str] = []    # Lines from declare_job()
        self._layer_attributes: dict[int, list[str]] = {}  # Attributes per layer
        self._layer_actions: dict[int, list[str]] = {}     # Actions per layer
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

        # Script output (assembled by stage_rpascript)
        self.rpascript: list[str] = []
        self._inline_used: bool = False
        # Caller-set toggle (per-instance): when False, the staging warning
        # for inline() use is suppressed (the TUI validates a throwaway
        # instance before applying to the real driver). Never reset by
        # new_gluescript() or the re-stage reset block — re-stage replays
        # declare_job(), which calls new_gluescript(); resetting here would
        # re-arm the flag mid-replay and defeat the suppression.
        self._warn_inline: bool = True
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
                "REF_POINT_ABSOLUTE",
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
                "REF_POINT_ANCHOR",
            ],
        }
        
        self._layer_modes: dict[str, str] = {
            "VECTOR": "NONE",
            "RASTER": "",
            "DITHER": "",
            "IMAGE": "",
            "DEPTHMAP": "NONE",
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
            "air_assist_on",
            "air_assist_off",
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
        ]
        for name in registry_methods:
            self._command_registry[name] = getattr(self, name)

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
        self._job_complete = False
        self._assembling = False
        self._inline_used = False
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
        # rpascript is assembled by stage_rpascript() — clear to empty
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
            if self._job_complete:
                # After end_job() — lands just before END_JOB.
                self._inline_epilogue.append(cmd)
            elif self._layer >= 1:
                # Inside a declared layer — lands in that layer's action
                # block at the call position. Never route to layer 0.
                self._layer_actions.setdefault(self._layer, []).append(cmd)
            else:
                # Job declared but no layer yet — lands right after the
                # job header.
                self._inline_prelude.append(cmd)

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

        # rpascript — job header (assembled later by stage_rpascript)
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
        if self._layer > 0 and self._last_layer_has_content:
            self._layer_attributes[self._layer].append(
                f"LAYER_TOP_RIGHT Layer:{self._layer} X={self._layer_trx:.3f}mm Y={self._layer_try:.3f}mm"
            )
            self._layer_attributes[self._layer].append(
                f"LAYER_BOTTOM_LEFT Layer:{self._layer} X={self._layer_blx:.3f}mm Y={self._layer_bly:.3f}mm"
            )

        self._job_complete = True
        self._assembling = False
        self.gluescript.append("end_job()")

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

        # Validate power range
        if min_power_1 < 8.0:
            raise ValueError(
                f"Minimum power {min_power_1}% is below 8% — CO2 laser "
                f"will not reliably fire below this threshold"
            )
        if max_power_1 > 70.0:
            logger.warning(
                "Maximum power %s%% exceeds 70%% — CO2 laser tube life "
                "is reduced at higher power settings", max_power_1
            )

        # Resolve overscan from mode override
        resolved_overscan = overscan
        mode_override = self._layer_modes[mode]
        if mode_override:
            resolved_overscan = mode_override

        # Snapshot previous layer's bbox with concrete values
        if self._layer > 0 and self._last_layer_has_content:
            self._layer_attributes.setdefault(self._layer, []).append(
                f"LAYER_TOP_RIGHT Layer:{self._layer} X={self._layer_trx:.3f}mm Y={self._layer_try:.3f}mm"
            )
            self._layer_attributes[self._layer].append(
                f"LAYER_BOTTOM_LEFT Layer:{self._layer} X={self._layer_blx:.3f}mm Y={self._layer_bly:.3f}mm"
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

        # gluescript (positional args only — matches _parse_gluescript_line)
        self.gluescript.append(
            f"declare_layer({label!r}, {color!r}, {mode!r}, "
            f"{overscan!r}, {speed!r}, {frequency!r}, "
            f"{min_power_1!r}, {max_power_1!r})"
        )

        # rpascript — store in _layer_attributes (assembled later by stage_rpascript)
        attrs: list[str] = []
        attrs.append(f"# Layer {self._layer}: {label}")
        # Escape '#' so the rpascript interpreter's inline-comment stripping
        # does not eat the color value (matches rpascript/generator.py).
        attrs.append(f"LAYER_COLOR Layer:{self._layer} Color:{color.replace('#', '\\#')}")
        attrs.extend(self._overscan_modes[resolved_overscan])
        attrs.append(
            f"LAYER_SPEED_LASER_1 Layer:{self._layer} Speed:{speed}mm/S"
        )
        attrs.append(
            f"LAYER_MIN_POWER_1 Layer:{self._layer} Power:{min_power_1}%"
        )
        attrs.append(
            f"LAYER_MAX_POWER_1 Layer:{self._layer} Power:{max_power_1}%"
        )
        attrs.append(f"LAYER_ATTRIBUTES Layer:{self._layer} 0")
        self._layer_attributes[self._layer] = attrs


        # Action boundary signal (for RPC batch sending)
        self._on_action_boundary()

    # ------------------------------------------------------------------ #
    #  Phase 4: Jogging & Homing Methods
    # ------------------------------------------------------------------ #

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

    def jog_xy_to(self, x: float, y: float) -> list[str]:
        """Generate jog rpascript to move XY to absolute coordinate.
        
        Returns:
            list[str]: rpascript lines for the jog command.
        """
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_XY Rel:MACHINE X={x}mm Y={y}mm",
        ]
        self._current_x = x
        self._current_y = y
        return lines

    def jog_x_to(self, x: float) -> list[str]:
        """Generate jog rpascript to move X to absolute coordinate."""
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_X Rel:MACHINE X={x}mm",
        ]
        self._current_x = x
        return lines

    def jog_y_to(self, y: float) -> list[str]:
        """Generate jog rpascript to move Y to absolute coordinate."""
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_Y Rel:MACHINE Y={y}mm",
        ]
        self._current_y = y
        return lines

    def jog_z_to(self, z: float) -> list[str]:
        """Generate jog rpascript to move Z to absolute coordinate.
        
        Warns if current Z is above 2000mm (suggesting Z home needed).
        """
        if z > 2000.0:
            logger.warning(
                "Requested Z position %.3fmm is above 2000mm. "
                "A Z home may be required to establish the correct home position.",
                z,
            )
            return []
        lines = [
            f"SPEED_LASER_1 {self.jog_z_speed}",
            f"JOG_Z Rel:MACHINE Z={z}mm",
        ]
        self._current_z = z
        return lines

    def jog_u_to(self, u: float) -> list[str]:
        """Generate jog rpascript to move U to absolute coordinate."""
        lines = [
            f"SPEED_LASER_1 {self.jog_u_speed}",
            f"JOG_U Rel:MACHINE U={u}mm",
        ]
        self._current_u = u
        return lines

    def jog_xy_rel(self, x: float | None = None, y: float | None = None) -> list[str]:
        """Generate jog rpascript to move XY relative to current position.
        
        When x or y is None, the configured jog distance (x_rel / y_rel) is used.
        """
        if x is None:
            x = self.x_rel
        if y is None:
            y = self.y_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_XY Rel:CURRENT X={x}mm Y={y}mm",
        ]
        self._current_x += x
        self._current_y += y
        return lines

    def jog_x_rel(self, x: float | None = None) -> list[str]:
        """Generate jog rpascript to move X relative to current position."""
        if x is None:
            x = self.x_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_X Rel:CURRENT X={x}mm",
        ]
        self._current_x += x
        return lines

    def jog_y_rel(self, y: float | None = None) -> list[str]:
        """Generate jog rpascript to move Y relative to current position."""
        if y is None:
            y = self.y_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_xy_speed}",
            f"JOG_Y Rel:CURRENT Y={y}mm",
        ]
        self._current_y += y
        return lines

    def jog_z_rel(self, z: float | None = None) -> list[str]:
        """Generate jog rpascript to move Z relative to current position.
        
        A positive distance moves the table down.
        """
        if z is None:
            z = self.z_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_z_speed}",
            f"JOG_Z Rel:CURRENT Z={z}mm",
        ]
        self._current_z += z
        return lines

    def jog_u_rel(self, u: float | None = None) -> list[str]:
        """Generate jog rpascript to move U relative to current position."""
        if u is None:
            u = self.u_rel
        lines = [
            f"SPEED_LASER_1 {self.jog_u_speed}",
            f"JOG_U Rel:CURRENT U={u}mm",
        ]
        self._current_u += u
        return lines

    def update_position(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        u: float | None = None,
    ) -> None:
        """Sync tracked position from controller-reported coordinates.

        Called when a live session receives MEM_CURRENT_POSITION replies.
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

    def home(self) -> list[str]:
        """Generate rpascript to home the X and Y axes.

        Returns:
            list[str]: rpascript lines for the home command.
        """
        return ["HOME_XY"]

    def home_z(self) -> list[str]:
        """Generate rpascript to home the Z axis.

        Returns:
            ["HOME_Z"] as rpascript to home the Z axis.
        """
        return ["HOME_Z"]

    def home_u(self) -> list[str]:
        """Generate rpascript to home the U axis (rotary).

        Returns:
            ["HOME_U"] as rpascript to home the U axis (rotary).
        """
        return ["HOME_U"]

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
            f"MOVE_{form}_XY X={x:.3f}mm Y={y:.3f}mm"
        )
        self._current_x = x
        self._current_y = y
        self._expand_bounding_boxes(x, y)

    def move_x_to(self, x: float) -> None:
        """Move to absolute X coordinate relative to job reference point."""
        delta_x = x - self._current_x
        form = self._choose_move_form(delta_x)
        self.gluescript.append(f"move_x_to({x!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"MOVE_{form}_X X={x:.3f}mm")
        self._current_x = x
        self._expand_bounding_boxes(x, self._current_y)

    def move_y_to(self, y: float) -> None:
        """Move to absolute Y coordinate relative to job reference point."""
        delta_y = y - self._current_y
        form = self._choose_move_form(delta_y)
        self.gluescript.append(f"move_y_to({y!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"MOVE_{form}_Y Y={y:.3f}mm")
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
            f"CUT_{form}_XY X={x:.3f}mm Y={y:.3f}mm"
        )
        self._current_x = x
        self._current_y = y
        self._expand_bounding_boxes(x, y)

    def cut_x_to(self, x: float) -> None:
        """Cut to absolute X coordinate relative to job reference point."""
        delta_x = x - self._current_x
        form = self._choose_move_form(delta_x)
        self.gluescript.append(f"cut_x_to({x!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"CUT_{form}_X X={x:.3f}mm")
        self._current_x = x
        self._expand_bounding_boxes(x, self._current_y)

    def cut_y_to(self, y: float) -> None:
        """Cut to absolute Y coordinate relative to job reference point."""
        delta_y = y - self._current_y
        form = self._choose_move_form(delta_y)
        self.gluescript.append(f"cut_y_to({y!r})")
        self._layer_actions.setdefault(self._layer, []).append(f"CUT_{form}_Y Y={y:.3f}mm")
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
        the final rpascript by stage_rpascript().
        """
        self._layer_actions.setdefault(layer, []).extend(lines)

    def stage_rpascript(self, gluescript: list[str] | None = None) -> list[str]:
        """Finalize the rpascript or re-stage a gluescript.
        
        Assembles rpascript from structured storage: job header first,
        then all layer attributes, then all layer actions with SELECT_LAYER
        prefix, then END_JOB.
        """
        # Re-staging path — skip _job_complete check; gluescript will set it
        if gluescript is not None:
            # Reset everything and replay gluescript through registry
            self._job_header = []
            self._layer_attributes = {}
            self._layer_actions = {}
            self._job_complete = False
            self._layer = 0
            self._inline_used = False
            self._inline_prelude = []
            self._inline_epilogue = []
            self._layer_trx = float('inf')
            self._layer_try = float('inf')
            self._layer_blx = -float('inf')
            self._layer_bly = -float('inf')
            self._last_layer_has_content = False
            self._assembling = True

            try:
                for line in gluescript:
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
                            "(jog and home commands act on the live session and are not part of a saved job)",
                            name,
                        )
                        continue
                    try:
                        self._command_registry[name](*args)
                    except Exception as exc:
                        raise RuntimeError(
                            f"Error re-staging command {name!r} with args {args!r}: {exc}"
                        ) from exc

                if not self._job_complete:
                    raise RuntimeError(
                        "Re-staged gluescript is missing end_job() — job was not completed"
                    )
            finally:
                self._assembling = False

        # Finalization — require end_job() for non-re-staging path
        if gluescript is None and not self._job_complete:
            raise RuntimeError("end_job() must be called before stage_rpascript()")

        # ── Assemble rpascript from structured storage ──
        self.rpascript = []

        # Section 1: Job header (reference point, start_job, settings)
        self.rpascript.extend(self._job_header)

        # Inline commands issued before the first layer land right after
        # the job header, preserving their call position.
        self.rpascript.extend(self._inline_prelude)

        # Section 2: All layer attributes (sorted by layer number)
        for layer_num in sorted(self._layer_attributes):
            self.rpascript.extend(self._layer_attributes[layer_num])

        # Section 3: All layer actions with SELECT_LAYER prefix
        for layer_num in sorted(self._layer_actions):
            self.rpascript.append(f"SELECT_LAYER Layer:{layer_num}")
            self.rpascript.extend(self._layer_actions[layer_num])

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
        return finalized
