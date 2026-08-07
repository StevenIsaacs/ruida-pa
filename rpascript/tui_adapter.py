"""
L7 TuiAdapter — Textual-based TUI for interactive Ruida script execution.

Provides a terminal user interface for connecting to Ruida laser controllers,
executing rpascript commands interactively, and monitoring status/reply events
in real-time via the AppAdapter → RdDriver → RdSession stack.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import inspect
import json
import logging
import math
import os
import re
import sys
import types
import threading
import gc
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import argparse
from rpalib.rpa_emitter import RpaEmitter
from rpalib.version import __version__
try:
    from rpalib.bokeh_app import BokehApp
except ImportError:
    BokehApp = None
from protocols.ruida.ruida_analyzer import RuidaProtocolAnalyzer
from protocols.ruida.ruida_parser import RdParser
from rpalib.rd_binary_reader import RdBinaryStream
from rpascript.generator import ScriptGenerator
from rpalib.rpa_swizzler import RpaSwizzler

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Callback, Key
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Header, Input, RichLog, Static, TextArea

from rpalib.ruida_transcoder import RdDecoder, RdEncoder
from rpascript.encoding import encode_command, is_resolvable_address, parse_value
from rpascript.interpreter import ScriptParser, reconstruct_script_line
from ruidadriver.rd_status import RdStatusEvent
from ruidadriver.ruida_driver import RdDriver, StatusDict
from ruidadriver.rd_gluescript import GlueScript

from rpyc.utils.server import ThreadedServer

_log = logging.getLogger(__name__)


def _parse_timeout_spec(to_str: str) -> float:
    """Parse a timeout spec like '5s' or '5000ms' into seconds (float).

    Raises ValueError on invalid format.
    """
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s)", to_str.strip())
    if not match:
        raise ValueError(f"Invalid timeout format: '{to_str}'. Use e.g., 5s, 500ms")
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "ms":
        return value / 1000.0
    return value


class ErrorScreen(ModalScreen):
    """Modal screen that displays a crash traceback and waits for keypress."""

    CSS = """
    ErrorScreen {
        align: center middle;
    }
    #error-box {
        width: 80%;
        height: 80%;
        border: thick $error;
        background: $surface;
    }
    #error-title {
        padding: 1 2;
        text-style: bold;
        background: $error;
        color: $text;
    }
    #error-detail {
        height: 1fr;
        padding: 1 2;
    }
    #error-footer {
        padding: 1 2;
        text-style: dim;
        text-align: center;
    }
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="error-box"):
            yield Static("⚠ Application Crashed", id="error-title")
            yield RichLog(id="error-detail", highlight=True, markup=True)
            yield Static("Press Escape to exit.", id="error-footer")

    def on_mount(self) -> None:
        """Render the traceback into the detail panel."""
        from rich.traceback import Traceback

        detail = self.query_one("#error-detail", RichLog)
        tb = Traceback.from_exception(
            type(self._error),
            self._error,
            self._error.__traceback__,
        )
        detail.write(tb)

    def on_key(self, event: Key) -> None:
        """Escape exits the app; all other keys are consumed to keep the screen open."""
        if event.key == "escape":
            self.app.exit(return_code=1)
        else:
            event.stop()


