"""Top-level Bokeh application: server launcher, tab container, and thread-safe updates.

Manages BokehView instances, launches Bokeh server with port auto-selection,
and handles thread-safe data updates via queue.Queue + add_periodic_callback."""

import logging
import queue
import threading

# Fail-fast import check for Bokeh and Tornado dependencies.
try:
    from bokeh.application import Application
    from bokeh.application.handlers import FunctionHandler
    from bokeh.layouts import column
    from bokeh.models import ColumnDataSource, Div, Tabs
    from bokeh.server.server import Server
except ImportError:
    raise ImportError(
        "Bokeh and Tornado are required for plotting. Install with: pip install bokeh"
    )

from rpalib.bokeh_view import BokehView

log = logging.getLogger(__name__)

# Sentinel value to signal queue shutdown.
_SENTINEL = object()


class BokehApp:
    """Top-level Bokeh application managing views, tab container, and server.

    Combines server lifecycle (port selection, background IO loop), a tabbed
    UI container, and a thread-safe update pipeline using queue.Queue and
    a periodic callback on the Bokeh server thread.

    The start/shutdown flow:
        start() -> background IO loop thread -> browser connects
        shutdown() -> sentinel -> drain_queue stops server
    """

    DEFAULT_PORT = 5006
    MAX_PORT_ATTEMPTS = 5

    def __init__(self, args, plotter):
        """Initialise the Bokeh application.

        Parameters:
            args        Command line arguments for the rpa program.
            plotter  A BokehPlotter instance providing to_column_data().
        """
        self.args = args
        self.plotter = plotter

        # Thread-safe queue: decoder thread pushes data, server thread drains.
        self.data_queue: queue.Queue = queue.Queue()

        # Server and view state.
        self.server = None
        self.views: list[BokehView] = []
        self._running = False
        self._thread = None

        # Port tracking for auto-selection.
        self.port = self.DEFAULT_PORT

        # Snapshot of initial data at construction time.
        self._initial_data = self.plotter.to_column_data()

        # Populated by _make_document when a session connects.
        self._first_view = None
        self._doc = None

    def _make_document(self, doc):
        """Create the Bokeh document served by the Bokeh server.

        Called by FunctionHandler when a new session starts.  Sets up the tab
        container, the first view, and the periodic callback that drains the
        data queue onto the ColumnDataSource.

        Parameters:
            doc  The Bokeh Document to populate.
        """
        # ColumnDataSource populated with data decoded before server start.
        _source = ColumnDataSource(data=self._initial_data)

        # Resolve output stem for save filenames
        _out_stem = None
        if self.plotter.out.args.output_file:
            _out_stem = str(self.plotter.out.out_stem)
        # Create the primary tab holding the XY plot and histograms.
        _view = BokehView(
            self.args,
            source=_source,
            title="All Vectors",
            color_lut=self.plotter.color_lut,
            out_stem=_out_stem,
        )
        _view.set_app(self)
        _view.update_histograms()
        self.views.append(_view)
        self._first_view = _view

        # Wrapping tab container (supports future multi-view expansion).
        self.tabs = Tabs(tabs=[_view.tab])

        # Status banner shown above the plot area.
        _status = Div(
            text="Now plotting moves. Press Ctrl+C in the terminal to exit.",
            styles={"font-size": "14px", "margin": "10px 0"},
        )

        # Set the browser window/tab title.
        doc.title = "Ruida Protocol Analyzer"

        # Assemble the root layout.
        doc.add_root(column(_status, self.tabs, sizing_mode="stretch_width"))

        # Periodic callback to drain the data queue (every 100 ms).
        doc.add_periodic_callback(self._drain_queue, 100)

        # Store doc reference for potential future use.
        self._doc = doc

    def _drain_queue(self):
        """Drain the data queue and update the ColumnDataSource.

        Called periodically via add_periodic_callback on the Bokeh server
        thread.  This is the thread-safe mechanism for pushing data from
        the decoder thread to the Bokeh UI.
        """
        # Collect all pending items from the queue.
        _updates = []
        try:
            while True:
                _item = self.data_queue.get_nowait()
                if _item is _SENTINEL:
                    # Shutdown sentinel received -- stop the server.
                    self._running = False
                    if self.server is not None:
                        self.server.stop()
                    return
                _updates.append(_item)
        except queue.Empty:
            pass

        # Nothing to update or no view yet.
        if not _updates or self._first_view is None:
            return

        # Append new rows to the existing ColumnDataSource.
        _source = self._first_view.source
        _current = dict(_source.data)
        for _key in _current:
            for _update in _updates:
                _val = _update.get(_key)
                if _val is not None:
                    _current[_key].append(_val)

        # Replace source data to trigger automatic plot re-render.
        _source.data = _current

        # Recompute histograms based on the updated data.
        self._first_view.update_histograms(_source)

        # Update command search completions on the first view
        if hasattr(self._first_view, "_update_cmd_completions"):
            self._first_view._update_cmd_completions()

    # ---- Tab Management (Phase 5a.3) ----

    def add_view(self, source_data: dict, title: str):
        """Create a new BokehView and append its tab to the container.

        Each new view receives its own ColumnDataSource so that filtering
        and range-scrolling in one tab does not affect others.

        Parameters:
            source_data  A ColumnDataSource-compatible dict with keys
                         cmd_id, command, index, start_x, start_y,
                         end_x, end_y, command_start_x, command_start_y,
                         command_end_x, command_end_y, length, speed,
                         power, width, style, color, annotation.
            title        The tab title string.

        Returns:
            The newly created BokehView.
        """
        # Guard clause: ensure document is initialised.
        if not hasattr(self, "tabs"):
            return None

        # Resolve output stem for save filenames
        _out_stem = None
        if self.plotter.out.args.output_file:
            _out_stem = str(self.plotter.out.out_stem)
        _source = ColumnDataSource(data=source_data)
        _view = BokehView(
            self.args,
            source=_source,
            title=title,
            color_lut=self.plotter.color_lut,
            out_stem=_out_stem,
        )
        _view.set_app(self)
        _view.update_histograms()
        self.views.append(_view)
        self.tabs.tabs = list(self.tabs.tabs) + [_view.tab]
        return _view

    def remove_view(self, tab_index: int):
        """Remove the tab and view at the given index.

        The first tab (index 0, "All Vectors") is protected and cannot
        be removed.

        Parameters:
            tab_index  Index of the tab to remove.
        """
        # Guard clause: protect the first (All Vectors) tab.
        if tab_index <= 0 or tab_index >= len(self.tabs.tabs):
            return

        if tab_index < len(self.views):
            self.views.pop(tab_index)
        _tabs = list(self.tabs.tabs)
        _tabs.pop(tab_index)
        self.tabs.tabs = _tabs

    def add_tab_from_cmd_id(self, cmd_id: int, source_view) -> BokehView:
        """Create a new view starting from the vector with the given cmd_id.

        Filters source_view's data to include only vectors from the
        matching cmd_id onward.

        Parameters:
            cmd_id       The command ID to start from.
            source_view  The source BokehView to copy data from.

        Returns:
            The newly created BokehView.
        """
        _data = source_view.source.data

        # Guard clause: no data to filter.
        _cmd_ids = _data.get("cmd_id", [])
        if not _cmd_ids:
            return self.duplicate_view(source_view)

        # Find the index of the first occurrence of cmd_id.
        _start_idx = None
        for _i, _cid in enumerate(_cmd_ids):
            if _cid == cmd_id:
                _start_idx = _i
                break

        # Guard clause: cmd_id not found — duplicate the full view.
        if _start_idx is None:
            return self.duplicate_view(source_view)

        # Build a subset from _start_idx onward.
        _subset = {}
        for _key in _data:
            _subset[_key] = _data[_key][_start_idx:]

        _title = f"View from Cmd #{cmd_id}"
        return self.add_view(_subset, _title)

    def duplicate_view(self, source_view) -> BokehView:
        """Create a new view with an independent copy of source_view's data.

        Parameters:
            source_view  The BokehView to duplicate.

        Returns:
            The newly created BokehView.
        """
        _data = {}
        for _key in source_view.source.data:
            # Copy the list to avoid sharing references.
            _data[_key] = list(source_view.source.data[_key])

        _title = f"Copy of {source_view.title}"
        return self.add_view(_data, _title)

    def push_data(self, data: dict):
        """Push vector data to the Bokeh server in a thread-safe manner.

        Parameters:
            data  A dict with keys matching ColumnDataSource columns
                  (cmd_id, command, start_x, start_y, end_x, end_y,
                   command_start_x, command_start_y, command_end_x,
                   command_end_y, length, speed, power, width, style,
                   color, annotation).
        """
        # Guard clause: silently drop data if the server is not running.
        if not self._running:
            return
        self.data_queue.put(data)

    def shutdown(self):
        """Signal the Bokeh server to shut down gracefully and wait for it."""
        # Best-effort: put sentinel for the periodic callback (polite data cleanup)
        self.data_queue.put(_SENTINEL)

        # Directly stop the server and IO loop to release the port immediately.
        # This is more reliable than relying solely on the sentinel, which only
        # gets processed by a periodic callback in the Document (requires a
        # connected browser session).
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                log.warning("Bokeh server thread did not exit within 5 seconds")

        self.server = None
        self._thread = None
        self._running = False

    def start(self, port: int = None) -> bool:
        """Start the Bokeh server in a background thread.

        Attempts to bind to *port* (default 5006).  If the port is already in
        use, the method tries the next port, and so on up to
        MAX_PORT_ATTEMPTS times.

        Parameters:
            port  Port number to listen on.  Defaults to 5006.

        Returns:
            True if the server started successfully, False otherwise.
        """
        if port is not None:
            self.port = port

        _app = Application(FunctionHandler(self._make_document))

        for _attempt in range(self.MAX_PORT_ATTEMPTS):
            try:
                self.server = Server(
                    {"/": _app},
                    port=self.port,
                    allow_websocket_origin=["*"],
                    session_token_expiration=86400,  # 24 hours
                )
                self.server.start()
                self._running = True

                # Run the Tornado IO loop in a daemon thread so it does not
                # prevent the process from exiting.
                self._thread = threading.Thread(
                    target=self.server.io_loop.start,
                    daemon=True,
                )
                self._thread.start()

                log.info("Bokeh server started on port %d", self.port)
                return True

            except OSError as e:
                if "Address already in use" in str(e):
                    self.port += 1
                    continue
                log.error("Failed to start Bokeh server: %s", e)
                return False

            except Exception as e:
                log.error("Failed to start Bokeh server: %s", e)
                return False

        log.error(
            "Could not find an available port after %d attempts. Last tried: %d",
            self.MAX_PORT_ATTEMPTS,
            self.port,
        )
        return False