class ScriptEditor(ModalScreen):
    """Full-screen text editor for the loaded script.

    Opens with the current _loaded_script content as editable text.
    Ctrl+S saves (strips blank lines, updates _loaded_script).
    Escape cancels (discards changes). An optional title is shown in
    the editor header.
    """

    CSS = """
    ScriptEditor {
        align: center middle;
    }
    #editor-box {
        width: 90%;
        height: 90%;
        border: thick $primary;
        background: $surface;
    }
    #editor-title {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $primary-darken-1;
        text-style: bold;
        content-align: left middle;
    }
    #find-bar {
        dock: top;
        height: 3;
        padding: 0 1;
        background: $surface;
        display: none;
    }
    #find-bar.visible {
        display: block;
    }
    #find-input {
        width: 30;
    }
    #find-info {
        width: auto;
        min-width: 10;
        content-align: center middle;
    }
    #editor-area {
        height: 1fr;
    }
    #editor-footer {
        dock: bottom;
        height: 3;
        padding: 0 1;
        background: $surface;
        content-align: center middle;
    }
    """

    BINDINGS = [
        ("ctrl+s", "save", "Save"),
        ("ctrl+f", "find", "Find"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, initial_text: str, title: str | None = None) -> None:
        super().__init__()
        self._initial = initial_text
        self._title = title
        self._find_matches: list[tuple[int, int]] = []
        self._find_index: int = -1
        self._find_active: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-box"):
            if self._title:
                yield Static(self._title, id="editor-title")
            with Horizontal(id="find-bar"):
                yield Input(placeholder="Find...", id="find-input")
                yield Static("", id="find-info")
            yield TextArea(self._initial, id="editor-area", language="python")
            yield Static(
                "  \\[Ctrl+S] Save  \\[Ctrl+F] Find  \\[Esc] Cancel  ",
                id="editor-footer",
            )

    def action_save(self) -> None:
        """Save edited text and dismiss."""
        text = self.query_one("#editor-area", TextArea).text
        lines = [line for line in text.splitlines() if line.strip()]
        self.dismiss(lines)

    def action_cancel(self) -> None:
        """Discard changes and dismiss."""
        self.dismiss(None)

    # ------------------------------------------------------------------
    # Find (Ctrl+F)
    # ------------------------------------------------------------------

    def action_find(self) -> None:
        """Toggle the find bar visibility."""
        find_bar = self.query_one("#find-bar")
        if self._find_active:
            find_bar.remove_class("visible")
            self._find_active = False
            self.query_one("#editor-area", TextArea).focus()
        else:
            find_bar.add_class("visible")
            self._find_active = True
            self.query_one("#find-input", Input).focus()

    def on_key(self, event: Key) -> None:
        """Handle keys for find bar navigation."""
        if not self._find_active:
            return
        if event.key == "escape":
            find_bar = self.query_one("#find-bar")
            find_bar.remove_class("visible")
            self._find_active = False
            self.query_one("#editor-area", TextArea).focus()
            event.stop()

    @on(Input.Changed, "#find-input")
    def _on_find_changed(self, event: Input.Changed) -> None:
        """Update search matches as user type."""
        query = event.value
        textarea = self.query_one("#editor-area", TextArea)
        info = self.query_one("#find-info", Static)

        if not query:
            self._find_matches = []
            self._find_index = -1
            info.update("")
            return

        # Find all case-insensitive matches in the full text
        text = textarea.text
        lower_text = text.lower()
        lower_query = query.lower()
        matches: list[tuple[int, int]] = []
        start = 0
        while True:
            idx = lower_text.find(lower_query, start)
            if idx == -1:
                break
            matches.append((idx, idx + len(query)))
            start = idx + 1

        self._find_matches = matches

        if matches:
            self._find_index = 0
            self._highlight_match(textarea)
            info.update(f"1 of {len(matches)}")
        else:
            self._find_index = -1
            info.update("No matches")

    @on(Input.Submitted, "#find-input")
    def _on_find_submit(self, _event: Input.Submitted) -> None:
        """Navigate to next match on Enter."""
        self._find_next()

    def _find_next(self) -> None:
        """Move to the next match."""
        if not self._find_matches:
            return
        self._find_index = (self._find_index + 1) % len(self._find_matches)
        self._highlight_match(self.query_one("#editor-area", TextArea))
        self.query_one("#find-info", Static).update(
            f"{self._find_index + 1} of {len(self._find_matches)}"
        )

    def _find_prev(self) -> None:
        """Move to the previous match."""
        if not self._find_matches:
            return
        self._find_index = (self._find_index - 1) % len(self._find_matches)
        self._highlight_match(self.query_one("#editor-area", TextArea))
        self.query_one("#find-info", Static).update(
            f"{self._find_index + 1} of {len(self._find_matches)}"
        )

    @staticmethod
    def _compute_location(text: str, offset: int) -> tuple[int, int]:
        """Convert a flat character offset to (row, col)."""
        row = text.count("\n", 0, offset)
        last_nl = text.rfind("\n", 0, offset)
        col = offset - last_nl - 1 if last_nl >= 0 else offset
        return (row, col)

    def _highlight_match(self, textarea: TextArea) -> None:
        """Select and scroll to the current match."""
        if self._find_index < 0 or self._find_index >= len(self._find_matches):
            return
        start, end = self._find_matches[self._find_index]
        text = textarea.text
        start_loc = self._compute_location(text, start)
        end_loc = self._compute_location(text, end)
        textarea.move_cursor(start_loc, select=False)
        textarea.move_cursor(end_loc, select=True, center=True)


def _deep_getsizeof(obj: Any, seen: set[int] | None = None, _depth: int = 500, _level: int = 0) -> tuple[int, int]:
    """Recursively compute deep memory footprint of an object.

    Walks __dict__, __slots__, and container items (dict, list, tuple, set)
    to sum sys.getsizeof for the object and all objects it transitively
    references.  Stops recursion at primitive types (int, float, str, bytes,
    bool, NoneType) and shared runtime types (type, ModuleType, etc.).

    Uses id()-based cycle detection via the *seen* set.

    Args:
        obj: The object to measure.
        seen: Set of object ids already visited (for cycle detection).

    Returns:
        Tuple of (total deep size in bytes, maximum walk depth level reached).
    """
    _PRIMITIVE_TYPES = (int, float, str, bytes, bool, type(None))
    _STOP_TYPES = (
        type,
        types.ModuleType,
        types.FunctionType,
        types.BuiltinFunctionType,
        types.BuiltinMethodType,
        types.MethodType,
        types.CodeType,
        types.FrameType,
        types.TracebackType,
        types.GeneratorType,
    )

    if seen is None:
        seen = set()

    obj_id = id(obj)
    if obj_id in seen:
        return (0, _level)
    seen.add(obj_id)

    # Base size of the object itself
    try:
        total = sys.getsizeof(obj)
    except (TypeError, AttributeError):
        total = 0

    # Stop recursion at primitives, shared runtime types, or depth limit
    if isinstance(obj, _PRIMITIVE_TYPES + _STOP_TYPES):
        return (total, _level)
    if _depth <= 0:
        return (total, _level)

    # Walk based on container type
    max_depth = _level
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            ks, kd = _deep_getsizeof(k, seen, _depth - 1, _level + 1)
            vs, vd = _deep_getsizeof(v, seen, _depth - 1, _level + 1)
            total += ks + vs
            max_depth = max(max_depth, kd, vd)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in list(obj):
            item_s, item_d = _deep_getsizeof(item, seen, _depth - 1, _level + 1)
            total += item_s
            max_depth = max(max_depth, item_d)

    # Walk instance attributes via __dict__ and __slots__
    if hasattr(obj, '__dict__') and obj.__dict__ is not None:
        d_s, d_d = _deep_getsizeof(obj.__dict__, seen, _depth - 1, _level + 1)
        total += d_s
        max_depth = max(max_depth, d_d)

    for _cls in type(obj).__mro__:
        slots = getattr(_cls, '__slots__', ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            if slot == '__dict__':
                continue  # Already handled above
            if hasattr(obj, slot):
                try:
                    val = getattr(obj, slot)
                    v_s, v_d = _deep_getsizeof(val, seen, _depth - 1, _level + 1)
                    total += v_s
                    max_depth = max(max_depth, v_d)
                except (AttributeError, TypeError):
                    continue

    return (total, max_depth)


class TuiAdapter(App):
    """Textual-based TUI for interactive Ruida script execution.

    Implements the AppAdapter interface (duck-typing compatible) combined with
    Textual's App (TUI framework) to provide a terminal UI for connecting to
    Ruida controllers, executing rpascript commands, and monitoring status/reply
    events in real-time.

    Usage::
        app = TuiAdapter()
        app.run()  # Blocks until user quits
    """

    TITLE = f"Ruida Script TUI v{__version__}"
    SUB_TITLE = "Interactive Ruida Controller Interface"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "stop", "Stop"),
        ("page_up", "scroll_log_up", "Scroll log up"),
        ("page_down", "scroll_log_down", "Scroll log down"),
    ]

    _SLASH_COMMANDS: tuple[str, ...] = (
        "help",
        "load",
        "run",
        "clear",
        "quit",
        "status",
        "head",
        "import",
        "export",
        "tail",
        "list",
        "save",
        "stop",
        "dryrun",
        "edit",
        "frame",
        "plot",
        "protect",
        "rpclog",
        "gluescript",
        "monitor",
        "scan_mem",
    )
    # Only _cmd_descriptions and the /help block (usage text) stay
    # hand-maintained for live-only commands; recognition
    # (GlueScript.LIVE_ONLY_COMMANDS = JOG_COMMANDS | HOME_COMMANDS) and
    # this autocomplete list stay in sync automatically.
    _NORMAL_COMMANDS: tuple[str, ...] = ("session", "server") + tuple(
        sorted(GlueScript.LIVE_ONLY_COMMANDS)
    )

    CSS = """
    #main-container {
        height: 1fr;
    }

    #log-panel {
        width: 3fr;
        border-right: solid $primary;
    }

    #log-area {
        height: 1fr;
    }

    #command-input {
        dock: bottom;
        height: 3;
    }

    #side-panel {
        width: 1fr;
    }

    #status-log {
        text-style: dim;
    }

    #status-log {
        height: 1fr;
        border-bottom: solid $surface;
    }

    #reply-log {
        height: 1fr;
        border-bottom: solid $surface;
        padding: 1 2;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $surface;
        text-style: dim;
    }

    #suggest-popup {
        height: auto;
        max-height: 10;
        border-top: solid $primary;
        background: $panel;
        overflow-y: auto;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ruida_driver: RdDriver | None = None
        self._last_udp_host: str = ""
        self._last_usb_device: str = ""
        self._last_magic: int = 0x88
        self._parser = ScriptParser(
            warning_callback=lambda msg, syn: self._log_warning(
                f"{escape(msg)}  |  Syntax: {escape(syn)}"
            ),
        )
        self._decoder = RdDecoder()
        self._event_count = 0
        self._reply_count = 0
        self._script_count = 0
        self._logging_enabled: bool = True
        self._status_log_buffer: deque[str] = deque()
        self._introspect_map: dict[str, Callable[[], Any]] = {
            "session": lambda: self._ruida_driver,
            "transport": lambda: (
                self._ruida_driver._session.transport if self._ruida_driver._session else None
            ),
            "driver": lambda: self._ruida_driver,
            "status": lambda: (
                self._ruida_driver._session.status if self._ruida_driver._session else None
            ),
            "parser": lambda: self._parser,
            "decoder": lambda: self._decoder,
            "rpc": lambda: self._rpyc_server,
        }
        self._loaded_script: list[str] = []
        self._head_script: list[str] = []
        self._tail_script: list[str] = []
        self._session_connected = asyncio.Event()
        self._session_start_cancel = asyncio.Event()
        self._last_server_host: str = "localhost"
        self._last_server_port: int = 18812
        self._last_server_cert: str | None = None
        self._last_server_key: str | None = None
        self._last_server_token: str | None = None
        self._dryrun: bool = False
        self._auto_display_script: bool = False
        self._plot_source: str | None = None  # source label for /plot title (filename or "[RPC]")
        self._loaded_script_path: str | None = None  # Full path of last /load-ed file, for /save preselect
        self._gluescript_cglu_path: str | None = None  # Full path of last saved/loaded .cglu file, for /gluescript preselect
        self._preserved_gluescript: list[str] | None = None  # Transcript preserved across session teardown, re-staged on next driver creation
        self._bokeh_apps: list[BokehApp] = []  # running Bokeh servers for /clear shutdown
        self._gluescript_was_run: bool = False  # Tracks if staged gluescript has been executed
        self._rpyc_server: ThreadedServer | None = None
        # Serializes session-less GlueScript RPC delegates when the app is
        # not running (no TUI event loop to marshal onto).
        self._gluescript_lock = threading.Lock()
        self._suggest_popup = RichLog(
            id="suggest-popup", highlight=True, markup=True, max_lines=10
        )
        # Square-bracket optionals are Rich markup escapes: \[ renders a literal [. Keep new optional-parameter segments escaped.
        self._cmd_descriptions: dict[str, str] = {
            "help": "Show help text",
            "load": "Load a script file from disk",
            "run": "Execute job from loaded script (/run script for all lines)",
            "clear": "Clear all log panels, loaded script, head, and tail",
            "quit": "Exit the TUI",
            "status": "Toggle logging: /status \[on|off|status] for status/reply, /status connection \[on|off|status] for transport events",
            "session": "Start or end a controller session (start udp=<IP> usb=<device> to=<timeout> magic=0xNN / end)",
            "server": "Start or stop the RPC server. "
            "Server commands: start host=<IP> port=<N> cert=<path> key=<path> token=<token>, or stop",
            "head": "Load a script file to prepend to job on execution",
            "import": "Import a tshark log (.log) or RDWorks (.rd) file \[magic=0xNN] as a script",
            "export": "Export loaded script as .rd binary file (/export \[path])",
            "tail": "Load a script file to append to job on execution",
            "list": "Display loaded script (/list script), composed job (/list job), head (/list head), tail (/list tail), or toggle auto-display (/list auto \[on|off])",
            "save": "Save composed job (/save job <path>) or full script (/save script <path> | /save as <path>)",
            "stop": "Stop the current operation (session connection or script execution). Also bound to Escape.",
            "dryrun": "Toggle dry-run mode (on|off). When on, /run runs normally but RPC driver.run() only logs to TUI.",
            "edit": "Open loaded rpascript in a full-screen editor",
            "protect": "Toggle protect mode (on|off|status). When on, SET_SETTING commands are blocked to prevent hardware damage.",
            "rpclog": "Toggle RPC server logging: /rpclog \[on|off|status] (no args toggles)",
            "frame": "Frame job or layer boundaries. /frame job | /frame layer <N>",
            "plot": "Plot loaded script moves in a Bokeh visualization",
            "monitor": "Monitor memory and GC stats. /monitor on|off to toggle auto-update (15s), /monitor for immediate update",
            "scan_mem": "Generate a GET_SETTING script for all MT memory addresses",
            "gluescript": "GlueScript high-level scripting. Subcommands: new, show, stage, run, save, load, edit, list",
            "home": "home: Home X and Y axes (machine origin)",
            "home_z": "home_z: Home Z axis",
            "home_u": "home_u: Home U axis (rotary)",
            "jog_xy_to": "jog_xy_to <x> <y>: Jog XY to absolute position (mm)",
            "jog_x_to": "jog_x_to <x>: Jog X to absolute position (mm)",
            "jog_y_to": "jog_y_to <y>: Jog Y to absolute position (mm)",
            "jog_z_to": "jog_z_to <z>: Jog Z to absolute position (mm, max 2000)",
            "jog_u_to": "jog_u_to <u>: Jog U to absolute position (mm)",
            "jog_xy_rel": "jog_xy_rel \[x] \[y]: Jog XY relative (uses configured defaults)",
            "jog_x_rel": "jog_x_rel \[x]: Jog X relative (uses configured default)",
            "jog_y_rel": "jog_y_rel \[y]: Jog Y relative (uses configured default)",
            "jog_z_rel": "jog_z_rel \[z]: Jog Z relative (uses configured default)",
            "jog_u_rel": "jog_u_rel \[u]: Jog U relative (uses configured default)",
            "jog_set_xy_speed": "jog_set_xy_speed <speed>: Set XY jog speed (mm/s)",
            "jog_set_z_speed": "jog_set_z_speed <speed>: Set Z jog speed (mm/s)",
            "jog_set_u_speed": "jog_set_u_speed <speed>: Set U jog speed (mm/s)",
            "jog_set_xy_rel": "jog_set_xy_rel <delta>: Set relative XY jog distance (mm)",
            "jog_set_z_rel": "jog_set_z_rel <delta>: Set relative Z jog distance (mm)",
            "jog_set_u_rel": "jog_set_u_rel <delta>: Set relative U jog distance (mm)",
        }
        self._suggest_matches: list[str] = []
        self._suggest_selected: int = 0
        self._suggest_mode: str = ""  # 'slash', 'introspect', 'file', or '' when no popup
        self._suppress_popup: bool = (
            False  # Suppress on_input_changed for programmatic value changes
        )
        self._file_completions: list[tuple[str, bool]] = []  # (name, is_dir) pairs
        self._file_completion_dir: str = ""  # Current directory being browsed
        self._file_completion_cmd: str = ""  # Command that triggered completion (e.g., \"/load\")
        self._file_completion_prefix: str = ""  # Filename prefix typed so far
        self._command_history: list[str] = []
        self._history_index: int | None = None
        self._position: dict[str, tuple | None] = {
            "X": None,
            "Y": None,
            "Z": None,
            "U": None,
            "Card": None,
            "BedX": None,
            "BedY": None,
        }
        self._last_coord_change: dict[str, float] = {
            "X": 0.0,
            "Y": 0.0,
            "Z": 0.0,
            "U": 0.0,
        }
        self._session_disconnected: bool = False
        self._machine_status: int = 0
        self._machine_status_formatted: str = "0"
        self._connection_logging_enabled: bool = False
        self._status_bits: dict[str, bool] = {
            "MACHINE_STATUS_MOVING": False,
            "MACHINE_STATUS_LAYER_END": False,
            "MACHINE_STATUS_JOB_RUNNING": False,
        }
        # Memory monitor state
        self._mem_prev: dict[str, int] | None = None
        self._mem_initial: dict[str, int] = {}
        self._mem_timer: Any = None
        self._monitor_enabled: bool = False

        # GC object counter state
        self._gc_prev: dict[str, tuple[int, int, int]] | None = None
        self._gc_initial: dict[str, tuple[int, int, int]] = {}
        
        # Track connection status changes
        self._last_is_connected: bool | None = None

    # ------------------------------------------------------------------
    # Textual App lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Create the TUI layout widgets."""
        yield Header()
        with Horizontal(id="main-container"):
            with Vertical(id="log-panel"):
                yield RichLog(
                    id="log-area", highlight=True, markup=True, max_lines=1000
                )
                yield Input(
                    id="command-input",
                    placeholder="> Enter command (session start/end, or rpascript)...",
                )
            with Vertical(id="side-panel"):
                yield RichLog(
                    id="status-log", highlight=True, markup=True, max_lines=50
                )
                yield Static(id="reply-log", markup=True)
        yield Static(id="status-bar")

    def on_mount(self) -> None:
        """Widgets are ready — cache references, load history, and log startup message."""
        self._log_widget = self.query_one("#log-area", RichLog)
        self._status_log = self.query_one("#status-log", RichLog)
        self._reply_log = self.query_one("#reply-log", Static)
        self._status_bar = self.query_one("#status-bar", Static)
        # Restrict focus to command input only — Tab stays on Input
        self._log_widget.can_focus = False
        self._status_log.can_focus = False
        self._update_status_bar()
        self._load_command_history()
        self.query_one("#command-input", Input).focus()

    # ------------------------------------------------------------------
    # Command input handling
    # ------------------------------------------------------------------

    def _render_suggest_popup(self) -> None:
        """Render the suggestion popup with current selection highlighted.

        Only shows items within a visible window around the selected item to
        keep the selection on screen when the list exceeds max_lines.
        """
        self._suggest_popup.clear()
        if self._suggest_mode == "file":
            # File completions render from _file_completions and populate
            # _suggest_matches themselves. They must run even when
            # _suggest_matches is empty — two-word file commands (e.g.
            # /gluescript save, /save job) clear the slash-mode matches when
            # the space after the first word is typed.
            self._render_file_completions()
            return
        if not self._suggest_matches:
            self._suggest_popup.write("[dim]No matching commands[/dim]")
            return

        # Compute visible window centered on selected item
        max_items = self._suggest_popup.max_lines - 1  # reserve 1 line for header
        total = len(self._suggest_matches)
        half = max_items // 2
        start = max(0, self._suggest_selected - half)
        end = min(total, start + max_items)
        # If we're below max_items, shift window up
        if end - start < max_items:
            start = max(0, end - max_items)

        if self._suggest_mode == "slash":
            self._suggest_popup.write("[bold]Commands:[/bold]")
            for i in range(start, end):
                cmd = self._suggest_matches[i]
                line = f"  /{cmd:<12} {self._cmd_descriptions[cmd]}"
                if i == self._suggest_selected:
                    self._suggest_popup.write(f"[reverse]{line}[/reverse]")
                else:
                    self._suggest_popup.write(line)
        elif self._suggest_mode == "introspect":
            self._suggest_popup.write("[bold]Introspect:[/bold]")
            for i in range(start, end):
                obj = self._suggest_matches[i]
                line = f"  ?{obj}"
                if i == self._suggest_selected:
                    self._suggest_popup.write(f"[reverse]{line}[/reverse]")
                else:
                    self._suggest_popup.write(line)

    @on(Input.Submitted, "#command-input")
    async def on_command(self, event: Input.Submitted) -> None:
        """Handle command input submission from the user."""
        line = event.input.value.strip()
        event.input.clear()
        if not line:
            return

        # Add to command history (skip consecutive duplicates)
        if self._command_history and self._command_history[-1] == line:
            pass  # consecutive duplicate, skip
        else:
            self._command_history.append(line)
            if len(self._command_history) > 500:
                self._command_history.pop(0)
        self._history_index = None  # reset browsing position

        if self._suggest_popup.is_attached:
            self._suggest_popup.remove()
        self._clear_file_completion_state()

        # Introspection mode: ?<object>[.<attr>] [args...]
        if line.startswith("?"):
            expr = line[1:].strip()
            if not expr:
                # Just '?' — show available introspection objects
                known = ", ".join(sorted(self._introspect_map.keys()))
                self._log_widget.write("[bold]?[/bold]")
                self._log_info(f"Introspect: {known}")
                return
            result = self._handle_introspect(expr)
            self._log_widget.write(f"[bold]?{expr}[/bold]")
            self._log_info(result)
            return
        # Slash-prefixed TUI commands
        if line.startswith("/"):
            await self._handle_slash_command(line)
            return
        self._log_script(line)
        first_word = line.split(None, 1)[0]
        if first_word in GlueScript.LIVE_ONLY_COMMANDS:
            self._handle_live_command(line)
            return

        try:
            # Parse the line as a single rpascript command
            parsed = self._parser.parse_lines([line])
            if not parsed:
                return

            cmd = parsed[0]
            self._script_count += 1

            # Pre-encode regular commands to show wire-format bytes in the log
            if cmd["type"] not in ("SESSION_START", "SESSION_END", "SERVER_START", "SERVER_STOP", "DELAY", "WAIT"):
                try:
                    encoded = encode_command(
                        cmd,
                        self._parser.mnemonic_map,
                        self._parser._mt_map,
                        RdEncoder(),
                    )
                    hex_str = " ".join(f"{b:02X}" for b in encoded)
                    self._log_widget.write(
                        f"[dim]         ⇒ {hex_str} ({len(encoded)} bytes)[/dim]"
                    )
                except Exception as e:
                    self._log_error(f"Encoding failed: {e}")

            # Validate GET_SETTING commands have resolvable addresses
            if cmd.get("mnemonic", "") == "GET_SETTING":
                params = cmd.get("params", [])
                if not params or not self._is_resolvable_address(params[0]):
                    reason = (
                        f"unknown address: {params[0]}" if params else "missing address"
                    )
                    self._log_error(f"Invalid GET_SETTING: {reason}")
                    return

            if cmd["type"] == "SERVER_START":
                asyncio.create_task(self._start_server(**cmd["params"]))
            elif cmd["type"] == "SERVER_STOP":
                await self._stop_server()
            elif cmd["type"] == "SESSION_START":
                asyncio.create_task(self._start_session(**cmd["params"]))
            elif cmd["type"] == "SESSION_END":
                await self._stop_session()
            else:
                if self._ruida_driver is None:
                    self._log_error(
                        "No active session. Use 'session start udp=<IP> usb=<device>' first."
                    )
                    return
                try:
                    reconstructed = reconstruct_script_line(cmd)
                    self._ruida_driver.run([reconstructed])
                except RuntimeError as e:
                    self._log_error(str(e))
        except Exception as e:
            self._log_error(f"{type(e).__name__}: {e}")

    @on(Input.Changed, "#command-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Show/filter command popup as user types."""
        # Suppress popup for programmatic value changes (e.g., history recall)
        if self._suppress_popup:
            self._suppress_popup = False
            return
        value = event.value

        # --- File-path completion (checked before slash suggest) ---
        cmd, path_part = self._check_file_browse_trigger(value)
        if cmd:
            # File command detected — show/update file completions
            if self._suggest_mode != "file" or self._file_completion_cmd != cmd:
                # New file command or different command — fresh scan
                self._file_completion_cmd = cmd
                self._file_completions = self._get_file_completions(cmd, path_part)
            else:
                # Same command — check if directory changed
                if path_part and "/" in path_part:
                    dir_part = path_part.rsplit("/", 1)[0]
                else:
                    dir_part = ""
                resolved_dir = str(self._resolve_start_path(dir_part))
                if resolved_dir != self._file_completion_dir:
                    # Directory changed — full rescan
                    self._file_completions = self._get_file_completions(cmd, path_part)
                else:
                    # Just prefix changed — update for filtering only
                    self._file_completion_prefix = (
                        path_part.split("/")[-1] if "/" in path_part else path_part
                    )
            self._suggest_selected = 0
            self._suggest_mode = "file"
            if not self._suggest_popup.is_attached:
                self.query_one("#log-panel").mount(
                    self._suggest_popup, before="#command-input"
                )
            self._render_suggest_popup()
            return
        elif self._suggest_mode == "file":
            # Was in file mode but no longer a file command — clear
            self._clear_file_completion_state()

        # --- Slash commands (only if no space after command name) ---
        if value.startswith("/") and " " not in value:
            prefix = value[1:].strip()
            if not prefix:
                matches = list(self._SLASH_COMMANDS)
            else:
                matches = [
                    c for c in self._SLASH_COMMANDS if c.startswith(prefix)
                ]

            if not self._suggest_popup.is_attached:
                self.query_one("#log-panel").mount(
                    self._suggest_popup, before="#command-input"
                )
            if matches:
                self._suggest_matches = matches
                self._suggest_selected = 0
                self._suggest_mode = "slash"
            else:
                self._suggest_matches = []
                self._suggest_mode = ""
            self._render_suggest_popup()
            return

        # Introspection objects: ?<object>
        if value.startswith("?"):
            prefix = value[1:].strip()
            known = list(self._introspect_map.keys())
            if not prefix:
                matches = sorted(known)
            else:
                matches = sorted(k for k in known if k.startswith(prefix))

            if not self._suggest_popup.is_attached:
                self.query_one("#log-panel").mount(
                    self._suggest_popup, before="#command-input"
                )
            if matches:
                self._suggest_matches = matches
                self._suggest_selected = 0
                self._suggest_mode = "introspect"
            else:
                self._suggest_matches = []
                self._suggest_mode = ""
            self._render_suggest_popup()
            return

        # Normal commands (not introspection, not help query)
        if value and not value.startswith("?"):
            clean = value.strip()

            if " " in clean:
                # Space detected — lock to the matched command root, no more filtering
                root_cmd = clean.split(" ", 1)[0]
                if root_cmd in self._NORMAL_COMMANDS:
                    matches = [root_cmd]
                else:
                    matches = []
            else:
                # No space — filter by prefix match on the first word
                matches = [c for c in self._NORMAL_COMMANDS if c.startswith(clean)]

            if matches:
                if not self._suggest_popup.is_attached:
                    self.query_one("#log-panel").mount(
                        self._suggest_popup, before="#command-input"
                    )
                self._suggest_popup.clear()
                self._suggest_popup.write("[bold]Commands:[/bold]")
                for cmd in matches:
                    self._suggest_popup.write(
                        f"  {cmd:<22} {self._cmd_descriptions[cmd]}"
                    )
                return

        # No popup needed — remove if attached, clear any lingering file state
        if self._suggest_mode == "file":
            self._clear_file_completion_state()
        if self._suggest_popup.is_attached:
            self._suggest_popup.remove()
            self._suggest_matches = []
            self._suggest_mode = ""

    # ------------------------------------------------------------------
    # Command history (Up/Down navigation)
    # ------------------------------------------------------------------

    @on(Key)
    def on_command_key(self, event: Key) -> None:
        """Navigate command history with Up/Down arrow keys.

        Only responds when the command-input widget is focused and the
        command input's own screen is the current screen (no pushed
        screen — e.g. the editor modal — on top).
        """
        inp = self.query_one("#command-input", Input)
        if not inp.has_focus or self.screen is not inp.screen:
            return

        if event.key == "up":
            event.stop()
            # Navigate suggest popup (works for slash, introspect, and file modes)
            if self._suggest_popup.is_attached and self._suggest_matches:
                self._suggest_selected = (self._suggest_selected - 1) % len(
                    self._suggest_matches
                )
                self._render_suggest_popup()
                return
            if not self._command_history:
                return
            if self._history_index is None:
                self._history_index = len(self._command_history) - 1
            elif self._history_index > 0:
                self._history_index -= 1
            else:
                return  # already at oldest
            cmd = self._command_history[self._history_index]
            if self._suggest_popup.is_attached:
                self._suggest_popup.remove()
                self._suggest_matches = []
                self._suggest_mode = ""
            self._suppress_popup = True
            inp.value = cmd
            inp.cursor_position = len(cmd)

        elif event.key == "down":
            event.stop()
            if self._suggest_popup.is_attached and self._suggest_matches:
                self._suggest_selected = (self._suggest_selected + 1) % len(
                    self._suggest_matches
                )
                self._render_suggest_popup()
                return
            if self._history_index is None:
                return  # not browsing history
            if self._history_index < len(self._command_history) - 1:
                self._history_index += 1
                cmd = self._command_history[self._history_index]
            else:
                # At newest entry -> clear input
                self._history_index = None
                cmd = ""
            if self._suggest_popup.is_attached:
                self._suggest_popup.remove()
                self._suggest_matches = []
                self._suggest_mode = ""
            self._suppress_popup = True
            inp.value = cmd
            inp.cursor_position = len(cmd)

        elif event.key == "escape":
            if self._suggest_mode == "file" and self._suggest_popup.is_attached:
                event.stop()
                self._clear_file_completion_state()
                inp.focus()
                return

        elif event.key == "enter":
            """Confirm selection from suggest popup."""
            if self._suggest_popup.is_attached and self._suggest_matches:
                event.stop()
                selected = self._suggest_matches[self._suggest_selected]

                if self._suggest_mode == "file":
                    # File completion: build path relative to cwd
                    cwd = str(Path.cwd())
                    display_dir = self._file_completion_dir
                    if display_dir.startswith(cwd):
                        display_dir = "." + display_dir[len(cwd):]
                    if display_dir and display_dir != ".":
                        new_path = display_dir + "/" + selected
                    else:
                        new_path = selected
                    completed_val = self._file_completion_cmd + " " + new_path
                    self._suppress_popup = True
                    inp.focus()
                    inp.value = completed_val
                    self.post_message(Key("end", None))
                    self._clear_file_completion_state()
                    if self._suggest_popup.is_attached:
                        self._suggest_popup.remove()
                elif self._suggest_mode == "slash":
                    self._suppress_popup = True
                    completed_val = f"/{selected}"
                    inp.focus()
                    inp.value = completed_val
                    self.post_message(Key("end", None))
                    self._suggest_popup.remove()
                    self._suggest_matches = []
                    self._suggest_mode = ""
                else:  # introspect
                    self._suppress_popup = True
                    completed_val = f"?{selected}"
                    inp.focus()
                    inp.value = completed_val
                    self.post_message(Key("end", None))
                    self._suggest_popup.remove()
                    self._suggest_matches = []
                    self._suggest_mode = ""
                return

        elif event.key == "tab":
            """Tab autocomplete for slash, introspect, and file completion."""

            # --- File-path completion ---
            if self._suggest_mode == "file" and self._suggest_popup.is_attached:
                event.stop()
                inp.focus()

                if not self._file_completions:
                    return

                # Get currently displayed completions (filtered by prefix)
                prefix = self._file_completion_prefix.lower()
                filtered = [
                    (name, is_dir)
                    for name, is_dir in self._file_completions
                    if name.lower().startswith(prefix)
                ]

                if not filtered:
                    return

                cwd = str(Path.cwd())
                display_dir = self._file_completion_dir
                if display_dir.startswith(cwd):
                    display_dir = "." + display_dir[len(cwd):]

                if len(filtered) == 1:
                    # Single match — auto-complete
                    selected_name, is_dir = filtered[0]
                    if display_dir and display_dir != ".":
                        new_path = display_dir + "/" + selected_name
                    else:
                        new_path = selected_name
                    completed_val = self._file_completion_cmd + " " + new_path
                    self._suppress_popup = True
                    inp.value = completed_val
                    self.post_message(Key("end", None))

                    if is_dir:
                        # Directory: scan its contents and stay in file mode
                        self._file_completions = self._get_file_completions(
                            self._file_completion_cmd, new_path
                        )
                        self._file_completion_prefix = ""
                        self._suggest_selected = 0
                        self._render_suggest_popup()
                    else:
                        # File: complete and dismiss
                        self._clear_file_completion_state()
                        if self._suggest_popup.is_attached:
                            self._suggest_popup.remove()
                else:
                    # Multiple matches — cycle to next
                    self._suggest_selected = (
                        (self._suggest_selected + 1) % len(filtered)
                    )
                    self._render_suggest_popup()
                return

            # --- Slash/introspect Tab (popup visible, input has focus) ---
            if self._suggest_popup.is_attached and self._suggest_matches:
                event.stop()
                selected = self._suggest_matches[self._suggest_selected]
                prefix = "/" if self._suggest_mode == "slash" else "?"
                self._suppress_popup = True
                completed_val = f"{prefix}{selected}"
                inp.focus()
                inp.value = completed_val
                self.post_message(Key("end", None))
                self._suggest_popup.remove()
                self._suggest_matches = []
                self._suggest_mode = ""
                return

            # --- Auto-complete from input (single-match shortcut) ---
            if not self._suggest_popup.is_attached:
                return
            event.stop()
            value = inp.value

            if value.startswith("/"):
                prefix = value[1:].strip()
                if not prefix:
                    matches = list(self._SLASH_COMMANDS)
                else:
                    matches = [
                        c
                        for c in self._SLASH_COMMANDS
                        if c.startswith(prefix)
                    ]
                if len(matches) == 1:
                    completed_val = f"/{matches[0]}"
                    inp.value = completed_val
                    self.post_message(Key("end", None))

            elif value.startswith("?"):
                prefix = value[1:].strip()
                known = list(self._introspect_map.keys())
                if not prefix:
                    matches = sorted(known)
                else:
                    matches = sorted(
                        k for k in known if k.startswith(prefix)
                    )
                if len(matches) == 1:
                    completed_val = f"?{matches[0]}"
                    inp.value = completed_val
                    self.post_message(Key("end", None))

    # ------------------------------------------------------------------
    # Slash-command handlers
    # ------------------------------------------------------------------

    def _handle_help(self) -> str:
        """Return formatted help text covering all command categories."""
        cmd_list = "\n".join(
            f"  /{cmd:<12} {self._cmd_descriptions[cmd]}"
            for cmd in self._SLASH_COMMANDS
        )
        # Square-bracket optionals are Rich markup escapes: \[ renders a literal [. Keep new optional-parameter segments escaped.
        return (
            "[bold]TUI Commands[/bold] (prefix with /):\n"
            f"{cmd_list}\n"
            "[bold]Introspection[/bold] (prefix with ?):\n"
            "  ?<object>[.<attr>] \\[args...]  Inspect or call objects\n"
            "  ?                 List available introspection objects\n"
            "  Available: session, transport, driver, status, parser, decoder, rpc\n"
            "\n"
            "[bold]Ruida Commands[/bold] (no prefix):\n"
            "  session start udp=<IP> usb=<device> to=<timeout> magic=0xNN  Connect to a controller (to: optional, e.g. 5s or 5000ms; magic: optional swizzle magic number, e.g. magic=0x88)\n"
            "  session end               Disconnect\n"
            "  server start host=<IP> port=<N>  Start the RPC server\n"
            "  server stop                Stop the RPC server\n"
            "  Jog & Home commands (live-only):\n"
            "    home                        Home X and Y axes (machine origin)\n"
            "    home_z                      Home Z axis\n"
            "    home_u                      Home U axis (rotary)\n"
            "    jog_xy_to <x> <y>           Jog XY to absolute position (mm)\n"
            "    jog_x_to <x>                Jog X to absolute position (mm)\n"
            "    jog_y_to <y>                Jog Y to absolute position (mm)\n"
            "    jog_z_to <z>                Jog Z to absolute position (mm, max 2000)\n"
            "    jog_u_to <u>                Jog U to absolute position (mm)\n"
            "    jog_xy_rel \\[x] \\[y]          Jog XY relative (uses configured defaults)\n"
            "    jog_x_rel \\[x]               Jog X relative (uses configured default)\n"
            "    jog_y_rel \\[y]               Jog Y relative (uses configured default)\n"
            "    jog_z_rel \\[z]               Jog Z relative (uses configured default)\n"
            "    jog_u_rel \\[u]               Jog U relative (uses configured default)\n"
            "    jog_set_xy_speed <speed>    Set XY jog speed (mm/s)\n"
            "    jog_set_z_speed <speed>     Set Z jog speed (mm/s)\n"
            "    jog_set_u_speed <speed>     Set U jog speed (mm/s)\n"
            "    jog_set_xy_rel <delta>      Set relative XY jog distance (mm)\n"
            "    jog_set_z_rel <delta>       Set relative Z jog distance (mm)\n"
            "    jog_set_u_rel <delta>       Set relative U jog distance (mm)\n"
            "  <rpascript command>       Send command to controller\n"
            "\n"
            "[bold]Flow Control[/bold] (for loaded scripts):\n"
            "  delay <time>              Pause execution (e.g. 5s, 100ms)\n"
            "  wait <status> \\[to=...]    Wait for MACHINE_STATUS_* bit\n"
            "  wait !<status> \\[to=...]   Wait for lifecycle (active then inactive)\n"
            "  Statuses: MACHINE_STATUS_MOVING, MACHINE_STATUS_LAYER_END,\n"
            "            MACHINE_STATUS_JOB_RUNNING\n"
            "  to=   Optional timeout (e.g. to=30s). Default: forever\n"
            "\n"
            "[bold]GlueScript Commands[/bold] (prefix with /):\n"
            "  /gluescript new \\[label]                     Reset and declare a new job (MACHINE ref)\n"
            "  /gluescript show                             Display current gluescript state summary\n"
            "  /gluescript stage                            Finalize (if needed) and generate rpascript from gluescript\n"
            "  /gluescript run                              Finalize (if needed), stage, and execute the job\n"
            "  /gluescript save <path>                         Save gluescript to a .cglu file\n"
            "  /gluescript load <path>                         Load a .cglu gluescript file and stage it\n"
            "  /gluescript edit                            Edit the gluescript in a full-screen editor\n"
            "  /gluescript list                             Display high-level gluescript commands\n"
        )

    async def _handle_slash_command(self, raw: str) -> None:
        """Dispatch a /-prefixed TUI command to its handler."""
        parts = raw[1:].split(None, 1)  # strip leading /
        if not parts:
            self._log_error("Empty command. Type /help for available commands.")
            return
        cmd = parts[0]
        if cmd not in self._SLASH_COMMANDS:
            self._log_error(
                f"Unknown TUI command: /{cmd}. Type /help for available commands."
            )
            return
        args = parts[1] if len(parts) > 1 else ""
        try:
            if cmd == "help":
                self._log_info(self._handle_help())
            elif cmd == "load":
                self._cmd_load(args)
            elif cmd == "run":
                self._cmd_exec(args)
            elif cmd == "export":
                self._cmd_export(args)
            elif cmd == "clear":
                self._cmd_clear()
            elif cmd == "quit":
                self._cmd_quit()
            elif cmd == "status":
                self._cmd_log(args)
            elif cmd == "head":
                self._cmd_head(args)
            elif cmd == "import":
                self._cmd_import(args)
            elif cmd == "tail":
                self._cmd_tail(args)
            elif cmd == "list":
                await self._cmd_list(args)
            elif cmd == "save":
                self._cmd_save(args)
            elif cmd == "stop":
                self._cmd_stop(args)
            elif cmd == "dryrun":
                self._cmd_dryrun(args)
            elif cmd == "edit":
                self._cmd_edit(args)
            elif cmd == "frame":
                self._cmd_frame(args)
            elif cmd == "protect":
                self._cmd_protect(args)
            elif cmd == "rpclog":
                self._cmd_rpclog(args)
            elif cmd == "plot":
                self._cmd_plot(args)
            elif cmd == "monitor":
                self._cmd_monitor(args)
            elif cmd == "scan_mem":
                self._cmd_scan_mem()
            elif cmd == "gluescript":
                self._cmd_gluescript(args)
        except Exception as e:
            self._log_error(f"Command /{cmd} failed: {e}")

    # ------------------------------------------------------------------
    # _ImportCollector — in-memory script line collector for /import
    # ------------------------------------------------------------------

    class _ImportCollector:
        """In-memory equivalent of ScriptGenerator — accumulates .rds lines
        from decoded parser command data instead of writing to a file."""

        def __init__(self, source_file: str | None = None) -> None:
            self.lines: list[str] = []
            self.lines.append("# Generated by rpa.py script generator")
            if source_file is not None:
                self.lines.append(f"# Source: {os.path.basename(source_file)}")
            self._pending_line: str | None = None
            self._pending_expect: str | None = None
            self._last_cmd_n = 0
            self._packet_count = 0

        @staticmethod
        def _extract_reply_expect(decoded: str) -> str | None:
            """Extract the reply value from a decoded command string.

            Returns the reply value as a string, or '?' for unknown/TBD values,
            or None if no reply is present.
            """
            if ":Reply:" in decoded:
                reply_part = decoded.split(":Reply:", 1)[1]
                if "Unknown" in reply_part or "TBD" in reply_part:
                    return "?"
                return reply_part
            return None

        def write_command(
            self,
            *,
            label,
            cmd_values,
            param_list,
            command,
            sub_command,
            decoded,
            cmd_n,
        ) -> None:
            """Receive a decoded command from the parser callback.

            Mirrors ScriptGenerator.write_command — buffers the formatted line
            until any reply callback arrives (same cmd_n) so the reply value
            can be captured as ``= expected``.
            """
            # Reply on same cmd_n → capture expected value
            if cmd_n == self._last_cmd_n:
                if self._pending_expect is None:
                    self._pending_expect = self._extract_reply_expect(decoded)
                return

            # New command → flush any previously buffered line first
            self._flush_pending()

            self._last_cmd_n = cmd_n
            self._pending_expect = None
            line = ScriptGenerator._format_line(label, param_list, cmd_values, decoded)
            self._pending_line = line

        def on_new_packet(self) -> None:
            """Called once per host→controller packet, before any commands in it."""
            self._flush_pending()
            self._packet_count += 1
            if self._packet_count > 1:
                self.lines.append("new_packet")

        def _flush_pending(self) -> None:
            """Write the buffered command line, appending ``= <expect>`` if a
            reply value was captured from a subsequent callback on the same cmd_n."""
            if self._pending_line is None:
                return
            line = self._pending_line
            if self._pending_expect is not None:
                line += f"  = {self._pending_expect}"
            self.lines.append(line)
            self._pending_line = None

        def get_script(self) -> list[str]:
            """Flush any remaining buffered line and return all collected lines."""
            self._flush_pending()
            return self.lines



    def _cmd_import(self, args: str) -> None:
        """Import a tshark capture file (.log) or RDWorks file (.rd) as a script.

        Decodes the file in-process using the RuidaProtocolAnalyzer pipeline
        (for .log files) or the RdBinaryStream reader + RdParser (for .rd files),
        converts decoded commands to .rds script lines via the _ImportCollector,
        and loads the result into _loaded_script for /run or /save.
        """
        if not args:
            self._log_error("Usage: /import <path> \\[magic=0xNN]")
            return

        tokens = args.split()
        path = os.path.expanduser(tokens[0])

        # Parse optional arguments
        magic = 0x88
        for tok in tokens[1:]:
            if tok.startswith("magic="):
                try:
                    val = tok.split("=", 1)[1]
                    if val.lower().startswith("0x"):
                        magic = int(val, 16) & 0xFF
                    else:
                        raise ValueError
                except (ValueError, IndexError):
                    self._log_error(f"Invalid magic number: {tok}")
                    return

        if not os.path.isfile(path):
            self._log_error(f"File not found: {path}")
            return

        _, ext = os.path.splitext(path)
        ext = ext.lower()

        # Build minimal args namespace for the decode pipeline
        ns = argparse.Namespace(
            magic=magic,
            input_file=path,
            input_encoding="utf-8",
            verbose=False,
            raw=False,
            unswizzled=False,
            stop_on_error=False,
            quiet=True,
            output_file=None,
        )

        output = RpaEmitter(ns)
        try:
            if ext == ".rd":
                stream = RdBinaryStream(path, magic=magic)
                collector = self._ImportCollector(source_file=path)
                parser = RdParser(output, path)
                parser.on_command = collector.write_command
                while True:
                    b = stream.next_byte()
                    if b is None:
                        break
                    parser.step(
                        b,
                        is_reply=False,
                        take=stream.take,
                        remaining=stream.remaining,
                    )
                script = collector.get_script()
            elif ext in (".log", ".txt"):
                with open(path, "r", encoding="utf-8") as fp:
                    analyzer = RuidaProtocolAnalyzer(ns, fp, output)
                    collector = self._ImportCollector(source_file=path)
                    analyzer.parser.on_command = collector.write_command
                    analyzer.on_new_packet = collector.on_new_packet
                    analyzer.decode()
                    script = collector.get_script()
            else:
                self._log_error(f"Unsupported file extension: {ext}")
                return
        except SyntaxError as e:
            self._log_error(f"Decode error: {e}")
            return
        except LookupError as e:
            self._log_error(f"Command lookup error: {e}")
            return
        except ValueError as e:
            self._log_error(f"Command formatting error: {e}")
            return
        except RuntimeError as e:
            self._log_error(f"Decode error: {e}")
            return
        except OSError as e:
            self._log_error(f"File error: {e}")
            return
        except Exception as e:
            self._log_error(f"Unexpected error importing {path}: {e}")
            return

        if not script:
            self._log_warning(f"No commands found in {path}")
            self._loaded_script = []
            return

        self._loaded_script = script
        self._log_info(f"Imported {len(script)} lines from {path}")
        self._plot_source = os.path.basename(path)

    def _cmd_load(self, path: str) -> None:
        """Load a script file into memory."""
        if not path:
            self._log_error("Usage: /load <path>")
            return
        path = os.path.expanduser(path)
        self._loaded_script_path = path
        try:
            with open(path, "r") as f:
                content = f.read()
            lines = [line for line in content.splitlines() if line.strip()]
            if not lines:
                self._log_error(f"File is empty or contains only blank lines: {path}")
                return
            self._loaded_script = lines
            self._log_info(f"Loaded {len(lines)} lines from {path}")
            self._plot_source = os.path.basename(path)
        except FileNotFoundError:
            self._log_error(f"File not found: {path}")
        except PermissionError:
            self._log_error(f"Permission denied: {path}")
        except UnicodeDecodeError:
            self._log_error(f"File is not a valid text file: {path}")
        except Exception as e:
            self._log_error(f"Error reading {path}: {type(e).__name__}: {e}")

    def _cmd_head(self, path: str) -> None:
        """Load a script file to prepend to job on execution."""
        if not path:
            self._log_error("Usage: /head <path>")
            return
        path = os.path.expanduser(path)
        try:
            with open(path, "r") as f:
                content = f.read()
            lines = [line for line in content.splitlines() if line.strip()]
            if not lines:
                self._log_error(f"File is empty or contains only blank lines: {path}")
                return
            self._head_script = lines
            self._log_info(f"Head loaded: {len(lines)} lines from {path}")
            if self._ruida_driver is not None:
                self._ruida_driver.set_head_script(self._head_script)
        except FileNotFoundError:
            self._log_error(f"File not found: {path}")
        except PermissionError:
            self._log_error(f"Permission denied: {path}")
        except UnicodeDecodeError:
            self._log_error(f"File is not a valid text file: {path}")
        except Exception as e:
            self._log_error(f"Error reading {path}: {type(e).__name__}: {e}")

    def _cmd_tail(self, path: str) -> None:
        """Load a script file to append to job on execution."""
        if not path:
            self._log_error("Usage: /tail <path>")
            return
        path = os.path.expanduser(path)
        try:
            with open(path, "r") as f:
                content = f.read()
            lines = [line for line in content.splitlines() if line.strip()]
            if not lines:
                self._log_error(f"File is empty or contains only blank lines: {path}")
                return
            self._tail_script = lines
            self._log_info(f"Tail loaded: {len(lines)} lines from {path}")
            if self._ruida_driver is not None:
                self._ruida_driver.set_tail_script(self._tail_script)
        except FileNotFoundError:
            self._log_error(f"File not found: {path}")
        except PermissionError:
            self._log_error(f"Permission denied: {path}")
        except UnicodeDecodeError:
            self._log_error(f"File is not a valid text file: {path}")
        except Exception as e:
            self._log_error(f"Error reading {path}: {type(e).__name__}: {e}")

    def _cmd_exec(self, args: str = "") -> None:
        """Execute the loaded script.

        Defaults to executing only the job portion (START_JOB to EOF).
        Uses driver.run_job(job) — the extracted job body is passed
        explicitly, so the staged-rpascript default never applies here —
        which composes head + job + tail at runtime.
        Use '/run script' to execute all loaded commands.
        """
        if not self._loaded_script:
            self._log_error("No script loaded. Use /load <path> first.")
            return
        if self._ruida_driver is None or not self._ruida_driver.is_connected:
            self._log_error(
                "No active session. Use 'session start udp=<IP> usb=<device>' first."
            )
            return
        action = args.strip().lower()
        if action == "":
            job = self._filter_job_commands(self._loaded_script)
            if not job:
                self._log_error("No job commands found (no START_JOB/EOF markers).")
                return
            self._log_info(f"Executing {len(job)} job commands...")
            try:
                self._ruida_driver.run_job(job, auto_checksum=True)
            except RuntimeError as e:
                self._log_error(f"Run failed: {e}")
        elif action == "script":
            self._log_info(f"Executing {len(self._loaded_script)} lines...")
            try:
                self._ruida_driver.run(self._loaded_script)
            except RuntimeError as e:
                self._log_error(f"Run failed: {e}")
        else:
            self._log_error(f"Unknown run action: '{action}'. Usage: /run \\[script]")

    @staticmethod
    def _filter_job_commands(lines: list[str]) -> list[str]:
        """Filter lines to only include commands between START_JOB and EOF (inclusive).

        Excludes GET_SETTING and new_packet directives — they are not part of the job.
        """
        in_job = False
        result: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == "START_JOB" or stripped.startswith("START_JOB "):
                in_job = True
            if in_job:
                # Skip GET_SETTING and new_packet — not part of the job
                if stripped.startswith("GET_SETTING") or stripped.startswith(
                    "new_packet"
                ):
                    continue
                result.append(line)
            if stripped == "EOF" or stripped.startswith("EOF "):
                break
        return result

    def _format_job_with_markers(self) -> list[str]:
        """Format the job with section comment markers for display.

        Returns a list of lines with # --- Head ---, # --- Job ---,
        and # --- Tail --- section markers, showing how the job will
        be composed at runtime by the driver.
        """
        job = self._filter_job_commands(self._loaded_script)
        if not job:
            return []
        result: list[str] = []
        result.append("# --- Head ---")
        if self._head_script:
            result.extend(self._head_script)
        else:
            result.append("# (empty)")
        result.append("# --- Job ---")
        result.extend(job)
        result.append("# --- Tail ---")
        if self._tail_script:
            result.extend(self._tail_script)
        else:
            result.append("# (empty)")
        return result

    def _cmd_export(self, args: str) -> None:
        """Export the loaded script as an .rd binary file.

        Derives the default filename from the source of the loaded script
        (e.g., capture.log → capture.rd). If the file exists, logs an
        error asking the user to specify a different path.
        """
        if not self._loaded_script:
            self._log_error("No script loaded. Use /load <path> first.")
            return

        # Parse optional magic argument
        magic = 0x88
        if args:
            tokens = args.split()
            path_arg = tokens[0]
            for tok in tokens[1:]:
                if tok.startswith("magic="):
                    try:
                        val = tok.split("=", 1)[1]
                        if val.lower().startswith("0x"):
                            magic = int(val, 16) & 0xFF
                        else:
                            raise ValueError
                    except (ValueError, IndexError):
                        self._log_error(f"Invalid magic number: {tok}")
                        return
        else:
            path_arg = ""

        # Derive export path
        if path_arg:
            export_path = os.path.expanduser(path_arg)
        elif self._plot_source and self._plot_source != "[RPC]":
            base, _ = os.path.splitext(self._plot_source)
            export_path = f"{base}.rd"
        else:
            self._log_error(
                "No source to derive filename from. Specify a path: /export <path>"
            )
            return

        # Check if file exists
        if os.path.exists(export_path):
            self._log_error(
                f"File exists: {export_path}. Specify a different path: /export <path>"
            )
            return

        # Parse the loaded script into command dicts
        parsed = self._parser.parse_lines(self._loaded_script)
        if not parsed:
            self._log_error("No commands found in script.")
            return

        # Encode commands to raw bytes (continuous USB stream, no packet boundaries)
        enc = RdEncoder()
        raw = bytearray()
        for cmd in parsed:
            cmd_type = cmd.get("type")
            if cmd_type in ("new_packet", "SESSION_START", "SESSION_END", "DELAY", "WAIT"):
                continue
            mnemonic = cmd.get("mnemonic")
            if not mnemonic:
                continue
            # Skip read-only query commands (GET_SETTING, GET_UNKNOWN) — they
            # have no place in a write-only .rd binary export.
            if mnemonic.startswith("GET_"):
                continue
            try:
                cmd_bytes = encode_command(
                    cmd, self._parser.mnemonic_map, self._parser._mt_map, enc
                )
            except (ValueError, TypeError) as e:
                self._log_error(
                    f"Encoding failed for command '{mnemonic}' "
                    f"(params={cmd.get('params', [])!r}): {e}"
                )
                return
            raw.extend(cmd_bytes)

        if not raw:
            self._log_error("No encodable commands in script.")
            return

        # Swizzle and write .rd file
        swizzler = RpaSwizzler(magic=magic)
        swizzled = swizzler.swizzle(raw)

        # - 10-byte RDWORKV header (7 magic bytes + 3 wildcard bytes)
        # - Followed by swizzled payload bytes
        try:
            with open(export_path, "wb") as f:
                f.write(b"RDWORKV" + b"\x00" * 3)
                f.write(swizzled)
            self._log_info(
                f"Exported {len(raw)} bytes to {export_path}"
                f" ({len(swizzled)} swizzled)"
            )
        except OSError as e:
            self._log_error(f"Error writing {export_path}: {e}")

    def _cmd_clear(self) -> None:
        """Clear all log panels, loaded script, head, and tail."""
        self._log_widget.clear()
        self._status_log.clear()
        self._reply_log.update("")
        self._loaded_script = []
        self._head_script = []
        self._tail_script = []
        if self._ruida_driver is not None:
            self._ruida_driver.set_head_script([])
            self._ruida_driver.set_tail_script([])
        # Stop memory monitor timer
        if self._mem_timer is not None:
            self._mem_timer.cancel()
            self._mem_timer = None
        self._monitor_enabled = False
        self._mem_initial = {}
        self._mem_prev = None
        self._gc_initial = {}
        self._gc_prev = None
        # Shut down any running Bokeh servers
        for _app in self._bokeh_apps:
            _app.shutdown()
        self._bokeh_apps = []
        self._plot_source = None
        self._loaded_script_path = None
        self._gluescript_cglu_path = None
        self._log_info("Logs, head, and tail cleared")

    def _cmd_quit(self) -> None:
        """Exit the TUI."""
        self.exit()

    def action_stop(self) -> None:
        """Handle Escape key: stop current operation."""
        self._cmd_stop("")

    def action_scroll_log_up(self) -> None:
        """Page Up: scroll the log area up by one page."""
        self._log_widget.scroll_page_up()

    def action_scroll_log_down(self) -> None:
        """Page Down: scroll the log area down by one page."""
        self._log_widget.scroll_page_down()

    # ------------------------------------------------------------------
    # Exception handling
    # ------------------------------------------------------------------

    def _handle_exception(self, error: BaseException) -> None:
        """Override default: keep app alive and show persistent error screen.

        Textual's default _handle_exception calls panic() which calls
        _close_messages_no_wait(), shutting down the app immediately
        and printing the traceback to stderr after alt-screen restore.
        Instead, we populate _exit_renderables (for terminal fallback)
        and schedule a screen push via call_later so the user sees the
        error and must press a key to exit.
        """
        from rich.text import Text as RichText
        from rich.traceback import Traceback

        self._exit_renderables = [
            RichText(f"Fatal error: {error}", style="bold red"),
            Traceback.from_exception(
                type(error), error, error.__traceback__
            ),
        ]
        # Do NOT call panic() or _fatal_error() — keep the app alive
        self.call_later(self._show_error_screen, error)

    def _show_error_screen(self, error: BaseException) -> None:
        """Push the ErrorScreen onto the screen stack.

        Falls back to terminal exit if push_screen fails (e.g., no
        screen stack yet).
        """
        try:
            self.push_screen(ErrorScreen(error))
        except Exception:
            import sys
            sys.exit(1)

    def _cmd_log(self, args: str) -> None:
        """Handle /status subcommands: on, off, status, connection, or toggle."""
        parts = args.strip().split(None, 1)
        action = parts[0].lower() if parts else ""

        if action == "connection":
            sub = parts[1].strip().lower() if len(parts) > 1 else "toggle"
            if sub in ("", "toggle"):
                if self._connection_logging_enabled:
                    self._disable_connection_logging()
                    self._log_info("Connection logging disabled")
                else:
                    self._enable_connection_logging()
                    self._log_info("Connection logging enabled")
            elif sub == "on":
                self._enable_connection_logging()
                self._log_info("Connection logging enabled")
            elif sub == "off":
                self._disable_connection_logging()
                self._log_info("Connection logging disabled")
            elif sub == "status":
                state = "ON" if self._connection_logging_enabled else "OFF"
                self._log_info(f"Connection logging is {state}")
            else:
                self._log_error("Usage: /log connection \\[on|off|status]")
        elif action in ("", "toggle"):
            self._logging_enabled = not self._logging_enabled
            state = "ON" if self._logging_enabled else "OFF"
            self._log_info(f"Logging is {state}")
        elif action == "on":
            self._logging_enabled = True
            self._log_info("Logging enabled")
        elif action == "off":
            self._logging_enabled = False
            self._log_info("Logging disabled (status/reply suppressed)")
        elif action == "status":
            state = "ON" if self._logging_enabled else "OFF"
            self._log_info(f"Logging is {state}")
        else:
            self._log_error("Usage: /log \\[on|off|status|connection \\[on|off|status]]")

    async def _write_lines_chunked(self, lines: list[str], prefix: str = "") -> None:
        """Write lines to the log widget in chunks, yielding between each chunk.

        Calls _update_status_bar() after each write to keep the status bar
        current while the list is being displayed.
        """
        CHUNK_SIZE = 100
        formatted = [f"{prefix}{line}" for line in lines]
        for i in range(0, len(formatted), CHUNK_SIZE):
            chunk = formatted[i:i + CHUNK_SIZE]
            self._log_widget.write("\n".join(chunk))
            self._update_status_bar()
            self._drain_status_log_buffer()
            if i + CHUNK_SIZE < len(formatted):
                await asyncio.sleep(0)

    def _drain_status_log_buffer(self) -> None:
        """Drain buffered status log messages into the status log widget.

        Thread-safe: deque.popleft() is atomic under GIL. Must be called
        from the event loop thread (for widget safety).
        """
        while self._status_log_buffer:
            try:
                msg = self._status_log_buffer.popleft()
            except IndexError:
                break
            self._status_log.write(msg)

    def _enable_connection_logging(self) -> None:
        """Register connection log callbacks with driver for TUI display."""
        if self._ruida_driver and self._ruida_driver._session:
            transport = self._ruida_driver._session.transport
            status = self._ruida_driver._session.status
            transport.set_connection_log(self._on_connection_log)
            if status:
                status.set_connection_log(self._on_connection_log)
        self._connection_logging_enabled = True

    def _disable_connection_logging(self) -> None:
        """Unregister connection log callbacks from driver."""
        if self._ruida_driver and self._ruida_driver._session:
            transport = self._ruida_driver._session.transport
            status = self._ruida_driver._session.status
            transport.set_connection_log(None)
            if status:
                status.set_connection_log(None)
        self._connection_logging_enabled = False

    def _on_connection_log(self, msg: str) -> None:
        """Receive connection log message from background thread, display in TUI log."""
        self._status_log_buffer.append(msg)

    async def _cmd_list(self, args: str) -> None:
        """Handle /list subcommands: script, job, head, tail, or auto."""
        action = args.strip().lower()
        if action == "script":
            if not self._loaded_script:
                self._log_info("No script loaded. Use /load <path> first.")
                return
            self._log_info(f"Loaded script ({len(self._loaded_script)} lines):")
            await self._write_lines_chunked(self._loaded_script, prefix="  ")
        elif action == "job":
            if not self._loaded_script:
                self._log_info("No script loaded. Use /load <path> first.")
                return
            formatted = self._format_job_with_markers()
            if not formatted:
                self._log_error("No job commands found (no START_JOB/EOF markers).")
                return
            self._log_info(f"Composed job ({len(formatted)} lines):")
            await self._write_lines_chunked(formatted, prefix="  ")
        elif action == "head":
            if not self._head_script:
                self._log_info("No head script loaded. Use /head <path> first.")
                return
            self._log_info(f"Head script ({len(self._head_script)} lines):")
            await self._write_lines_chunked(self._head_script, prefix="  ")
        elif action == "tail":
            if not self._tail_script:
                self._log_info("No tail script loaded. Use /tail <path> first.")
                return
            self._log_info(f"Tail script ({len(self._tail_script)} lines):")
            await self._write_lines_chunked(self._tail_script, prefix="  ")
        elif action == "auto" or action.startswith("auto "):
            arg = action[5:].strip() if len(action) > 5 else ""
            if arg == "on":
                self._auto_display_script = True
                self._log_info("Auto-display of RPC scripts ON")
            elif arg == "off":
                self._auto_display_script = False
                self._log_info("Auto-display of RPC scripts OFF")
            elif arg == "":
                state = "ON" if self._auto_display_script else "OFF"
                self._log_info(f"Auto-display of RPC scripts is {state}")
            else:
                self._log_error("Usage: /list auto \\[on|off]")
        else:
            self._log_error("Usage: /list \\[job|script|head|tail|auto]")

    def _cmd_save(self, args: str) -> None:
        """Handle /save subcommands: job <path>, script <path>, or as <path>,
        or bare /save <path> which defaults to script save."""
        parts = args.strip().split(None, 1)
        if not parts:
            self._log_error("Usage: /save <path> | /save job <path> | /save script <path> | /save as <path>")
            return

        # Determine subcommand and path
        if parts[0] in ("job", "script", "as"):
            if len(parts) < 2:
                self._log_error("Usage: /save {job|script|as} <path>")
                return
            subcmd = parts[0]
            path = parts[1]
        else:
            # Bare /save <path> — default to script save
            subcmd = "script"
            path = args.strip()

        if not self._loaded_script:
            self._log_error("No script loaded. Use /load <path> first.")
            return

        if subcmd == "job":
            lines = self._filter_job_commands(self._loaded_script)
            if not lines:
                self._log_error("No job commands found (no START_JOB/EOF markers).")
                return
            label = "job"
        else:  # script or as
            lines = self._loaded_script
            label = "script"

        path = os.path.expanduser(path)
        try:
            with open(path, "w") as f:
                f.write("\n".join(lines) + "\n")
            self._log_info(f"{label.capitalize()} saved to {path} ({len(lines)} lines)")
        except PermissionError:
            self._log_error(f"Permission denied: {path}")
        except OSError as e:
            self._log_error(f"Error writing {path}: {type(e).__name__}: {e}")

    def _cmd_stop(self, args: str) -> None:
        """Stop script execution."""
        if self._ruida_driver is not None:
            self._ruida_driver.cancel_script()
            self._log_info("Script execution stopped")
        else:
            self._log_info("Nothing to stop")

    def _cmd_dryrun(self, args: str = "") -> None:
        """Toggle dry-run mode (on|off)."""
        arg = args.strip().lower()
        if arg == "on":
            self._dryrun = True
            self._log_info("Dry-run mode ON — RPC driver.run() will only log to TUI")
        elif arg == "off":
            self._dryrun = False
            self._log_info("Dry-run mode OFF — RPC driver.run() will execute normally")
        else:
            self._log_error("Usage: /dryrun on|off")

    def _cmd_rpclog(self, args: str = "") -> None:
        """Toggle verbose RPC logging (on|off|status)."""
        arg = args.strip().lower()
        if self._rpyc_server is None:
            self._log_error("No RPC server running. Use 'server start' first.")
            return
        service = self._rpyc_server.service  # the live RpycTuiService — NOT self._logging_enabled
        if arg == "on":
            service.enable_logging()        # emits "[RPC] Logging enabled" itself
        elif arg == "off":
            service.disable_logging()       # emits "[RPC] Logging disabled" itself
        elif arg == "status":
            state = "ON" if service.logging_enabled() else "OFF"
            self._log_info(f"RPC logging is {state}")
        elif arg == "":
            if service.logging_enabled():
                service.disable_logging()
            else:
                service.enable_logging()
        else:
            self._log_error("Usage: /rpclog [on|off|status]")

    def _cmd_protect(self, args: str = "") -> None:
        """Toggle protect mode (on|off|status)."""
        arg = args.strip().lower()
        if arg == "on":
            if self._ruida_driver is not None:
                self._ruida_driver.set_protect(True)
            self._log_info("Protect mode ON — SET_SETTING commands are blocked")
        elif arg == "off":
            if self._ruida_driver is not None:
                self._ruida_driver.set_protect(False)
            self._log_info("Protect mode OFF — SET_SETTING commands will be sent to controller")
        elif arg == "" or arg == "status":
            if self._ruida_driver is not None and self._ruida_driver.protect_enabled:
                self._log_info("Protect mode: ON — SET_SETTING commands are blocked")
            else:
                self._log_info("Protect mode: OFF — SET_SETTING commands will be sent")
        else:
            self._log_error("Usage: /protect on|off|status")

    def _cmd_plot(self, args: str = "") -> None:
        """Plot the loaded script in a Bokeh visualization."""
        if not self._loaded_script:
            self._log_error("No script loaded. Use /load <path> first.")
            return

        if BokehApp is None:
            self._log_error("Bokeh is not installed. Install with: pip install bokeh")
            return

        from protocols.ruida.rpa_plotter import RpaPlotter

        parsed = self._parser.parse_lines(self._loaded_script)
        if not parsed:
            self._log_error("No commands found in script.")
            return

        ns = argparse.Namespace(
            input_file=self._plot_source or "<script>",
            output_file=None,
            bokeh_port=5006,
            quiet=True,
            stop_on_error=False,
            verbose=False,
            raw=False,
            unswizzled=False,
            magic=0x88,
            input_encoding="utf-8",
            plot_moves=False,
        )

        out = RpaEmitter(ns)
        plotter = RpaPlotter(out, "Script Plot")
        plotter.plot.enable()

        cmd_id = 0
        for cmd in parsed:
            cmd_type = cmd.get("type")
            if cmd_type in ("SESSION_START", "SESSION_END", "DELAY", "WAIT", "new_packet"):
                continue

            mnemonic = cmd.get("mnemonic")
            if not mnemonic:
                continue

            info = self._parser.mnemonic_map.get(mnemonic)
            if info is None:
                continue

            prefix_byte = info[0]

            if len(info) == 4:
                sub_cmd = info[2]
                cmd_entry = info[3]
            else:
                sub_cmd = info[1] if len(info) >= 2 else None
                cmd_entry = info[2] if len(info) > 2 else None

            param_specs = cmd_entry[1:] if cmd_entry and len(cmd_entry) > 1 else ()
            param_values = cmd.get("params", [])

            values = []
            for i, spec in enumerate(param_specs):
                if i >= len(param_values):
                    break
                if not isinstance(spec, tuple) or len(spec) < 2:
                    continue
                decoder_fn = spec[1]
                rd_type = spec[2] if len(spec) >= 3 else None
                token = param_values[i].strip()
                if "=" in token:
                    _, token = token.split("=", 1)
                try:
                    values.append(parse_value(token, decoder_fn, rd_type))
                except Exception:
                    continue

            cmd_id += 1
            try:
                plotter.cmd_update(cmd_id, mnemonic, prefix_byte, sub_cmd, values)
            except Exception:
                continue

        if cmd_id == 0:
            self._log_error("No plot-relevant commands found in script.")
            return

        # Shut down any existing Bokeh server before starting a new one
        for _app in self._bokeh_apps:
            _app.shutdown()
        self._bokeh_apps = []

        try:
            bokeh_app = BokehApp(ns, plotter.plot)
            if bokeh_app.start(port=5006):
                self._log_info(
                    "Bokeh visualization: http://localhost:{}".format(bokeh_app.port)
                )
                self._bokeh_apps.append(bokeh_app)
            else:
                self._log_error("Failed to start Bokeh server.")
        except Exception as e:
            self._log_error("Failed to start Bokeh server: {}".format(e))

    def _cmd_frame(self, args: str) -> None:
        """Frame the job or a specific layer.

        Sets speed to 600 mm/S and moves the laser head to the top-right
        corner, then to the bottom-left corner using jog moves.

        Usage: /frame job | /frame layer <N>
        """
        if not self._loaded_script:
            self._log_error("No script loaded. Use /load <path> first.")
            return
        if self._ruida_driver is None or not self._ruida_driver.is_connected:
            self._log_error(
                "No active session. Use 'session start udp=<IP>' first."
            )
            return

        tokens = args.strip().split()
        if not tokens:
            self._log_error("Usage: /frame job | /frame layer <N>")
            return

        mode = tokens[0].lower()
        layer_idx = None
        if mode == "layer":
            if len(tokens) < 2:
                self._log_error("Usage: /frame layer <N>")
                return
            try:
                layer_idx = int(tokens[1])
            except ValueError:
                self._log_error(f"Invalid layer number: {tokens[1]}")
                return
        elif mode != "job":
            self._log_error(
                f"Unknown mode: {mode}. Use 'job' or 'layer <N>'."
            )
            return

        parsed = self._parser.parse_lines(self._loaded_script)

        top_right: tuple[float, float] | None = None
        bottom_left: tuple[float, float] | None = None

        for cmd in parsed:
            mnemonic = cmd.get("mnemonic", "")
            params = cmd.get("params", [])

            if mode == "job":
                if mnemonic == "JOB_TOP_RIGHT":
                    top_right = self._extract_xy(params)
                elif mnemonic == "JOB_BOTTOM_LEFT":
                    bottom_left = self._extract_xy(params)
            elif mode == "layer" and layer_idx is not None:
                if mnemonic in ("LAYER_TOP_RIGHT", "LAYER_BOTTOM_LEFT"):
                    if len(params) > 0 and params[0].startswith("Layer:"):
                        try:
                            lid = int(params[0].split(":", 1)[1])
                        except (ValueError, IndexError):
                            continue
                        if lid == layer_idx:
                            if mnemonic == "LAYER_TOP_RIGHT":
                                top_right = self._extract_xy(params[1:])
                            else:
                                bottom_left = self._extract_xy(params[1:])

        label = "job" if mode == "job" else f"layer {layer_idx}"

        if top_right is None or bottom_left is None:
            self._log_error(
                f"Could not find {label} boundary coordinates."
            )
            return

        frame_script = [
            "SPEED_LASER_1 Speed:600.000mm/S",
            f"JOG_XY Rel:MACHINE X={top_right[0]:.3f}mm Y={top_right[1]:.3f}mm",
            f"JOG_XY Rel:MACHINE X={bottom_left[0]:.3f}mm Y={bottom_left[1]:.3f}mm",
        ]

        self._log_info(
            f"Framing {label}: "
            f"top_right=({top_right[0]:.1f},{top_right[1]:.1f}) "
            f"bottom_left=({bottom_left[0]:.1f},{bottom_left[1]:.1f})"
        )
        try:
            self._ruida_driver.run(frame_script)
        except RuntimeError as e:
            self._log_error(f"Frame failed: {e}")

    @staticmethod
    def _extract_xy(params: list[str]) -> tuple[float, float] | None:
        """Extract X,Y coordinate values from parsed command params.

        Handles params in the form ``"X=335.000mm"``, ``"Y=225.000mm"``.
        Returns ``(x, y)`` or ``None`` if either value is missing.
        """
        x_val: float | None = None
        y_val: float | None = None
        for p in params:
            p = p.strip()
            if p.startswith("X="):
                try:
                    x_val = float(p[2:].removesuffix("mm").strip())
                except ValueError:
                    return None
            elif p.startswith("Y="):
                try:
                    y_val = float(p[2:].removesuffix("mm").strip())
                except ValueError:
                    return None
        if x_val is not None and y_val is not None:
            return (x_val, y_val)
        return None

    def _cmd_monitor(self, args: str) -> None:
        """Handle /monitor subcommand: on, off, or immediate update."""
        action = args.strip().lower()
        if action in ("", "update"):
            asyncio.create_task(self._update_mem_monitor())
            self._log_info("Memory/GC monitor updated")
        elif action == "on":
            if self._mem_timer is None:
                self._mem_timer = self.set_interval(15, self._update_mem_monitor)
            self._monitor_enabled = True
            self._log_info("Monitor ON — auto-update every 15s")
        elif action == "off":
            if self._mem_timer is not None:
                self._mem_timer.cancel()
                self._mem_timer = None
            self._monitor_enabled = False
            self._log_info("Monitor OFF")
        else:
            self._log_error("Usage: /monitor \\[on|off]")

    def _cmd_scan_mem(self) -> None:
        """Generate a GET_SETTING script for all MT memory addresses."""
        from protocols.ruida.ruida_protocol import MT, UNKNOWN_ADDRESS

        lines: list[str] = []
        for msb in sorted(MT):
            for lsb in sorted(MT[msb]):
                entry = MT[msb][lsb]
                if entry is UNKNOWN_ADDRESS:
                    lines.append(f"GET_SETTING 0x{msb:02X}{lsb:02X}")
                else:
                    lines.append(f"GET_SETTING {entry[0]}")
        self._loaded_script = lines
        self._log_info(f"Scan script: {len(lines)} GET_SETTING commands staged")
        self._log_info("Use /run script to run, or review with /list")

    def _finalize_gluescript_job(self, driver) -> bool:
        """Auto-finalize the current gluescript job.

        Returns True when the job is (now) complete. Logs a friendly error
        and returns False when there is no declared job to finalize
        (end_job() would raise — the only remaining RuntimeError given the
        job_complete guard).
        """
        if driver.job_complete:
            return True
        try:
            driver.end_job()
        except RuntimeError:
            self._log_error("No job to finalize. Use /gluescript new to start a job.")
            return False
        self._log_info("GlueScript: Job finalized.")
        return True

    def _create_driver(self) -> RdDriver:
        """Create a new RdDriver wired to the TUI.

        Registers the TUI listeners, syncs cached head/tail scripts, and
        restores any preserved gluescript transcript. Does NOT assign
        ``self._ruida_driver`` and does NOT call ``start()`` — the caller
        decides when to connect.
        """
        driver = RdDriver()
        driver.register_status_listener(self.on_status_event)
        driver.register_error_listener(self.on_error)
        driver.register_reply_listener(self.on_reply_data)

        # Sync any cached head/tail scripts to the new driver
        if self._head_script:
            driver.set_head_script(self._head_script)
        if self._tail_script:
            driver.set_tail_script(self._tail_script)

        if self._preserved_gluescript:
            # Deliberate boundary: do NOT sync _loaded_script here —
            # _ensure_gluescript_driver runs this at the top of every
            # session-less subcommand, and syncing would clobber a /load-ed
            # script on /gluescript show/list after teardown.
            try:
                driver.stage_gluescript(
                    self._preserved_gluescript, require_complete=False
                )
            except RuntimeError as exc:
                recovery_dir = os.path.join("tmp")
                os.makedirs(recovery_dir, exist_ok=True)
                timestamp = time.strftime("%Y%m%d-%H%M%S")
                recovery_path = os.path.join(
                    recovery_dir, f"gluescript-recovery-{timestamp}.cglu"
                )
                with open(recovery_path, "w") as f:
                    f.write("\n".join(self._preserved_gluescript) + "\n")
                self._log_error(
                    f"Could not restore preserved gluescript: {exc}; "
                    f"transcript saved to {recovery_path}"
                )
            else:
                # Restored in full — the transcript is no longer "pending run"
                self._gluescript_was_run = False
                if not driver._job_complete:
                    self._log_info(
                        f"GlueScript: restored {len(self._preserved_gluescript)} lines, transcript incomplete"
                    )
            # One-shot restore: consumed whether it succeeded or was dumped
            self._preserved_gluescript = None
        return driver

    def _ensure_gluescript_driver(self) -> RdDriver:
        """Return the current driver, creating one lazily for session-less gluescript use."""
        if self._ruida_driver is None:
            self._ruida_driver = self._create_driver()
        return self._ruida_driver

    def _release_driver(self) -> None:
        """Stop and release the current driver, preserving its gluescript.

        Copies (never aliases) the gluescript transcript so session-less
        subcommands can continue editing it after the session ends; the
        copy is re-staged onto the next driver by ``_create_driver()``.
        Callers keep responsibility for ``_session_connected.clear()`` and
        any logging.
        """
        driver = self._ruida_driver
        if driver is None:
            return
        if driver.gluescript:
            self._preserved_gluescript = list(driver.gluescript)
        driver.stop()
        self._ruida_driver = None

    # ------------------------------------------------------------------
    # GlueScript RPC delegate surface
    # ------------------------------------------------------------------

    def _gluescript_bridge(self, fn: Callable[[], Any]) -> Any:
        """Run a GlueScript delegate on the TUI thread, returning its result.

        Three execution paths, in order:
        - already on the TUI thread (running app): invoke directly
        - app not running (owned-adapter path, no event loop): invoke under
          ``self._gluescript_lock``
        - running app from another thread: ``call_from_thread`` blocks and
          returns the callback's result

        ``_thread_id`` and ``_loop`` are set by Textual only while the app's
        message loop runs, so they are read via ``getattr`` — an owned
        adapter that never ran has neither attribute.
        """
        if getattr(self, "_thread_id", None) == threading.get_ident():
            return fn()
        if getattr(self, "_loop", None) is None:
            with self._gluescript_lock:
                return fn()
        return self.call_from_thread(fn)

    def _gluescript_live_command(
        self, name: str, *args: Any
    ) -> list[str] | None:
        """Run a live-only GlueScript command (jog or home) on the driver.

        Mirrors ``_handle_live_command`` for the RPC delegate layer: the
        driver is created lazily, movement and homing commands require a
        connected session (``jog_set_*`` setters do not), and generated
        rpascript lines are sent to the controller. Returns the sent lines,
        or None when nothing was sent.
        """
        driver = self._ensure_gluescript_driver()
        if not name.startswith("jog_set_") and not driver.is_connected:
            self._log_warning(f"gluescript {name} ignored — no active session")
            return None
        result = getattr(driver, name)(*args)
        if not isinstance(result, list):
            return None  # jog_set_* setters return None
        if not result:
            self._log_warning(
                f"gluescript {name} ignored — "
                "command produced no rpascript lines"
            )
            return None
        try:
            driver.run(result)
        except RuntimeError as exc:
            self._log_warning(f"gluescript {name} not sent — {exc}")
            return None
        self._log_info(f"gluescript {name} sent to controller")
        return result

    def gluescript_new_gluescript(self) -> None:
        """Reset all script data for a new job (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().new_gluescript()
        )

    def gluescript_comment(self, comments: list[str]) -> None:
        """Append comment lines to the generated rpascript (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().comment(comments)
        )

    def gluescript_inline(self, commands: list[str]) -> None:
        """Append raw rpascript commands at the call point (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().inline(commands)
        )

    def gluescript_declare_job(
        self,
        label: str,
        ref_point: str = "MACHINE",
        abs_xy: list[float] | None = None,
        columns: int = 1,
        rows: int = 1,
        xstep: float = 0.0,
        ystep: float = 0.0,
    ) -> None:
        """Declare a new job via the GlueScript driver (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().declare_job(
                label, ref_point, abs_xy, columns, rows, xstep, ystep
            )
        )

    def gluescript_end_job(self) -> None:
        """End the current job via the GlueScript driver (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().end_job()
        )

    def gluescript_declare_layer(
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
        """Declare a new layer via the GlueScript driver (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().declare_layer(
                label, color, mode, overscan, speed, frequency,
                min_power_1, max_power_1,
            )
        )

    def gluescript_move_xy_to(self, x: float, y: float) -> None:
        """Move to an absolute XY coordinate (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().move_xy_to(x, y)
        )

    def gluescript_move_x_to(self, x: float) -> None:
        """Move to an absolute X coordinate (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().move_x_to(x)
        )

    def gluescript_move_y_to(self, y: float) -> None:
        """Move to an absolute Y coordinate (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().move_y_to(y)
        )

    def gluescript_cut_xy_to(self, x: float, y: float) -> None:
        """Cut to an absolute XY coordinate (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().cut_xy_to(x, y)
        )

    def gluescript_cut_x_to(self, x: float) -> None:
        """Cut to an absolute X coordinate (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().cut_x_to(x)
        )

    def gluescript_cut_y_to(self, y: float) -> None:
        """Cut to an absolute Y coordinate (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().cut_y_to(y)
        )

    def gluescript_power(self, percent: float | None = None) -> None:
        """Set laser power percentage; valid for IMAGE/DEPTHMAP layers."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().power(percent)
        )

    def gluescript_air_assist_on(self) -> None:
        """Enable air assist for the current layer (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().air_assist_on()
        )

    def gluescript_air_assist_off(self) -> None:
        """Disable air assist for the current layer (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().air_assist_off()
        )

    def gluescript_add_layer_action(
        self, layer: int, lines: list[str]
    ) -> None:
        """Add raw rpascript lines to a specific layer (session-less)."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().add_layer_action(
                layer, lines
            )
        )

    def gluescript_update_position(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        u: float | None = None,
    ) -> None:
        """Sync tracked position from controller-reported coordinates."""
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().update_position(
                x, y, z, u
            )
        )

    def gluescript_stage_gluescript(
        self,
        gluescript: list[str] | None = None,
        require_complete: bool = True,
    ) -> bool:
        """Finalize the rpascript or re-stage a gluescript.

        Returns True when the gluescript was successfully staged.
        """
        return self._gluescript_bridge(
            lambda: self._ensure_gluescript_driver().stage_gluescript(
                gluescript, require_complete
            )
        )

    def gluescript_jog_set_xy_speed(self, speed: float) -> None:
        """Set XY jog speed (mm/s) on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_set_xy_speed", speed)
        )

    def gluescript_jog_set_z_speed(self, speed: float) -> None:
        """Set Z jog speed (mm/s) on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_set_z_speed", speed)
        )

    def gluescript_jog_set_u_speed(self, speed: float) -> None:
        """Set U jog speed (mm/s) on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_set_u_speed", speed)
        )

    def gluescript_jog_set_xy_rel(self, delta: float) -> None:
        """Set relative XY jog distance (mm) on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_set_xy_rel", delta)
        )

    def gluescript_jog_set_z_rel(self, delta: float) -> None:
        """Set relative Z jog distance (mm) on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_set_z_rel", delta)
        )

    def gluescript_jog_set_u_rel(self, delta: float) -> None:
        """Set relative U jog distance (mm) on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_set_u_rel", delta)
        )

    def gluescript_jog_xy_to(self, x: float, y: float) -> list[str] | None:
        """Jog XY to an absolute position and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_xy_to", x, y)
        )

    def gluescript_jog_x_to(self, x: float) -> list[str] | None:
        """Jog X to an absolute position and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_x_to", x)
        )

    def gluescript_jog_y_to(self, y: float) -> list[str] | None:
        """Jog Y to an absolute position and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_y_to", y)
        )

    def gluescript_jog_z_to(self, z: float) -> list[str] | None:
        """Jog Z to an absolute position and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_z_to", z)
        )

    def gluescript_jog_u_to(self, u: float) -> list[str] | None:
        """Jog U to an absolute position and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_u_to", u)
        )

    def gluescript_jog_xy_rel(
        self, x: float | None = None, y: float | None = None
    ) -> list[str] | None:
        """Jog XY relative to the current position and run it live."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_xy_rel", x, y)
        )

    def gluescript_jog_x_rel(self, x: float | None = None) -> list[str] | None:
        """Jog X relative to the current position and run it live."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_x_rel", x)
        )

    def gluescript_jog_y_rel(self, y: float | None = None) -> list[str] | None:
        """Jog Y relative to the current position and run it live."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_y_rel", y)
        )

    def gluescript_jog_z_rel(self, z: float | None = None) -> list[str] | None:
        """Jog Z relative to the current position and run it live."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_z_rel", z)
        )

    def gluescript_jog_u_rel(self, u: float | None = None) -> list[str] | None:
        """Jog U relative to the current position and run it live."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("jog_u_rel", u)
        )

    def gluescript_home(self) -> list[str] | None:
        """Home the X and Y axes and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("home")
        )

    def gluescript_home_z(self) -> list[str] | None:
        """Home the Z axis and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("home_z")
        )

    def gluescript_home_u(self) -> list[str] | None:
        """Home the U axis and run it on the live session."""
        return self._gluescript_bridge(
            lambda: self._gluescript_live_command("home_u")
        )

    def gluescript_get_gluescript(self) -> list[str]:
        """Return a copy of the driver's gluescript (empty when no driver)."""
        return self._gluescript_bridge(
            lambda: list(self._ruida_driver.gluescript)
            if self._ruida_driver is not None
            else []
        )

    def gluescript_get_rpascript(self) -> list[str]:
        """Return a copy of the staged rpascript (empty when no driver)."""
        return self._gluescript_bridge(
            lambda: list(self._ruida_driver.rpascript)
            if self._ruida_driver is not None
            else []
        )

    def gluescript_job_complete(self) -> bool:
        """Return whether the job is complete (False when no driver)."""
        return self._gluescript_bridge(
            lambda: self._ruida_driver.job_complete
            if self._ruida_driver is not None
            else False
        )

    def _copy_staged_rpascript_to_loaded(self) -> None:
        """Mirror the staged rpascript into the TUI's loaded-script slot.

        Makes the generated rpascript available exactly as if it had been
        /load-ed from a .rds file: viewable via /list script and editable
        via /edit. Deliberately leaves _loaded_script_path and _plot_source
        untouched (user decision) — the path only feeds the bare-/save
        preselect and the plot label stays stale until a file is loaded.
        """
        driver = self._ruida_driver
        # NOTE: non-empty is guaranteed at the call sites (stage/run raise on
        # failure; _apply_gluescript_lines pre-validates; new_gluescript
        # empties driver.rpascript), so this guard never silently diverges.
        if driver.rpascript:
            self._loaded_script = list(driver.rpascript)  # copy, never alias
        else:
            self._log_warning(
                "GlueScript: staged rpascript unexpectedly empty — "
                "loaded-script slot left unchanged."
            )

    def _cmd_gluescript(self, args: str) -> None:
        """Handle /gluescript subcommands for high-level scripting."""
        tokens = args.strip().split()
        if not tokens:
            self._log_info("Usage: /gluescript <subcommand> \\[args]")
            return

        sub = tokens[0].lower()

        if sub == "new":
            # A fresh job wipes any preserved transcript BEFORE the driver
            # is (re)created — avoids restore-then-wipe waste and the
            # misleading "restored N lines" path.
            self._preserved_gluescript = None

        if sub == "run":
            # Running needs a live controller — never lazy-create here.
            if self._ruida_driver is None or not self._ruida_driver.is_connected:
                self._log_error("No active session to run gluescript.")
                return
        elif sub in (
            "new", "show", "list",
            "stage", "save", "load", "edit",
        ):
            # Session-less subcommands work on pure in-memory state; the
            # driver exists just to hold the transcript.
            self._ensure_gluescript_driver()

        driver = self._ruida_driver

        if sub == "new":
            label = " ".join(tokens[1:]).strip() or "New Job"
            driver.declare_job(label=label, ref_point="MACHINE")
            self._gluescript_was_run = False
            self._gluescript_cglu_path = None
            # Wipe the loaded-script slot too: the previous job's rpascript
            # no longer exists. _plot_source is intentionally NOT cleared
            # (user decision) — the stale label is accepted until a file is
            # loaded.
            self._loaded_script = []
            self._loaded_script_path = None
            self._log_info(
                f"GlueScript: New job started (label={label!r}, ref=MACHINE)."
            )

        elif sub == "show":
            layer_count = len(driver._layer_attributes) if hasattr(driver, '_layer_attributes') else 0
            action_count = sum(len(v) for v in (driver._layer_actions or {}).values())
            rpa_staged = len(driver.rpascript) if driver.rpascript else 0
            self._log_info(
                f"GlueScript: {len(driver.gluescript)} commands, "
                f"{layer_count} layer(s), "
                f"{action_count} action(s), "
                f"{rpa_staged} rpascript lines"
            )

        elif sub == "stage":
            try:
                if not self._finalize_gluescript_job(driver):
                    return
                driver.stage_gluescript()
                self._copy_staged_rpascript_to_loaded()
                self._log_info(
                    f"GlueScript: Staged {len(driver.rpascript)} rpascript lines."
                )
            except RuntimeError as e:
                self._log_error(f"Stage failed: {e}")

        elif sub == "run":
            try:
                if not self._finalize_gluescript_job(driver):
                    return
                driver.stage_gluescript()
                self._copy_staged_rpascript_to_loaded()
                driver.run_job()
                self._gluescript_was_run = True
                self._log_info(f"GlueScript: Executed job ({len(driver.rpascript)} rpascript lines).")
            except RuntimeError as e:
                self._log_error(f"Run failed: {e}")

        elif sub == "save":
            rest = args.strip().split(None, 1)
            path = os.path.expanduser(rest[1].strip()) if len(rest) > 1 else ""
            if not path:
                self._log_error("Usage: /gluescript save <path>")
                return
            if not driver.gluescript:
                self._log_error("GlueScript: Nothing to save (gluescript is empty).")
                return
            if not driver.job_complete:
                self._log_warning(
                    "Transcript has no end_job() — load will reject this file. "
                    "Run /gluescript stage to finalize the job first."
                )
            if "." not in os.path.basename(path):
                path += ".cglu"
            try:
                with open(path, "w") as f:
                    f.write("\n".join(driver.gluescript) + "\n")
                self._log_info(f"GlueScript saved to {path} ({len(driver.gluescript)} lines)")
                self._gluescript_cglu_path = path
            except PermissionError:
                self._log_error(f"Permission denied: {path}")
            except OSError as e:
                self._log_error(f"Error writing {path}: {type(e).__name__}: {e}")

        elif sub == "load":
            rest = args.strip().split(None, 1)
            path = os.path.expanduser(rest[1].strip()) if len(rest) > 1 else ""
            if not path:
                self._log_error("Usage: /gluescript load <path>")
                return
            if "." not in os.path.basename(path):
                path += ".cglu"
            try:
                with open(path, "r") as f:
                    content = f.read()
            except FileNotFoundError:
                self._log_error(f"File not found: {path}")
                return
            except PermissionError:
                self._log_error(f"Permission denied: {path}")
                return
            except UnicodeDecodeError:
                self._log_error(f"File is not a valid text file: {path}")
                return
            except Exception as e:
                self._log_error(f"Error reading {path}: {type(e).__name__}: {e}")
                return
            lines = content.splitlines()
            if not [ln for ln in lines if ln.strip()]:
                self._log_error(f"File is empty or contains only blank lines: {path}")
                return
            result = self._apply_gluescript_lines(lines, "on load", f"in {path}", "Load")
            if result is None:
                return
            kept, staged_count = result
            self._gluescript_was_run = False
            self._gluescript_cglu_path = path
            self._log_info(
                f"Loaded {len(kept)} gluescript lines from {path}, "
                f"staged {staged_count} rpascript lines"
            )

        elif sub == "edit":
            if not driver.gluescript:
                self._log_error(
                    "GlueScript: Nothing to edit (gluescript is empty). "
                    "Use /gluescript new or /gluescript load first."
                )
                return

            def on_edit(edited: list[str] | None) -> None:
                if edited is None:
                    self._log_info("GlueScript: Edit cancelled.")
                    return
                applied = self._apply_gluescript_lines(edited, "after edit", "after edit", "Edit")
                if applied is None:
                    return
                kept, staged_count = applied
                self._gluescript_was_run = False
                self._log_info(
                    f"GlueScript: Edited — {len(kept)} gluescript lines, "
                    f"staged {staged_count} rpascript lines"
                )

            self.push_screen(
                ScriptEditor("\n".join(driver.gluescript), title="Edit GlueScript"),
                on_edit,
            )

        elif sub == "list":
            if not driver.gluescript:
                self._log_info("GlueScript: No gluescript commands.")
                return
            for i, line in enumerate(driver.gluescript):
                self._log_widget.write(f"[dim]{i:4d}:[/dim] {line}")

        else:
            self._log_error(f"Unknown gluescript subcommand: {sub}")
            self._log_info("Available: new, show, stage, run, save, load, edit, list")

    def _apply_gluescript_lines(
        self,
        lines: list[str],
        live_ctx: str,
        where_ctx: str,
        fail_prefix: str,
    ) -> tuple[list[str], int] | None:
        """Filter live-only lines, validate, and apply a gluescript to the driver.

        Shared pipeline for ``/gluescript load`` and ``/gluescript edit``:
        drops live-only jog/home lines with a warning, requires at least one
        stageable command, validates on a throwaway GlueScript instance
        (suppressing the inline() staging warning), then applies via
        ``driver.stage_gluescript()``. Returns the kept lines and the number
        of staged rpascript lines (the length of the driver's rpascript
        right after apply), or None if the input cannot be staged (the
        error is already logged).
        """
        driver = self._ruida_driver
        # Defensive only — _cmd_gluescript ensures a driver exists before
        # dispatching load/edit, so this guard is currently unreachable.
        if driver is None:
            self._log_error("No active session. Use 'session start' first.")
            return None
        gluescript_parser = GlueScript()
        kept_lines: list[str] = []
        live_only_dropped = False
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                kept_lines.append(line)
                continue
            try:
                name, _args = gluescript_parser._parse_gluescript_line(line)
            except (ValueError, SyntaxError):
                kept_lines.append(line)
                continue
            if name in GlueScript.LIVE_ONLY_COMMANDS:
                live_only_dropped = True
                self._log_warning(
                    f"GlueScript: ignoring live-only command line {live_ctx}: {line.strip()}"
                )
                continue
            kept_lines.append(line)
        if not [ln for ln in kept_lines if ln.strip() and not ln.lstrip().startswith("#")]:
            if live_only_dropped:
                self._log_error(
                    f"GlueScript: no stageable commands {where_ctx} "
                    "(live-only commands — jogs and homing — were ignored)"
                )
            else:
                self._log_error(f"GlueScript: no stageable commands {where_ctx}")
            return None
        try:
            validation = GlueScript()
            validation._warn_inline = False
            validation.stage_gluescript(kept_lines)
        except RuntimeError as e:
            self._log_error(f"{fail_prefix} failed: {e}")
            return None
        driver.stage_gluescript(kept_lines)
        self._copy_staged_rpascript_to_loaded()
        return kept_lines, len(driver.rpascript)

    def _handle_live_command(self, line: str) -> None:
        """Dispatch a bare live-only command (jog or home) to the driver."""
        try:
            tokens = line.split()
            name = tokens[0]
            args = tokens[1:]

            driver = self._ruida_driver
            if driver is None:
                self._log_error(
                    "No active session. Use 'session start udp=<IP> usb=<device>' first."
                )
                return

            method = getattr(driver, name, None)
            if method is None:
                self._log_error(f"Unknown command: {name}")
                return

            values: list[float] = []
            for a in args:
                msg = f"Invalid number for {name}: '{a}'"
                try:
                    v = float(a)
                except ValueError:
                    self._log_error(msg)
                    return
                if not math.isfinite(v):
                    self._log_error(msg)
                    return
                values.append(v)

            # Movement jogs mutate position tracking, and home commands are
            # machine actions, so check connectivity before invoking them;
            # is_connected does not guarantee the background script runner
            # thread is alive — run() raises RuntimeError when it isn't.
            if not name.startswith("jog_set_") and not driver.is_connected:
                self._log_warning(f"{name} ignored — no active session")
                return

            try:
                result = method(*values)
            except TypeError:
                self._log_error(f"Usage: {self._cmd_descriptions.get(name, name)}")
                return

            if isinstance(result, list):
                if not result:
                    self._log_warning(f"{name} ignored — command produced no rpascript lines")
                    return
                try:
                    driver.run(result)
                except RuntimeError as e:
                    self._log_warning(f"{name} not sent — {e}")
                    return
                self._log_info(f"{name} sent to controller")
            else:
                self._log_info(f"{name} applied")
        except Exception as e:
            self._log_error(f"{type(e).__name__}: {e}")

    def _cmd_edit(self, args: str = "") -> None:
        """Open the loaded script in a full-screen editor."""

        def on_edit(result: list[str] | None) -> None:
            if result is not None:
                self._loaded_script = result
                self._log_info(f"Script updated: {len(result)} lines")

        text = "\n".join(self._loaded_script) if self._loaded_script else ""
        self.push_screen(ScriptEditor(text, title="Edit loaded script"), on_edit)

    @staticmethod
    def _file_extensions_for_cmd(cmd: str) -> set[str] | None:
        """Return allowed file extensions for a file-path command.

        Returns None to allow all extensions, or a set of lowercase extensions.
        """
        if cmd in ("/load", "/head", "/tail"):
            return {".rds"}
        if cmd == "/import":
            return {".log", ".txt", ".rd"}
        if cmd in ("/save", "/save job", "/save script", "/save as"):
            return None  # All files
        if cmd == "/export":
            return {".rd"}
        if cmd in ("/gluescript save", "/gluescript load"):
            return {".cglu"}
        return set()  # Changed from None to set() — unknown commands show no files

    def _resolve_start_path(self, path_part: str) -> Path:
        """Resolve a partial path to a starting directory for the file browser.

        Handles ~/ expansion and falls back to cwd on unresolvable paths.
        """
        expanded = os.path.expanduser(path_part.strip())
        if not expanded:
            return Path.cwd()
        if os.path.isdir(expanded):
            return Path(expanded)
        if os.path.isfile(expanded):
            return Path(expanded).parent
        # Partial path — try parent directory
        parent = os.path.dirname(expanded)
        if parent and os.path.isdir(parent):
            return Path(parent)
        return Path.cwd()

    def _check_file_browse_trigger(self, value: str) -> tuple[str | None, str]:
        """Check if the input value is a file-path command with at least one space.

        Returns (cmd, path_part) if triggered, or (None, '') if not.
        The cmd is a slash-prefixed command like '/load', '/save', etc.
        The path_part is whatever the user typed after the command (may be empty).
        """
        if not value.startswith("/"):
            return (None, "")

        # Find first space to split command from rest
        space_idx = value.find(" ")
        if space_idx == -1:
            return (None, "")  # No space yet — still typing command name

        cmd = value[:space_idx]
        rest = value[space_idx:].strip()

        # Simple path-taking commands: /load, /head, /tail, /import
        simple_cmds = {"/load", "/head", "/tail", "/import", "/export"}
        if cmd in simple_cmds:
            return (cmd, rest)

        # /save job <path> or /save script <path> or /save as <path>
        if cmd == "/save":
            if rest == "job" or rest.startswith("job "):
                path_part = rest[3:].strip() if len(rest) > 3 else ""
                return ("/save job", path_part)
            if rest == "script" or rest.startswith("script "):
                path_part = rest[6:].strip() if len(rest) > 6 else ""
                return ("/save script", path_part)
            if rest == "as" or rest.startswith("as "):
                path_part = rest[2:].strip() if len(rest) > 2 else ""
                return ("/save as", path_part)
            return ("/save", self._loaded_script_path or "")

        # /gluescript save <path> or /gluescript load <path>
        if cmd == "/gluescript":
            if rest == "save" or rest.startswith("save "):
                path_part = rest[4:].strip() if len(rest) > 4 else ""
                if not path_part:
                    path_part = self._gluescript_cglu_path or ""
                return ("/gluescript save", path_part)
            if rest == "load" or rest.startswith("load "):
                path_part = rest[4:].strip() if len(rest) > 4 else ""
                if not path_part:
                    path_part = self._gluescript_cglu_path or ""
                return ("/gluescript load", path_part)

        return (None, "")

    def _get_file_completions(self, cmd: str, path_part: str) -> list[tuple[str, bool]]:
        """Get file/directory completions for a given command and path.

        Returns list of (name, is_dir) tuples, sorted with directories first.
        Directories are always included. Files are filtered by command extensions.
        """
        allowed_exts = self._file_extensions_for_cmd(cmd)
        if allowed_exts is not None and len(allowed_exts) == 0:
            return []  # Unknown command, no files shown

        # Split path_part to get directory and prefix
        if path_part and "/" in path_part:
            dir_part, prefix = path_part.rsplit("/", 1)
        elif path_part:
            dir_part, prefix = "", path_part
        else:
            dir_part, prefix = "", ""

        # Resolve the directory
        start_dir = self._resolve_start_path(dir_part)

        # Store for later use
        self._file_completion_dir = str(start_dir)
        self._file_completion_prefix = prefix

        completions: list[tuple[str, bool]] = []
        try:
            with os.scandir(start_dir) as entries:
                for entry in entries:
                    name = entry.name
                    # Skip hidden files (like bash default)
                    if name.startswith("."):
                        continue
                    if entry.is_dir():
                        completions.append((name + "/", True))
                    elif entry.is_file():
                        # Filter by extension
                        if allowed_exts is None or name.lower().endswith(tuple(allowed_exts)):
                            completions.append((name, False))
        except PermissionError:
            return []
        except OSError:
            return []

        # Sort: directories first, then files, alphabetically within each group
        dirs = sorted((n, d) for n, d in completions if d)
        files = sorted((n, d) for n, d in completions if not d)
        return dirs + files

    def _render_file_completions(self) -> None:
        """Render file completions in the suggest popup."""
        self._suggest_popup.clear()
        if not self._file_completions:
            self._suggest_matches = []
            self._suggest_popup.write("[dim]No matching files[/dim]")
            return

        # Filter by prefix
        prefix = self._file_completion_prefix.lower()
        filtered = [
            (name, is_dir) for name, is_dir in self._file_completions
            if name.lower().startswith(prefix)
        ]

        if not filtered:
            self._suggest_matches = []
            self._suggest_popup.write("[dim]No matching files[/dim]")
            return

        # Show current directory
        display_dir = self._file_completion_dir
        cwd = str(Path.cwd())
        if display_dir.startswith(cwd):
            display_dir = "." + display_dir[len(cwd):]
        self._suggest_popup.write(f"[bold]Files in {display_dir}:[/bold]")

        # Compute visible window centered on the selected entry — same math as
        # _render_suggest_popup so the selection stays on screen when the list
        # exceeds max_lines.
        max_items = self._suggest_popup.max_lines - 1  # reserve 1 line for header
        total = len(filtered)
        half = max_items // 2
        start = max(0, self._suggest_selected - half)
        end = min(total, start + max_items)
        # If we're below max_items, shift window up
        if end - start < max_items:
            start = max(0, end - max_items)

        # Render matches within the window
        for i in range(start, end):
            name, is_dir = filtered[i]
            line = f"  {name}"
            if is_dir:
                line = f"  [bold]{name}[/bold]"
            if i == self._suggest_selected:
                self._suggest_popup.write(f"[reverse]{line}[/reverse]")
            else:
                self._suggest_popup.write(line)

        # Store the FULL filtered list for navigation — Enter/Tab/arrows work
        # over every match even though only the window is rendered.
        self._suggest_matches = [name for name, _ in filtered]

    def _clear_file_completion_state(self) -> None:
        """Reset file completion state and dismiss popup if in file mode."""
        self._file_completions = []
        self._file_completion_dir = ""
        self._file_completion_cmd = ""
        self._file_completion_prefix = ""
        if self._suggest_mode == "file":
            self._suggest_mode = ""
            self._suggest_matches = []
            if self._suggest_popup.is_attached:
                self._suggest_popup.remove()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _start_session(
        self, udp: str | None = None, usb: str | None = None, to: str | None = None,
        magic: str | None = None,
    ) -> None:
        """Connect to a Ruida controller and start the script runner.

        Creates an RdDriver (or reuses a detached one from session-less
        gluescript use), registers TUI listeners, then calls
        driver.start() which creates the session, opens the transport,
        starts the script runner and status monitor, and returns.
        """
        # Resolve None params against last-used values so params persist
        # across session end/start cycles even when RdDriver is discarded.
        if udp is None:
            udp = self._last_udp_host
        if usb is None:
            usb = self._last_usb_device

        if not udp and not usb:
            self._log_error(
                "No connection parameters. Provide udp=<host> or usb=<device>."
            )
            return

        # Parse to=/magic before the reuse branch so both branches honor
        # the same validation.
        timeout: float | None = None
        if to is not None:
            try:
                timeout = _parse_timeout_spec(to)
            except ValueError as e:
                self._log_error(str(e))
                return

        # Parse optional magic number
        if magic is not None:
            try:
                if magic.lower().startswith("0x"):
                    self._last_magic = int(magic, 16) & 0xFF
                else:
                    raise ValueError
            except (ValueError, AttributeError):
                self._log_error(f"Invalid magic number: {magic}")
                return

        # Live session — reconnect immediately using last-used params.
        if self._ruida_driver is not None and self._ruida_driver._session is not None:
            self._ruida_driver.start(udp_host=udp, usb_device=usb, magic=self._last_magic)
            self._last_udp_host = udp
            self._last_usb_device = usb
            return

        # Check pyserial availability before attempting USB connection
        if usb:
            try:
                import serial  # noqa: F401
            except ImportError:
                self._log_error(
                    "pyserial is not installed. "
                    "Install it with: pip install ruida-pa\\[serial]"
                )
                return

        loop = asyncio.get_running_loop()

        if udp:
            resolved = await loop.run_in_executor(None, _resolve_hostname, udp, 50200)
            if resolved is None:
                self._log_error(
                    f"Unable to resolve '{udp}'. Check the address and try again."
                )
                return
            udp = resolved

        try:
            self._log_info(f"Connecting (udp={udp}, usb={usb})...")

            # Fresh driver when none exists (registers listeners, syncs
            # head/tail, restores preserved gluescript); otherwise reuse
            # the detached driver from session-less gluescript use — its
            # listeners and scripts were wired at creation.
            driver = (
                self._ruida_driver if self._ruida_driver is not None
                else self._create_driver()
            )
            # Assign before start() so the except path can preserve the restored
            # transcript via _release_driver() (stop() is idempotent on a
            # half-started driver). On the detached-reuse path this is a no-op.
            self._ruida_driver = driver

            opened = driver.start(udp_host=udp, usb_device=usb, magic=self._last_magic)
            self._last_udp_host = udp
            self._last_usb_device = usb
            if not opened:
                self._log_info("Transport not available yet (retrying in background)")

            # Wait for connection with optional timeout + cancel support
            self._session_connected.clear()
            self._session_start_cancel.clear()

            connect_task = asyncio.create_task(self._session_connected.wait())
            cancel_task = asyncio.create_task(self._session_start_cancel.wait())

            done, pending = await asyncio.wait(
                [connect_task, cancel_task],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel unfinished tasks
            for task in pending:
                task.cancel()

            if connect_task in done:
                self._log_info("Session started successfully")
            elif cancel_task in done:
                self._log_error("Session start cancelled by user")
                await self._teardown_session()
                return
            else:
                self._log_error("Session connection timeout")
                await self._teardown_session()
                return

            self._update_status_bar()

            # Re-enable connection logging if it was already on
            if self._connection_logging_enabled:
                self._enable_connection_logging()

        except Exception as e:
            self._log_error(f"Failed to start session: {e}")
            if self._ruida_driver is not None:
                self._disable_connection_logging()
                self._session_connected.clear()
                self._release_driver()

    async def _stop_session(self) -> None:
        """Disconnect from the controller and clean up resources."""
        if self._ruida_driver is None:
            self._log_info("No active session.")
            return

        try:
            self._disable_connection_logging()
            self._release_driver()
            self._session_connected.clear()
            self._log_info("Session ended")
            self._update_status_bar()

        except Exception as e:
            self._log_error(f"Error stopping session: {e}")
            self._session_connected.clear()
            self._release_driver()

    async def _start_server(
        self, host: str | None = None, port: int | None = None,
        cert: str | None = None, key: str | None = None,
        token: str | None = None,
    ) -> None:
        """Start the RPyC server in a background thread.

        Resolves None params against last-used values so params persist
        across server start/stop cycles.

        Localhost/127.0.0.1 connections skip TLS and authentication.
        """
        # Resolve None params against last-used values
        if host is None:
            host = self._last_server_host
        if port is None:
            port = self._last_server_port
        if cert is None:
            cert = self._last_server_cert
        if key is None:
            key = self._last_server_key
        if token is None:
            token = self._last_server_token

        if self._rpyc_server is not None:
            self._log_error("RPC server is already running. Use 'server stop' first.")
            return

        # Store last-used values
        self._last_server_host = host
        self._last_server_port = port
        self._last_server_cert = cert
        self._last_server_key = key
        self._last_server_token = token

        # Localhost skips TLS and auth
        is_local = host in ("127.0.0.1", "::1", "localhost")
        if is_local:
            cert = None
            key = None
            token = None

        from rpalib.rpyc_service import start_rpyc_server

        def _run():
            """Create, register, and start the RPyC server (blocking)."""
            server = start_rpyc_server(
                self,
                host=host,
                port=port,
                cert_path=cert,
                key_path=key,
                token=token,
                auto_start=False,
            )
            self._rpyc_server = server
            self.post_message(Callback(
                lambda: self._log_info(f"RPC server started on {host}:{port}")
            ))
            server.start()  # Blocks until server stops

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._log_info(f"Starting RPC server on {host}:{port}...")

    async def _stop_server(self) -> None:
        """Stop the RPyC server."""
        if self._rpyc_server is None:
            self._log_info("No RPC server running.")
            return

        host = self._last_server_host
        port = self._last_server_port
        server = self._rpyc_server
        self._rpyc_server = None
        try:
            server.close()
            self._log_info(f"RPC server on {host}:{port} stopped.")
        except Exception as e:
            self._log_error(f"Error stopping RPC server: {e}")

    async def _teardown_session(self) -> None:
        """Tear down the current session (stop driver, disconnect).

        Used by timeout/cancel paths in _start_session.
        """
        if self._ruida_driver is not None:
            self._disable_connection_logging()
            self._release_driver()
            self._session_connected.clear()

        self._session_connected.clear()
        self._update_status_bar()

    # ------------------------------------------------------------------
    # AppAdapter-compatible interface (called from driver background thread)
    # ------------------------------------------------------------------

    def on_status_event(self, event: RdStatusEvent | StatusDict) -> None:
        """Handle a status event from the driver.

        Called from the driver's background thread. Updates data dicts directly
        (thread-safe via GIL) so _update_status_bar() can read fresh data even
        when the message pump is busy. UI updates go through post_message(Callback).
        """
        # --- Data updates (direct, thread-safe via GIL) ---
        if isinstance(event, dict):
            for key, value in event.items():
                if key == "MEM_CURRENT_POSITION_X":
                    raw, formatted = value
                    self._position["X"] = (raw, formatted)
                    self._last_coord_change["X"] = time.time()
                elif key == "MEM_CURRENT_POSITION_Y":
                    raw, formatted = value
                    self._position["Y"] = (raw, formatted)
                    self._last_coord_change["Y"] = time.time()
                elif key == "MEM_CURRENT_POSITION_Z":
                    raw, formatted = value
                    self._position["Z"] = (raw, formatted)
                    self._last_coord_change["Z"] = time.time()
                elif key == "MEM_CURRENT_POSITION_U":
                    raw, formatted = value
                    self._position["U"] = (raw, formatted)
                    self._last_coord_change["U"] = time.time()
                elif key == "MEM_CARD_ID":
                    raw, formatted = value
                    self._position["Card"] = (raw, formatted)
                elif key == "MEM_BED_SIZE_X":
                    raw, formatted = value
                    self._position["BedX"] = (raw, formatted)
                elif key == "MEM_BED_SIZE_Y":
                    raw, formatted = value
                    self._position["BedY"] = (raw, formatted)
                elif key == "MEM_MACHINE_STATUS":
                    raw, formatted = value
                    self._machine_status = int(raw)
                    self._machine_status_formatted = formatted
                elif key in (
                    "MACHINE_STATUS_MOVING",
                    "MACHINE_STATUS_LAYER_END",
                    "MACHINE_STATUS_JOB_RUNNING",
                ):
                    self._status_bits[key] = bool(value)
                else:
                    logging.getLogger(__name__).warning(
                        "Unknown status key in StatusDict: %s = %r", key, value
                    )
            self._event_count += 1
            self._status_log_buffer.append(f"[STATUS] {dict(event)}")
        else:
            # RdStatusEvent — only increment counter directly.
            # _session_disconnected / _session_connected are handled in _update()
            # below, which checks the flag BEFORE setting it.
            self._event_count += 1
            if self._logging_enabled:
                self._status_log_buffer.append(f"[STATUS] {event.value}")

        # --- UI updates (via message pump) ---
        def _update() -> None:
            if isinstance(event, dict):
                self._drain_status_log_buffer()
                self._update_status_bar()
                return

            # Script events received via status listener path
            self._drain_status_log_buffer()
            # Determine transport type for log messages
            transport_type = ""
            if (
                self._ruida_driver is not None
                and self._ruida_driver._session is not None
            ):
                transport = self._ruida_driver._session.transport
                if transport.is_usb:
                    transport_type = "USB"
                elif transport.is_udp:
                    transport_type = "UDP"
            suffix = f" ({transport_type})" if transport_type else ""

            if event in (RdStatusEvent.DISCONNECTED, RdStatusEvent.TERMINATED):
                if not self._session_disconnected or event is RdStatusEvent.TERMINATED:
                    msg = (
                        "Disconnected (session ended)"
                        if event is RdStatusEvent.TERMINATED
                        else f"Disconnected{suffix}"
                    )
                    self._log_info(msg)
                self._session_disconnected = True
                self._session_connected.clear()
            elif event is RdStatusEvent.CONNECTED:
                if self._session_disconnected:
                    self._log_info(f"Connected{suffix}")
                self._session_disconnected = False
                self._session_connected.set()
            self._update_status_bar()

        self.post_message(Callback(_update))

    def on_reply_data(self, replies: list[str]) -> None:
        """Handle formatted reply data from the driver.

        Logs script command replies to the main TUI window.
        Thread-safe: bridges from driver thread to asyncio thread.
        """
        self.post_message(Callback(lambda: self._write_replies(replies)))

    def _write_replies(self, replies: list[str]) -> None:
        """Write reply strings to the main log area (asyncio thread only)."""
        for formatted in replies:
            self._log_widget.write(f"  ← {formatted}")

    def on_error(self, message: str) -> None:
        """Handle an error condition. Thread-safe via post_message(Callback(...))."""

        def _update() -> None:
            self._log_error(message)

        self.post_message(Callback(_update))

    def run_script(self, script: list[str], auto_checksum: bool = False) -> None:
        """Queue a script for execution.

        Args:
            script: List of rpascript-formatted command lines.
            auto_checksum: If True, auto-calculate END_JOB on mismatch
                with a warning instead of raising.

        Thread-safe: can be called from any thread.
        """
        if self._ruida_driver is None:

            def _error() -> None:
                self._log_error("No active session to run script.")

            if threading.get_ident() == getattr(self, "_thread_id", None):
                _error()
            else:
                self.post_message(Callback(_error))
            return

        def _run() -> None:
            try:
                self._ruida_driver.run(script, auto_checksum=auto_checksum)
                self._script_count += len(script)
                self._update_status_bar()
            except RuntimeError as e:
                self._log_error(str(e))

        if threading.get_ident() == getattr(self, "_thread_id", None):
            _run()
        else:
            self.post_message(Callback(_run))

    def set_head_script(self, script: list[str]) -> None:
        """Set the head script to prepend to job execution. Thread-safe.

        Stores locally and pushes to the driver if active.
        """
        self._head_script = list(script)
        if self._ruida_driver is not None:
            self._ruida_driver.set_head_script(self._head_script)
        self._log_info(f"[RPC] set_head_script({len(script)} lines)")

    def set_tail_script(self, script: list[str]) -> None:
        """Set the tail script to append to job execution. Thread-safe.

        Stores locally and pushes to the driver if active.
        """
        self._tail_script = list(script)
        if self._ruida_driver is not None:
            self._ruida_driver.set_tail_script(self._tail_script)
        self._log_info(f"[RPC] set_tail_script({len(script)} lines)")

    def get_head_script(self) -> list[str]:
        """Return the current head script. Thread-safe.

        Returns a copy so callers cannot mutate internal state.
        """
        return list(self._head_script)

    def get_tail_script(self) -> list[str]:
        """Return the current tail script. Thread-safe.

        Returns a copy so callers cannot mutate internal state.
        """
        return list(self._tail_script)

    def run_job(self, job: list[str] | None = None, auto_checksum: bool = False) -> None:
        """Queue a job for execution, composing head + job + tail.

        Delegates to driver.run_job() which composes head + job + tail
        at queue time. Thread-safe: can be called from any thread.

        When ``job`` is omitted (None), the driver runs the rpascript most
        recently staged by ``stage_gluescript()``; the driver raises
        RuntimeError when nothing has been staged (logged here).

        Args:
            job: List of rpascript-formatted command lines (job body only).
                When None, the staged rpascript from ``stage_gluescript()``
                is run.
            auto_checksum: If True, auto-calculate END_JOB on mismatch.
        """
        if self._ruida_driver is None:

            def _error() -> None:
                self._log_error("No active session to run job.")

            if threading.get_ident() == getattr(self, "_thread_id", None):
                _error()
            else:
                self.post_message(Callback(_error))
            return

        def _run() -> None:
            try:
                self._ruida_driver.run_job(job, auto_checksum=auto_checksum)
            except RuntimeError as e:
                self._log_error(str(e))

        if threading.get_ident() == getattr(self, "_thread_id", None):
            _run()
        else:
            self.post_message(Callback(_run))

    # ------------------------------------------------------------------
    # Introspection (?) subsystem
    # ------------------------------------------------------------------

    def _resolve_path(self, path: str) -> tuple[Any, str | None]:
        """Resolve a dotted path against the introspection object map.

        Returns (resolved_object, error_message).
        On success, error_message is None.
        On failure, resolved_object is None and error_message describes the issue.
        """
        # Handle 'self.' prefix for TuiAdapter itself
        if path.startswith("self."):
            obj = self
            remaining = path[5:]
        elif path == "self":
            return (self, None)
        else:
            # Split off the root object name
            parts = path.split(".", 1)
            root_name = parts[0]
            try:
                obj = self._introspect_map[root_name]()
            except KeyError:
                known = ", ".join(sorted(self._introspect_map.keys()))
                return (None, f"Unknown object: {root_name}. Known: {known}")
            remaining = parts[1] if len(parts) > 1 else ""

        # Walk the attribute chain
        if remaining:
            try:
                obj = functools.reduce(getattr, remaining.split("."), obj)
            except AttributeError as e:
                return (None, f"No such attribute: {path} ({e})")

        return (obj, None)

    def _handle_introspect(self, expr: str) -> str:
        """Handle a ?-prefixed introspection expression.

        No parentheses → variable view (repr).
        With parentheses → method call with args, or signature display if no args.
        """
        expr = expr.strip()
        if not expr:
            return "Usage: !<object>[.<attribute>] \\[args...]"

        # Split on first '(' to detect method call
        paren_idx = expr.find("(")
        if paren_idx == -1:
            # No parens: split on space for potential args
            parts = expr.split(None, 1)
            path = parts[0]
            args_raw = parts[1] if len(parts) > 1 else ""

            obj, err = self._resolve_path(path)
            if err:
                return err

            if args_raw:
                # Space-separated args → call the method
                args = self._parse_introspect_args(args_raw)
                try:
                    result = obj(*args)
                    return self._format_value(result)
                except TypeError as e:
                    return f"TypeError: {e}"
                except Exception as e:
                    return f"Error calling {path}: {type(e).__name__}: {e}"

            # No args → show signature for callables, repr for variables
            if callable(obj):
                return self._format_signature(obj)
            return self._format_value(obj)

        # Method call with parens
        path = expr[:paren_idx].strip()
        args_part = expr[paren_idx + 1 :]

        # Find matching close paren
        if not args_part.endswith(")"):
            return "Syntax error: unclosed parenthesis"
        args_str = args_part[:-1].strip()

        obj, err = self._resolve_path(path)
        if err:
            return err

        if not callable(obj):
            return f"{path} is not callable (type: {type(obj).__name__})"

        if not args_str:
            # No arguments — show signature
            return self._format_signature(obj)

        # Parse arguments
        args = self._parse_introspect_args(args_str)

        try:
            result = obj(*args)
            return self._format_value(result)
        except TypeError as e:
            return f"TypeError: {e}"
        except Exception as e:
            return f"Error calling {path}: {type(e).__name__}: {e}"

    def _format_signature(self, obj: Any) -> str:
        """Format an object's signature for display."""
        try:
            sig = inspect.signature(obj)
            return f"{getattr(obj, '__name__', type(obj).__name__)}{sig}"
        except (ValueError, TypeError):
            return repr(obj)

    def _format_value(self, value: Any) -> str:
        """Format a Python value for readable multi-line TUI display.

        Lists/tuples/dicts: one item per line with 2-space indentation.
        Multi-line strings (docstrings): literal line breaks.
        Other values: repr() output.
        """
        if isinstance(value, dict):
            if not value:
                return "{}"
            lines = ["{"]
            for k, v in value.items():
                v_fmt = self._format_value(v)
                if "\n" in v_fmt:
                    lines.append(f"  {repr(k)}:")
                    for sub in v_fmt.split("\n"):
                        lines.append(f"    {sub}")
                else:
                    lines.append(f"  {repr(k)}: {v_fmt}")
            lines.append("}")
            return "\n".join(lines)

        if isinstance(value, (list, tuple)):
            if not value:
                return "[]" if isinstance(value, list) else "()"
            bracket_open = "[" if isinstance(value, list) else "("
            bracket_close = "]" if isinstance(value, list) else ")"
            lines = [bracket_open]
            for item in value:
                item_fmt = self._format_value(item)
                for sub in item_fmt.split("\n"):
                    lines.append(f"  {sub}")
                lines[-1] += ","
            lines.append(bracket_close)
            return "\n".join(lines)

        if isinstance(value, str) and "\n" in value:
            # Multi-line string (docstring) — display with literal line breaks
            return value

        return repr(value)

    def _parse_introspect_args(self, args_str: str) -> list[Any]:
        """Parse a comma-separated argument string into Python values.

        Tries ast.literal_eval first. Falls back to hex→bytearray conversion
        for hex-formatted strings starting with 0x.
        """
        if not args_str:
            return []

        result = []
        for arg in args_str.split(","):
            arg = arg.strip()
            if not arg:
                continue

            # Try ast.literal_eval first
            try:
                val = ast.literal_eval(arg)
                result.append(val)
                continue
            except (ValueError, SyntaxError):
                pass

            # Try hex→bytearray conversion (starts with 0x, contains only hex chars)
            clean = arg[2:] if arg.startswith("0x") else arg
            if not clean:
                continue
            try:
                if (
                    all(c in "0123456789abcdefABCDEF" for c in clean)
                    and len(clean) % 2 == 0
                ):
                    val = bytearray.fromhex(clean)
                    result.append(val)
                    continue
            except ValueError:
                pass

            # Fallback: treat as string
            result.append(arg)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_script(self, line: str) -> None:
        """Log a script command to the log area with [SCRIPT] prefix."""
        if not hasattr(self, '_log_widget'):
            return
        self._log_widget.write(f"[SCRIPT] {line}")

    def _log_info(self, message: str) -> None:
        """Log an informational message in cyan."""
        if not hasattr(self, '_log_widget'):
            return
        self._log_widget.write(f"[bold cyan]{message}[/bold cyan]")

    def _log_error(self, message: str) -> None:
        """Log an error message in bold red."""
        if not hasattr(self, '_log_widget'):
            return
        self._log_widget.write(f"[bold red]ERROR: {message}[/bold red]")

    def _log_warning(self, message: str) -> None:
        """Log a warning message in bold yellow."""
        if not hasattr(self, '_log_widget'):
            return
        self._log_widget.write(f"[bold yellow]WARNING: {message}[/bold yellow]")

    def _update_status_bar(self) -> None:
        """Update the bottom status bar with connection info, counters, and position."""
        # Connection info
        if self._session_disconnected:
            conn = "[red]Disconnected[/red]"
        elif self._ruida_driver is not None and self._ruida_driver.is_connected:
            conn = "[green]Connected[/green]"
        elif self._ruida_driver is not None and self._ruida_driver._session is None:
            # Session-less gluescript driver — no session, not "Connecting".
            conn = "[red]Disconnected[/red]"
        elif self._ruida_driver is not None:
            conn = "[yellow]Connecting[/yellow]"
        else:
            conn = "[red]Disconnected[/red]"

        # Transport info
        if self._ruida_driver is not None and self._ruida_driver._session is not None:
            transport = self._ruida_driver._session.transport
            if transport.is_udp:
                transport_info = transport._udp_host
            elif transport.is_usb:
                transport_info = transport._usb_device
            else:
                transport_info = ""
        else:
            transport_info = ""

        # Counters
        counters = f"Events: {self._event_count}  Replies: {self._reply_count}  Scripts: {self._script_count}"

        # Machine info (Card, BedX, BedY) — use pre-formatted values from StatusDict
        machine_parts = []
        card = self._position.get("Card")
        if card is not None:
            _, formatted = card
            machine_parts.append(f"Card: {formatted}")
        else:
            machine_parts.append("Card: —")
        bedx = self._position.get("BedX")
        if bedx is not None:
            _, formatted = bedx
            machine_parts.append(f"BedX: [bold]{formatted}[/bold]")
        else:
            machine_parts.append("BedX: —")
        bedy = self._position.get("BedY")
        if bedy is not None:
            _, formatted = bedy
            machine_parts.append(f"BedY: [bold]{formatted}[/bold]")
        else:
            machine_parts.append("BedY: —")
        machine = "  ".join(machine_parts)

        # Machine status indicators (MOVE, LAYER, JOB)
        status_parts = []
        if self._status_bits["MACHINE_STATUS_MOVING"]:
            status_parts.append("[bold green]MOVE[/bold green]")
        else:
            status_parts.append("MOVE")
        if self._status_bits["MACHINE_STATUS_LAYER_END"]:
            status_parts.append("[bold green]LAYER[/bold green]")
        else:
            status_parts.append("LAYER")
        if self._status_bits["MACHINE_STATUS_JOB_RUNNING"]:
            status_parts.append("[bold green]JOB[/bold green]")
        else:
            status_parts.append("JOB")
        indicators = " ".join(status_parts)

        # Position — use pre-formatted values from StatusDict
        now = time.time()
        pos_parts = []
        for axis in ("X", "Y", "Z", "U"):
            v = self._position[axis]
            if v is not None:
                _, formatted = v
                if now - self._last_coord_change.get(axis, 0.0) < 2.0:
                    pos_parts.append(f"[bold yellow]{axis}: {formatted}[/bold yellow]")
                else:
                    pos_parts.append(f"{axis}: [bold]{formatted}[/bold]")
            else:
                pos_parts.append(f"{axis}: —")
        pos = "  ".join(pos_parts)

        # GlueScript state
        gluescript_info = ""
        if self._ruida_driver is not None:
            rpa_lines = len(self._ruida_driver.rpascript) if self._ruida_driver.rpascript else 0
            if rpa_lines > 0:
                status_symbol = "[green]R[/green]" if self._gluescript_was_run else "[yellow]S[/yellow]"
                gluescript_info = f"  |  GS:{len(self._ruida_driver.gluescript)} RPA:{rpa_lines} {status_symbol}"
            elif len(self._ruida_driver.gluescript) > 0:
                status_symbol = "[yellow]S[/yellow]"
                gluescript_info = f"  |  GS:{len(self._ruida_driver.gluescript)} RPA:0 {status_symbol}"

        self._status_bar.update(
            f"{conn}  {transport_info}  |  {indicators}  |  {machine}  |  {counters}  |  {pos}{gluescript_info}"
        )

    # ------------------------------------------------------------------
    # AppAdapter-compatible no-ops (TUI creates sessions on demand)
    # ------------------------------------------------------------------

    def create_driver_and_session(self) -> None:
        """AppAdapter interface — TUI creates sessions on demand via command input."""
        pass

    def start(self, udp_host: str | None = None, usb_device: str | None = None, magic: int | None = None) -> bool:
        """Start the driver session.

        Emulates RdDriver.start(). Creates a new RdDriver if none exists,
        registers TUI listeners, and delegates to RdDriver.start().

        Args:
            udp_host: UDP host address or hostname.
            usb_device: USB serial device path.
            magic: Optional swizzle magic number (default 0x88).

        Returns:
            True if transport opened immediately, False if retry needed.
        """
        if self._ruida_driver is None:
            self._ruida_driver = self._create_driver()

        result = self._ruida_driver.start(udp_host=udp_host, usb_device=usb_device, magic=magic)
        self._log_info(
            f"[RPC] driver.start(udp_host={udp_host!r}, usb_device={usb_device!r}, magic={magic!r}) -> {result}"
        )
        return result

    def stop(self) -> None:
        """AppAdapter interface — stop the driver if running."""
        if self._ruida_driver is not None:
            self._log_info("[RPC] driver.stop()")
            self._release_driver()
            self._session_connected.clear()

    def run(
        self,
        script: list[str] | None = None,
        auto_checksum: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Queue a script for execution, or start the TUI event loop.

        When *script* is None, delegates to ``App.run(self, **kwargs)`` so that
        ``run_tui()`` can call ``app.run()`` with no arguments and enter the
        Textual event loop normally.

        When *script* is provided, emulates ``RdDriver.run()``: optionally
        displays script lines in the TUI (if auto-display is enabled via ``/list auto on``) and stores the script in ``_loaded_script``
        for ``/list`` access.

        Args:
            script: List of rpascript-formatted command lines, or None to
                start the TUI event loop.
            auto_checksum: If True, auto-calculate END_JOB on mismatch.
            **kwargs: Forwarded to ``App.run()`` when *script* is None.
        """
        if script is None:
            # Called from run_tui() — start the TUI event loop
            return App.run(self, **kwargs)

        # Emulation path — display script in TUI log
        self._loaded_script = list(script)
        self._plot_source = "[RPC]"

        if self._auto_display_script:
            max_lines = 200
            self._log_info(f"[RPC] Received script ({len(script)} lines):")
            self._log_widget.write("\n".join(f"  [RPC] {line}" for line in script[:max_lines]))
            if len(script) > max_lines:
                self._log_widget.write(
                    f"  [dim]... ({len(script)} total, showing first {max_lines})[/dim]"
                )
        else:
            # Brief preview when auto-display is off
            if len(script) <= 3:
                preview = " / ".join(script)
            else:
                preview = " / ".join(script[:3]) + f" ... ({len(script)} lines)"
            self._log_info(f"[RPC] driver.run({preview})")

        if self._dryrun:
            self._log_info("[DRY-RUN] Script execution skipped — use /run after /dryrun off")
            return

        self.run_script(self._loaded_script, auto_checksum=auto_checksum)

    def register_status_listener(
        self, listener: Callable[[RdStatusEvent | StatusDict], None]
    ) -> None:
        """Register a status event listener.

        Emulates RdDriver.register_status_listener(). Delegates to the
        underlying driver if active, raises RuntimeError otherwise.
        """
        if self._ruida_driver is None:
            raise RuntimeError("No active driver. Call start() first.")
        self._ruida_driver.register_status_listener(listener)
        self._log_info(f"[RPC] register_status_listener({listener!r})")

    def register_error_listener(self, listener: Callable[[str], None]) -> None:
        """Register an error listener.

        Emulates RdDriver.register_error_listener().
        """
        if self._ruida_driver is None:
            raise RuntimeError("No active driver. Call start() first.")
        self._ruida_driver.register_error_listener(listener)
        self._log_info(f"[RPC] register_error_listener({listener!r})")

    def register_reply_listener(self, listener: Callable[[list[str]], None]) -> None:
        """Register a reply listener.

        Emulates RdDriver.register_reply_listener().
        """
        if self._ruida_driver is None:
            raise RuntimeError("No active driver. Call start() first.")
        self._ruida_driver.register_reply_listener(listener)
        self._log_info(f"[RPC] register_reply_listener({listener!r})")

    def unregister_status_listener(
        self, listener: Callable[[RdStatusEvent | StatusDict], None]
    ) -> None:
        """Remove a previously registered status listener.

        Silently no-ops if the driver is not active (e.g., disconnected).
        """
        if self._ruida_driver is not None:
            self._ruida_driver.unregister_status_listener(listener)
            self._log_info(f"[RPC] unregister_status_listener({listener!r})")
        else:
            self._log_info(f"[RPC] unregister_status_listener skipped (no driver)")

    def unregister_error_listener(self, listener: Callable[[str], None]) -> None:
        """Remove a previously registered error listener.

        Silently no-ops if the driver is not active (e.g., disconnected).
        """
        if self._ruida_driver is not None:
            self._ruida_driver.unregister_error_listener(listener)
            self._log_info(f"[RPC] unregister_error_listener({listener!r})")
        else:
            self._log_info(f"[RPC] unregister_error_listener skipped (no driver)")

    def unregister_reply_listener(self, listener: Callable[[list[str]], None]) -> None:
        """Remove a previously registered reply listener.

        Silently no-ops if the driver is not active (e.g., disconnected).
        """
        if self._ruida_driver is not None:
            self._ruida_driver.unregister_reply_listener(listener)
            self._log_info(f"[RPC] unregister_reply_listener({listener!r})")
        else:
            self._log_info(f"[RPC] unregister_reply_listener skipped (no driver)")

    def cancel_script(self) -> None:
        """Cancel the currently running script.

        Emulates RdDriver.cancel_script().
        """
        if self._ruida_driver is not None:
            self._ruida_driver.cancel_script()
            self._log_info("[RPC] cancel_script()")

    @property
    def is_connected(self) -> bool:
        """Return whether the driver is connected.

        Emulates RdDriver.is_connected. Only logs when connection state changes.
        """
        current_state = self._ruida_driver is not None and self._ruida_driver.is_connected
        
        # Log only if state has changed from last known state
        if self._last_is_connected != current_state:
            self._log_info(f"[RPC] is_connected -> {current_state}")
            self._last_is_connected = current_state
        else:
            # Update last state even when no log (in case it was None initially)
            self._last_is_connected = current_state
        
        return current_state

    @property
    def machine_status(self) -> dict[int, Any]:
        """Return the current machine status dict.

        Emulates RdDriver.machine_status.
        """
        if self._ruida_driver is None:
            self._log_info("[RPC] machine_status -> {} (no driver)")
            return {}
        result = self._ruida_driver.machine_status
        self._log_info(f"[RPC] machine_status -> {len(result)} items")
        return result

    @staticmethod
    def format_reply_value(
        address: int, raw_reply: bytearray
    ) -> tuple[str | None, str]:
        """Format a single reply value.

        Emulates RdDriver.format_reply_value().
        """
        _log.info(f"[RPC] format_reply_value(addr=0x{address:04X}, raw_len={len(raw_reply)})")
        return RdDriver.format_reply_value(address, raw_reply)

    @staticmethod
    def format_reply(reply: bytearray) -> str:
        """Format a reply bytearray.

        Emulates RdDriver.format_reply().
        """
        _log.info(f"[RPC] format_reply(len={len(reply)})")
        return RdDriver.format_reply(reply)

    @staticmethod
    def format_reply_list(replies: list[bytearray]) -> list[str]:
        """Format a list of reply bytearrays.

        Emulates RdDriver.format_reply_list().
        """
        _log.info(f"[RPC] format_reply_list(count={len(replies)})")
        return RdDriver.format_reply_list(replies)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _on_exit_app(self) -> None:
        """Save command history, then clean up active session when TUI exits.


        Overrides the internal Textual lifecycle hook _on_exit_app (called when
        the app exits) to persist command history and tear down the session.
        In Textual 8.x, the shutdown message is ExitApp, which dispatches to
        _on_exit_app — NOT on_exit (which has no matching message class).
        """
        # Stop memory monitor timer to prevent widget access during teardown
        if self._mem_timer is not None:
            self._mem_timer.cancel()
            self._mem_timer = None
        self._save_command_history()
        if self._ruida_driver is not None:
            self._session_connected.clear()
            self._release_driver()
        await super()._on_exit_app()

    @staticmethod
    def _history_path() -> str:
        """Return path to the command history file (XDG config dir)."""
        config_dir = os.path.expanduser("~/.config/ruida-tui")
        return os.path.join(config_dir, "command_history.json")

    def _load_command_history(self) -> None:
        """Load command history from disk. Silently handles missing/corrupt files."""
        path = self._history_path()
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, list) and all(isinstance(item, str) for item in data):
                self._command_history = data[-500:]
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            pass  # Start with empty history

    def _save_command_history(self) -> None:
        """Save command history to disk. Silently handles write failures."""
        path = self._history_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._command_history[-500:], f)
        except (OSError, PermissionError):
            pass  # Non-fatal if we can't save history

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_mem() -> dict[str, int]:
        """Read memory stats from /proc/self/status.

        Returns dict with keys: VmRSS, VmSize, VmPeak, Threads.
        Returns empty dict on any error (fail silent, monitor simply won't update).
        """
        try:
            with open("/proc/self/status") as f:
                data = f.read()
        except OSError:
            return {}
        result: dict[str, int] = {}
        fields = {"VmRSS", "VmSize", "VmPeak", "Threads"}
        for line in data.splitlines():
            for field in fields:
                if line.startswith(field + ":"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            result[field] = int(parts[1])
                        except ValueError:
                            pass
        return result

    @staticmethod
    def _render_mem_display(
        cur: dict[str, int],
        prev: dict[str, int] | None,
        initial: dict[str, int],
    ) -> str:
        """Format memory stats as a 4-line tabular display.

        Line 1: column headers (VmRSS KB, VmSize KB, VmPeak KB, Threads)
        Line 2: Mem:   current values
        Line 3: Change: delta since previous update (yellow if non-zero)
        Line 4: Total:  total change since start
        """
        fields = ["VmRSS", "VmSize", "VmPeak", "Threads"]
        col_w = [9, 9, 9, 7]  # right-aligned column widths
        sep = "  "  # inter-column gap (2 spaces)
        pad = " " * 8  # 8-char label column (left-aligned Mem:/Change:/Total:)

        # Helper: right-pad a value string in its column
        def _col(s: str, i: int) -> str:
            return f"{s:>{col_w[i]}}"

        # Helper: format delta value with sign, optionally yellow
        def _fmt_delta(d: int, i: int) -> str:
            if d > 0:
                text = f"+{d}"
            elif d < 0:
                text = str(d)
            else:
                text = "0"
            padded = _col(text, i)
            if d != 0:
                padded = f"[yellow]{padded}[/yellow]"
            return padded

        # Line 1: header
        headers = ["VmRSS KB", "VmSize KB", "VmPeak KB", "Threads"]
        hdr_line = pad + sep + sep.join(_col(h, i) for i, h in enumerate(headers))

        # Line 2: Mem (current values)
        cur_vals = [cur.get(f, 0) for f in fields]
        mem_line = f"{'Mem:':<8}" + sep + sep.join(
            _col(str(v), i) for i, v in enumerate(cur_vals)
        )

        # Line 3: Change (delta since previous update)
        if prev is None:
            chg_cells = [_col("-", i) for i in range(4)]
        else:
            chg_cells = []
            for i, f in enumerate(fields):
                d = cur.get(f, 0) - prev.get(f, 0)
                chg_cells.append(_fmt_delta(d, i))
        chg_line = f"{'Change:':<8}" + sep + sep.join(chg_cells)

        # Line 4: Total (delta since start)
        if not initial:
            tot_cells = [_col("-", i) for i in range(4)]
        else:
            tot_cells = []
            for i, f in enumerate(fields):
                d = cur.get(f, 0) - initial.get(f, 0)
                if d > 0:
                    text = f"+{d}"
                elif d < 0:
                    text = str(d)
                else:
                    text = "0"
                tot_cells.append(_col(text, i))
        tot_line = f"{'Total:':<8}" + sep + sep.join(tot_cells)

        return f"{hdr_line}\n{mem_line}\n{chg_line}\n{tot_line}"

    @staticmethod
    def _count_gc_objects() -> dict[str, tuple[int, int, int]]:
        """Count GC-tracked Ruida PA class instances and their total memory.

        Calls gc.collect(), then iterates gc.get_objects(), filtering to
        classes whose __module__ starts with a Ruida PA package prefix.

        Returns:
            dict mapping class name -> (instance_count, total_bytes, max_depth),
            sorted by total_bytes descending, truncated to top 20.
            Empty dict if gc.collect() itself fails (fail-silent).
        """
        try:
            gc.collect()
        except (AttributeError, TypeError, OSError):
            return {}

        counter: dict[str, tuple[int, int, int]] = {}
        for obj in gc.get_objects():
            try:
                mod = type(obj).__module__
                if not (
                    mod == "rpa"
                    or mod.startswith(("rpalib.", "protocols.", "rpascript.", "ruidadriver."))
                ):
                    continue
                cls_name = type(obj).__name__
                count, mem, depth = counter.get(cls_name, (0, 0, 0))
                obj_mem, obj_depth = _deep_getsizeof(obj)
                counter[cls_name] = (count + 1, mem + obj_mem, max(depth, obj_depth))
            except (AttributeError, TypeError, OSError):
                continue  # Skip objects that cause errors during inspection

        # Sort by total_bytes descending, take top 20
        try:
            sorted_items = sorted(
                counter.items(), key=lambda kv: kv[1][1], reverse=True
            )[:20]
            return dict(sorted_items)
        except (AttributeError, TypeError, OSError):
            return {}

    @staticmethod
    def _render_gc_display(
        cur: dict[str, tuple[int, int, int]],
        prev: dict[str, tuple[int, int, int]] | None,
        initial: dict[str, tuple[int, int, int]],
    ) -> str:
        """Format GC object counts as a 21-line table (header + 20 data rows).

        Columns: Class(15L)  Count(d:10R)  Mem(10R)  Change(10R)  Total(10R)

        Args:
            cur: Current snapshot — {class_name: (count, mem_bytes, max_depth)}
            prev: Previous snapshot for Change delta, or None for first update.
            initial: First snapshot for Total delta, or empty for first update.

        Returns:
            Formatted string with Textual markup for non-zero deltas.
        """
        col_w = [15, 10, 10, 10, 10]
        sep = "  "

        # Header row: Class left-aligned, others right-aligned
        headers = ["Class", "Count", "Mem", "Change", "Total"]
        cells: list[str] = []
        for i, h in enumerate(headers):
            if i == 0:
                cells.append(f"{h:<{col_w[i]}}")
            else:
                cells.append(f"{h:>{col_w[i]}}")
        hdr_line = sep.join(cells)

        lines: list[str] = []
        for cls_name in cur:
            cnt, mem, depth = cur[cls_name]
            # Change delta
            if prev is None:
                chg = "-"
                chg_str = f"{chg:>{col_w[3]}}"
            else:
                _, p_mem, _ = prev.get(cls_name, (0, 0, 0))
                d = mem - p_mem
                if d > 0:
                    chg_str = f"[yellow]{d:>+{col_w[3]}}[/yellow]"
                elif d < 0:
                    chg_str = f"[yellow]{d:>{col_w[3]}}[/yellow]"
                else:
                    chg_str = f"{'0':>{col_w[3]}}"
            # Total delta
            if not initial:
                tot = "-"
                tot_str = f"{tot:>{col_w[4]}}"
            else:
                i_cnt, i_mem, _ = initial.get(cls_name, (0, 0, 0))
                td = mem - i_mem
                if td > 0:
                    tot_str = f"{td:>+{col_w[4]}}"
                elif td < 0:
                    tot_str = f"{td:>{col_w[4]}}"
                else:
                    tot_str = f"{'0':>{col_w[4]}}"
            # Mem column
            mem_str = f"{mem:>{col_w[2]}}"
            # Count column -- show count:max_depth
            cnt_str = f"{cnt}:{depth}"
            cnt_str = f"{cnt_str:>{col_w[1]}}"

            if depth >= 500:  # Orange highlight when walk hit the recursion limit
                cls_str = f"[orange]{cls_name:<{col_w[0]}}[/orange]"
            else:
                cls_str = f"{cls_name:<{col_w[0]}}"
            lines.append(
                sep.join([cls_str, cnt_str, mem_str, chg_str, tot_str])
            )

        return hdr_line + "\n" + "\n".join(lines)

    async def _update_mem_monitor(self) -> None:
        """Timer callback: read memory, count GC objects, render display, update cache."""
        cur = self._read_mem()
        if not cur:
            return

        # --- Memory stats (inline, fast /proc read) ---
        if not self._mem_initial:
            self._mem_initial = dict(cur)
            self._mem_prev = dict(cur)
            rendered_mem = self._render_mem_display(cur, None, {})
        else:
            rendered_mem = self._render_mem_display(cur, self._mem_prev, self._mem_initial)
            self._mem_prev = dict(cur)

        # --- GC object stats (offloaded to executor to avoid blocking event loop) ---
        loop = asyncio.get_running_loop()
        gc_cur = await loop.run_in_executor(None, self._count_gc_objects)

        if gc_cur:
            if not self._gc_initial:
                self._gc_initial = dict(gc_cur)
                self._gc_prev = dict(gc_cur)
                rendered_gc = self._render_gc_display(gc_cur, None, {})
            else:
                rendered_gc = self._render_gc_display(
                    gc_cur, self._gc_prev, self._gc_initial
                )
                self._gc_prev = dict(gc_cur)

            self._reply_log.update(rendered_mem + "\n\n" + rendered_gc)
        else:
            self._reply_log.update(rendered_mem)

    def _is_resolvable_address(self, token: str) -> bool:
        """Check if a GET_SETTING address token can be resolved (MT mnemonic or numeric)."""
        return is_resolvable_address(token, self._parser._mt_map)


# ------------------------------------------------------------------
# Module-level entry point
# ------------------------------------------------------------------


def _resolve_hostname(host: str, port: int = 50200) -> str | None:
    """Resolve a hostname to an IP address. Returns IP string or None on failure.

    Performs DNS resolution via socket.getaddrinfo in a thread pool with
    a 5-second timeout. For valid IP addresses, returns the host unchanged.
    """
    import concurrent.futures
    import ipaddress
    import socket

    if not host:
        return ""  # Empty is OK (USB mode)

    # Already an IP? No DNS needed.
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    # Has spaces? Can't be a valid hostname.
    if " " in host:
        return None

    # Resolve hostname via DNS with timeout
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(socket.getaddrinfo, host, port)
        try:
            result = future.result(timeout=5.0)
            # Extract IP from getaddrinfo result
            # Result format: [(family, type, proto, canonname, sockaddr), ...]
            ip = result[0][4][0]
            return ip
        except concurrent.futures.TimeoutError:
            return None
        except socket.gaierror:
            return None


def run_tui() -> None:
    """Run the TuiAdapter TUI application.

    Creates an TuiAdapter instance and enters the Textual event loop.
    Blocks until the user quits (Ctrl+C).
    """
    app = TuiAdapter()
    app.run()
