"""Bokeh view/tab visualization for laser head movements.

A single tab containing XY plot, power histogram, and speed histogram
with interactive tools (zoom, pan, box select, hover), menu bar,
and right-click context menu."""

import json
import time
from pathlib import Path

# Fail-fast import check
try:
    from bokeh.embed import file_html
    from bokeh.layouts import column, row
    from bokeh.models import (
        BoxZoomTool,
        Button,
        CheckboxButtonGroup,
        ColumnDataSource,
        CustomJS,
        Div,
        Dropdown,  # noqa: F401
        CustomJSTickFormatter,
        HoverTool,
        PanTool,
        Paragraph,
        RangeSlider,
        SaveTool,
        Spinner,
        TabPanel,
        TapTool,
        TextInput,
        WheelZoomTool,
    )
    from bokeh.plotting import figure
    from bokeh.resources import CDN

except ImportError:
    raise ImportError("Bokeh is required for plotting. Install with: pip install bokeh")


class BokehView:
    """A single tab view with XY plot, power histogram, and speed histogram.

    Each BokehView represents one tab containing:
    - XY scatter/line plot (top, large) with interactive tools
    - Power histogram (bottom-left)
    - Speed histogram (bottom-right)
    """

    def __init__(
        self,
        args,
        source: ColumnDataSource,
        title: str = "All Vectors",
        color_lut: list = None,
        out_stem: str = None,
    ):
        """Create a new view tab.

        Parameters:
            args      Command line arguments for the rpa program.
            source    Shared ColumnDataSource with vector data.
            title     Tab title string.
            color_lut Color lookup table (list of 101 hex strings) for
                      power histogram bar colors.  Falls back to 'navy'
                      when not provided.
            out_stem  Output file stem for save filenames (e.g. the decoded
                      text file base name).  Falls back to old naming when
                      None.
        """
        self.args = args
        self.title = title
        self.source = source

        # Ensure alpha column exists for filter opacity control
        if "alpha" not in self.source.data:
            self.source.data["alpha"] = [1.0] * len(self.source.data.get("cmd_id", []))

        # Backup for reset view
        self._initial_source_data = dict(source.data)

        # Full unfiltered data for range slider filtering.
        self._full_data = {k: list(v) for k, v in source.data.items()}
        # Full unfiltered data as a CDS for CustomJS range slicing in standalone HTML.
        self._full_source = ColumnDataSource(data=self._full_data)

        # Color LUT for power histogram (shared with vector coloring).
        self._color_lut = color_lut

        # Output file stem for save filenames (uses decoded text file base name).
        self._out_stem = str(out_stem) if out_stem is not None else None

        # Grid line alpha for plot and histograms (consistent styling).
        self._grid_line_alpha = 0.8

        # Z-axis placeholder: Future 3D capability will add z coordinate
        # support for multi-layer engraving visualization.

        # ---- XY Plot ----
        # Drag tools for the plot toolbar
        self._box_zoom = BoxZoomTool(match_aspect=True)
        self._pan = PanTool()
        self._wheel_zoom = WheelZoomTool()

        self.xy_plot = figure(
            title=f"{self.args.input_file}: {self.title}\nLaser Head Movement",
            width=800,
            height=800,
            tools=[self._box_zoom, self._pan, self._wheel_zoom, "reset"],
            active_drag=self._box_zoom,
            active_scroll=self._wheel_zoom,
            output_backend="canvas",
        )

        # 1:1 aspect ratio to prevent distortion of CNC toolpaths
        self.xy_plot.match_aspect = True
        self.xy_plot.aspect_scale = 1

        # Axis labels
        self.xy_plot.xaxis.axis_label = "Bed X (mm)"
        self.xy_plot.yaxis.axis_label = "Bed Y (mm)"

        # Axis ticks: data is negated for geometry, so negate the tick
        # values to display the original positive command coordinates.
        self.xy_plot.xaxis.formatter = CustomJSTickFormatter(code="return -tick")
        self.xy_plot.yaxis.formatter = CustomJSTickFormatter(code="return -tick")

        # Grid
        self.xy_plot.grid.grid_line_alpha = self._grid_line_alpha

        # Render vector segments using the segment glyph
        self.xy_renderer = self.xy_plot.segment(
            x0="start_x",
            y0="start_y",
            x1="end_x",
            y1="end_y",
            source=source,
            line_color="color",
            line_width="width",
            line_dash="style",
            line_alpha="alpha",
        )

        # Highlight overlay: empty initially, populated on command search/context menu
        self._highlight_source = ColumnDataSource(
            data={
                "x0": [],
                "y0": [],
                "x1": [],
                "y1": [],
                "color": [],
                "width": [],
            }
        )
        self.xy_plot.segment(
            x0="x0",
            y0="y0",
            x1="x1",
            y1="y1",
            source=self._highlight_source,
            line_color="color",
            line_width="width",
            line_alpha=1.0,
            line_dash="solid",
        )

        # HoverTool formatted to match RpaLine.annotation format.
        # The multi-line string format renders a single HTML tooltip div
        # (as opposed to the list-of-tuples format which renders an HTML table).
        hover = HoverTool(
            renderers=[self.xy_renderer],
            tooltips="""
@{cmd_id}:@{command}

start=(@{command_start_x}mm, @{command_start_y}mm)
end=(@{command_end_x}mm, @{command_end_y}mm)
Length=@{length}mm
Power=@{power}{f.1}%
Speed=@{speed}{f.03}mm/S
""",
            point_policy="snap_to_data",
            mode="mouse",
        )
        self.xy_plot.add_tools(hover)

        # SaveTool: native Bokeh save icon in the plot toolbar.
        _f = Path(self.args.input_file).with_suffix("")
        self._save_tool = SaveTool(filename=str(_f))
        self.xy_plot.add_tools(self._save_tool)

        # Tool switching is handled by the Bokeh toolbar buttons.
        # BoxZoomTool is the default active_drag (set during figure creation).
        # PanTool and WheelZoomTool are also available via the toolbar.

        # Backup initial ranges for reset
        self._initial_x_range = (self.xy_plot.x_range.start, self.xy_plot.x_range.end)
        self._initial_y_range = (self.xy_plot.y_range.start, self.xy_plot.y_range.end)

        # Guard to prevent re-entrant range-slider updates.
        self._updating_range = False

        # Debounce: minimum interval (in seconds) between range change
        # processing.  Prevents event queue buildup during auto-repeat
        # (holding the spinner increment/decrement button).
        self._last_range_update = 0.0

        # ---- Power Histogram ----
        self.power_hist = figure(
            title="Power Distribution",
            width=400,
            height=250,
            tools="",
            output_backend="svg",
        )
        self.power_hist.xaxis.axis_label = "Power %"
        self.power_hist.yaxis.axis_label = "Frequency"
        self.power_hist.grid.grid_line_alpha = self._grid_line_alpha

        # ---- Speed Histogram ----
        self.speed_hist = figure(
            title="Speed Distribution",
            width=400,
            height=250,
            tools="",
            output_backend="svg",
        )
        self.speed_hist.xaxis.axis_label = "Speed (mm/S)"
        self.speed_hist.yaxis.axis_label = "Frequency"
        self.speed_hist.grid.grid_line_alpha = self._grid_line_alpha

        # ---- Persistent Histogram Sources (Phase 5e) ----
        # Persistent ColumnDataSources prevent UnknownReferenceError by avoiding
        # destruction/recreation of renderer models.  Data is updated in-place.
        self._power_hist_source = ColumnDataSource(
            data={
                "top": [],
                "center": [],
                "width": [],
                "color": [],
            }
        )
        self.power_hist.vbar(
            x="center",
            top="top",
            width="width",
            source=self._power_hist_source,
            fill_color="color",
            alpha=0.7,
        )

        self._speed_hist_source = ColumnDataSource(
            data={
                "top": [],
                "center": [],
                "width": [],
            }
        )
        self.speed_hist.vbar(
            x="center",
            top="top",
            width="width",
            source=self._speed_hist_source,
            fill_color="green",
            alpha=0.7,
        )

        # ---- Menu Bar ----
        # View presets and toggles (Settings menu saved for app integration).

        # Vector range slider (start index + count).
        _total = len(self._full_data.get("cmd_id", []))
        self._start_spinner = Spinner(
            title="Start:",
            low=0,
            high=max(0, _total - 1),
            value=0,
            step=1,
            width=80,
        )
        if self.args.plot_moves:
            self._start_spinner.on_change(
                "value", lambda attr, old, new: self._on_range_change("start", old, new)
            )

        self._count_spinner = Spinner(
            title="Count:",
            low=1,
            high=max(1, _total),
            value=max(1, _total),
            step=1,
            width=80,
        )
        if self.args.plot_moves:
            self._count_spinner.on_change(
                "value", lambda attr, old, new: self._on_range_change("count", old, new)
            )

        # ---- Phase 5c.2: Advanced Filter Controls ----
        # Vector type filter: Move vs Cut
        self._type_filter = CheckboxButtonGroup(
            labels=["Moves", "Cuts"],
            active=[0, 1],  # Both selected by default
            width=120,
        )
        if self.args.plot_moves:
            self._type_filter.on_change("active", self._on_filter_change)

        # Power range slider
        self._power_filter = RangeSlider(
            title="Power %",
            start=0,
            end=100,
            value=(0, 100),
            step=1,
            width=200,
            show_value=True,
        )
        if self.args.plot_moves:
            self._power_filter.on_change("value", self._on_filter_change)

        # Speed range slider
        _speeds = self.source.data.get("speed", [])
        _speed_min = min(_speeds) if _speeds else 0
        _speed_max = max(_speeds) if _speeds else 100
        self._speed_filter = RangeSlider(
            title="Speed mm/S",
            start=_speed_min,
            end=max(_speed_max, _speed_min + 1),
            value=(_speed_min, max(_speed_max, _speed_min + 1)),
            step=1,
            width=200,
            show_value=True,
        )
        if self.args.plot_moves:
            self._speed_filter.on_change("value", self._on_filter_change)

        # ---- CustomJS callbacks for standalone HTML export ----
        # These replicate the Python filter logic in JavaScript so
        # that the Moves/Cuts, Power, and Speed filters work in
        # exported standalone HTML (no Bokeh server).
        _filter_cb = CustomJS(
            args=dict(
                source=self.source,
                type_filter=self._type_filter,
                power_filter=self._power_filter,
                speed_filter=self._speed_filter,
            ),
            code="""
            const data = source.data;
            const total = data.cmd_id.length;
            if (total === 0) return;

            const active_types = type_filter.active;
            const power_range = power_filter.value;
            const speed_range = speed_filter.value;

            const new_alpha = new Array(total);
            for (let i = 0; i < total; i++) {
                let visible = true;

                // Type filter
                const style = data.style[i] || 'solid';
                const is_move = (style === 'dashed');
                const is_cut = (style === 'solid');
                if (active_types.indexOf(0) === -1 && is_move) visible = false;
                if (active_types.indexOf(1) === -1 && is_cut) visible = false;

                // Power filter
                const power = data.power[i];
                if (power < power_range[0] || power > power_range[1]) visible = false;

                // Speed filter
                const speed = data.speed[i];
                if (speed < speed_range[0] || speed > speed_range[1]) visible = false;

                new_alpha[i] = visible ? 1.0 : 0.2;
            }
            data.alpha = new_alpha;
            source.change.emit();
        """,
        )

        self._type_filter.js_on_change("active", _filter_cb)
        self._power_filter.js_on_change("value", _filter_cb)
        self._speed_filter.js_on_change("value", _filter_cb)

        # ---- CustomJS callbacks for Start/Count range spinners ----
        # Replicates the Python _on_range_change logic in JavaScript so
        # that the Start and Count spinners work in exported standalone HTML.
        _range_cb = CustomJS(
            args=dict(
                full_source=self._full_source,
                source=self.source,
                start_spinner=self._start_spinner,
                count_spinner=self._count_spinner,
                type_filter=self._type_filter,
                power_filter=self._power_filter,
                speed_filter=self._speed_filter,
            ),
            code="""
            // One-shot: attach keyup → blur on Enter for spinner inputs.
            // Bokeh Spinner's <input type="number"> only fires the `change`
            // event on blur (clicking outside), not on Enter keypress.
            // Blurring on Enter triggers the native `change` event,
            // which syncs the model `value` and fires both
            // js_on_change('value', ...) (CustomJS) and
            // on_change('value', Python callback).
            if (!window._spinnerEnterSetup) {
                window._spinnerEnterSetup = true;
                setTimeout(function() {
                    const setupSpinner = (sp) => {
                        const view = Bokeh.index[sp.id];
                        if (view && view.el) {
                            const input = view.el.querySelector('input');
                            if (input) {
                                input.addEventListener('keyup', function(e) {
                                    if (e.key === 'Enter') this.blur();
                                });
                            }
                        }
                    };
                    setupSpinner(start_spinner);
                    setupSpinner(count_spinner);
                }, 200);
            }

            // Guard: prevent recursion from programmatic value sync.
            if (window._rangeUpdating) return;
            window._rangeUpdating = true;

            // Debounce: skip events faster than 50ms to prevent event
            // queue buildup during auto-repeat (holding spinner button).
            const _now = Date.now();
            if (window._lastRangeUpdate && (_now - window._lastRangeUpdate < 50)) {
                window._rangeUpdating = false;
                return;
            }
            window._lastRangeUpdate = _now;

            const total = full_source.data.cmd_id.length;
            if (total === 0) { window._rangeUpdating = false; return; }

            let start = parseInt(start_spinner.value);
            let count = parseInt(count_spinner.value);

            // Clamp to valid bounds (same as Python _on_range_change).
            start = Math.max(0, Math.min(start, total - 1));
            count = Math.max(1, Math.min(count, total - start));

            // Sync spinners to clamped values (only if value changed to
            // avoid triggering recursive js_on_change callbacks from auto-repeat).
            if (start_spinner.value !== start) {
                start_spinner.value = start;
            }
            if (count_spinner.value !== count) {
                count_spinner.value = count;
            }

            const end = start + count;
            const full = full_source.data;

            // Build sliced data dict from full source.
            const sliced = {};
            for (const key in full) {
                if (full.hasOwnProperty(key)) {
                    sliced[key] = full[key].slice(start, end);
                }
            }
            if (!('alpha' in sliced)) {
                sliced.alpha = new Array(sliced.cmd_id.length).fill(1.0);
            }

            // Re-apply type/power/speed filter logic on the sliced data.
            const active_types = type_filter.active;
            const power_range = power_filter.value;
            const speed_range = speed_filter.value;

            const new_alpha = sliced.alpha;
            for (let i = 0; i < sliced.cmd_id.length; i++) {
                let visible = true;

                // Type filter: dashed = move (index 0), solid = cut (index 1)
                const style = sliced.style[i] || 'solid';
                const is_move = (style === 'dashed');
                const is_cut = (style === 'solid');
                if (active_types.indexOf(0) === -1 && is_move) visible = false;
                if (active_types.indexOf(1) === -1 && is_cut) visible = false;

                // Power filter
                const power = sliced.power[i];
                if (power < power_range[0] || power > power_range[1]) visible = false;

                // Speed filter
                const speed = sliced.speed[i];
                if (speed < speed_range[0] || speed > speed_range[1]) visible = false;

                new_alpha[i] = visible ? 1.0 : 0.2;
            }

            source.data = sliced;
            source.change.emit();

            window._rangeUpdating = false;
        """,
        )

        self._start_spinner.js_on_change("value", _range_cb)
        self._count_spinner.js_on_change("value", _range_cb)

        # ---- Phase 5c.1: Command Search ----
        # TextInput search box: type `cmd_id:command_name` to highlight a vector.
        self._cmd_search = TextInput(
            value="",
            placeholder="Search command (cmd_id:name)...",
            width=250,
        )
        if self.args.plot_moves:
            self._cmd_search.on_change("value", self._on_cmd_search)

        # Command summary display area
        self._cmd_summary = Div(
            text="Hover or select a command to see details",
            width=300,
            height=100,
            styles={"font-size": "12px", "overflow-y": "auto"},
        )

        # CustomJS for command search in standalone HTML export.
        # Highlights the matching vector and updates the summary display.
        self._cmd_search.js_on_change(
            "value",
            CustomJS(
                args=dict(
                    source=self.source,
                    hl_source=self._highlight_source,
                    summary=self._cmd_summary,
                ),
                code="""
            const value = cb_obj.value;
            if (!value) {
                hl_source.data = {
                    x0: [], y0: [], x1: [], y1: [],
                    color: [], width: [],
                };
                hl_source.change.emit();
                summary.text = '';
                return;
            }

            const parts = value.split(':');
            const target = parseInt(parts[0], 10);
            if (isNaN(target)) return;

            const data = source.data;
            const cmd_ids = data.cmd_id || [];

            // Pass 1: Exact match — cmd_id === target
            for (let i = 0; i < cmd_ids.length; i++) {
                if (cmd_ids[i] === target) {
                    hl_source.data = {
                        x0: [data.start_x[i]],
                        y0: [data.start_y[i]],
                        x1: [data.end_x[i]],
                        y1: [data.end_y[i]],
                        color: ['#FF0000'],
                        width: [3],
                    };
                    hl_source.change.emit();

                    const sx = Number(data.command_start_x[i]).toFixed(3);
                    const sy = Number(data.command_start_y[i]).toFixed(3);
                    const ex = Number(data.command_end_x[i]).toFixed(3);
                    const ey = Number(data.command_end_y[i]).toFixed(3);
                    const len = Number(data.length[i]).toFixed(3);
                    const pwr = Number(data.power[i]).toFixed(1);
                    const spd = Number(data.speed[i]).toFixed(1);

                    summary.text = '<b>' + target + ':' + data.command[i] + '</b><br>' +
                        'start=(' + sx + 'mm, ' + sy + 'mm) \\u2192 ' +
                        'end=(' + ex + 'mm, ' + ey + 'mm)<br>' +
                        'Length: ' + len + 'mm | Power: ' + pwr + '% | ' +
                        'Speed: ' + spd + 'mm/S | Type: ' + data.style[i];
                    return;
                }
            }

            // Pass 2: Nearest match — prefer next higher, fall back to highest lower
            let higherIdx = null;
            let lowerIdx = null;
            for (let i = 0; i < cmd_ids.length; i++) {
                if (cmd_ids[i] > target && (higherIdx === null || cmd_ids[i] < cmd_ids[higherIdx])) {
                    higherIdx = i;
                }
                if (cmd_ids[i] < target && (lowerIdx === null || cmd_ids[i] > cmd_ids[lowerIdx])) {
                    lowerIdx = i;
                }
            }

            let matchIdx;
            if (higherIdx !== null) {
                matchIdx = higherIdx;
            } else if (lowerIdx !== null) {
                matchIdx = lowerIdx;
            } else {
                summary.text = 'Command not found in current view';
                hl_source.data = {
                    x0: [], y0: [], x1: [], y1: [],
                    color: [], width: [],
                };
                hl_source.change.emit();
                return;
            }

            // Show the matched vector
            const i = matchIdx;
            hl_source.data = {
                x0: [data.start_x[i]],
                y0: [data.start_y[i]],
                x1: [data.end_x[i]],
                y1: [data.end_y[i]],
                color: ['#FF0000'],
                width: [3],
            };
            hl_source.change.emit();

            const sx = Number(data.command_start_x[i]).toFixed(3);
            const sy = Number(data.command_start_y[i]).toFixed(3);
            const ex = Number(data.command_end_x[i]).toFixed(3);
            const ey = Number(data.command_end_y[i]).toFixed(3);
            const len = Number(data.length[i]).toFixed(3);
            const pwr = Number(data.power[i]).toFixed(1);
            const spd = Number(data.speed[i]).toFixed(1);

            summary.text = '<b>' + cmd_ids[i] + ':' + data.command[i] + '</b><br>' +
                'start=(' + sx + 'mm, ' + sy + 'mm) \\u2192 ' +
                'end=(' + ex + 'mm, ' + ey + 'mm)<br>' +
                'Length: ' + len + 'mm | Power: ' + pwr + '% | ' +
                'Speed: ' + spd + 'mm/S | Type: ' + data.style[i];
        """,
            ),
        )
        # ---- Tap-to-populate search bar ----
        # When the user taps/clicks on a vector segment in the plot,
        # the TapTool hit-tests the nearest segment and sets the
        # source's selected indices.  We read those indices and
        # populate the search bar with the selected vector's info.
        # CustomJS for standalone HTML export:
        _tap_cb = CustomJS(
            args=dict(
                source=self.source,
                search_input=self._cmd_search,
            ),
            code="""
            if (window._tapGuard) return;
            window._tapGuard = true;
            const indices = source.selected.indices;
            if (indices.length > 0) {
                const idx = indices[0];
                const cid = source.data.cmd_id[idx];
                const cmd = source.data.command[idx];
                if (cid !== undefined && cmd !== undefined) {
                    search_input.value = String(cid) + ':' + String(cmd);
                }
            }
            // Clear selection to avoid visual indicator.
            source.selected.indices = [];
            window._tapGuard = false;
        """,
        )
        self.source.selected.js_on_change("indices", _tap_cb)
        # Python callback for server mode:
        if self.args.plot_moves:
            self.source.selected.on_change("indices", self._on_tap_select)

        # Add TapTool to the plot (hit-tests against main vector segments).
        self.xy_plot.add_tools(TapTool(renderers=[self.xy_renderer]))

        # "Open in new tab" button for the search result
        self._cmd_open_tab_btn = Button(
            label="Open Tab", button_type="primary", width=80
        )
        if self.args.plot_moves:
            self._cmd_open_tab_btn.on_click(self._on_cmd_open_tab)

        # Standalone fallback: open window with search hash.
        self._cmd_open_tab_btn.js_on_click(
            CustomJS(args=dict(cmd_search=self._cmd_search), code="""
            const val = cmd_search.value;
            if (val) {
                window.open(window.location.href.split("#")[0] + "#cmd-" + encodeURIComponent(val));
            }
        """)
        )
        self._cmd_open_tab_btn.disabled = True

        # ---- Save HTML ----
        # Rate-limiting guard to prevent concurrent saves from rapid clicks
        self._saving = False

        # Status Div for save confirmation messages
        self._save_status = Div(
            text="",
            width=140,
            height=20,
            styles={"font-size": "12px", "color": "#666", "margin-left": "8px"},
        )

        # Save HTML button: exports current view as standalone interactive HTML
        self._save_html_btn = Button(
            label="Save HTML", button_type="success", width=100
        )
        if self.args.plot_moves:
            self._save_html_btn.on_click(self._on_save_html)

        # Standalone fallback: show help message.
        self._save_html_btn.js_on_click(
            CustomJS(args=dict(status=self._save_status), code="""
            status.text = "Standalone: use browser Save As (Ctrl+S)";
        """)
        )

        # Menu bar row.
        self._menu_bar = row(
            self._start_spinner,
            self._count_spinner,
            self._type_filter,
            self._power_filter,
            self._speed_filter,
            self._cmd_search,
            self._cmd_open_tab_btn,
            self._save_html_btn,
            self._save_status,
            sizing_mode="stretch_width",
        )

        # ---- Context Menu (Phase 5b.2 / 5b.3) ----
        # Hidden Paragraph widget acts as a communication bridge between
        # client-side CustomJS and Python callbacks.  CustomJS writes a JSON
        # string here; the on_change handler deserialises and dispatches the
        # requested action (new_tab or duplicate).
        self._ctx_store = Paragraph(
            text="",
            visible=False,
            width=0,
            height=0,
            margin=0,
        )
        if self.args.plot_moves:
            self._ctx_store.on_change("text", self._on_ctx_action)

        # Right-click context menu overlay via native DOM listener.
        #
        # Bokeh 3.x plot events do not expose the mouse button property, so
        # we combine two techniques:
        #   1) Bokeh's 'press' event stores data-space click coordinates.
        #   2) A native DOM 'contextmenu' listener on the canvas (right-click
        #      only) reads the stored coordinates and creates the overlay.
        # The native listener is attached once per plot (guarded by a
        # plot-scoped window flag).  The coordinate capture runs on every
        # press to keep the stored values current.
        _ctx_menu_js = CustomJS(
            args=dict(
                source=self.source,
                store=self._ctx_store,
                plot=self.xy_plot,
            ),
            code=r"""
            // ---- Right-click context menu (Phase 5b) ----
            // One-time setup: attach native contextmenu listener to the
            // plot canvas (fires only on right-click).
            var setupKey = '_rt_ctx_' + plot.id;
            if (!window[setupKey]) {
                window[setupKey] = true;

                var plotEl = document.getElementById(plot.id);
                if (plotEl) {
                    function findInShadow(root, sel) {
                        var found = root.querySelector(sel);
                        if (found) return found;
                        if (root.shadowRoot) return findInShadow(root.shadowRoot, sel);
                        return null;
                    }
                    var canvas = findInShadow(plotEl, '.bk-canvas');
                    if (canvas) {
                        canvas.addEventListener('contextmenu', function (e) {
                            e.preventDefault();

                            // Guard: suppress context menu in standalone (no-server) mode
                            if (typeof Bokeh !== 'undefined' && !Bokeh.session) return;

                            // Read coordinates stored by the press handler below.
                            var cx = window._rt_ctx_cx;
                            var cy = window._rt_ctx_cy;
                            var sx = window._rt_ctx_sx;
                            var sy = window._rt_ctx_sy;
                            if (cx === undefined) return;

                            var data = source.data;

                            // Find nearest vector by midpoint distance.
                            var nearest_idx = -1;
                            var nearest_dist = Infinity;
                            for (var i = 0; i < data.cmd_id.length; i++) {
                                var mx = (data.start_x[i] + data.end_x[i]) / 2;
                                var my = (data.start_y[i] + data.end_y[i]) / 2;
                                var dx = mx - cx;
                                var dy = my - cy;
                                var dist = Math.sqrt(dx * dx + dy * dy);
                                if (dist < nearest_dist) {
                                    nearest_dist = dist;
                                    nearest_idx = i;
                                }
                            }

                            var cmd_id = nearest_idx >= 0 ? data.cmd_id[nearest_idx] : -1;

                            // Remove any existing context menu overlay.
                            var oldMenu = document.getElementById('bokeh-ctx-menu');
                            if (oldMenu) oldMenu.remove();

                            // Create the context menu overlay <div>.
                            var menu = document.createElement('div');
                            menu.id = 'bokeh-ctx-menu';
                            menu.style.cssText = 'display:block; position:fixed; background:white; '
                                + 'border:1px solid #ccc; border-radius:4px; '
                                + 'box-shadow:2px 2px 6px rgba(0,0,0,0.3); '
                                + 'z-index:1000; font-size:13px; font-family:sans-serif;';

                            // --- "Open new tab" menu item ---
                            var item1 = document.createElement('div');
                            item1.textContent = 'Open new tab with this vector as start';
                            item1.style.cssText = 'padding:6px 16px; cursor:pointer;';
                            item1.onmouseover = function () { this.style.background = '#e8e8e8'; };
                            item1.onmouseout  = function () { this.style.background = ''; };
                            item1.onclick = function () {
                                store.text = JSON.stringify({action:'new_tab', cmd_id:cmd_id});
                                menu.remove();
                            };
                            menu.appendChild(item1);

                            // --- "Duplicate view" menu item ---
                            var item2 = document.createElement('div');
                            item2.textContent = 'Duplicate current view in new tab';
                            item2.style.cssText = 'padding:6px 16px; cursor:pointer;';
                            item2.onmouseover = function () { this.style.background = '#e8e8e8'; };
                            item2.onmouseout  = function () { this.style.background = ''; };
                            item2.onclick = function () {
                                store.text = JSON.stringify({action:'duplicate'});
                                menu.remove();
                            };
                            menu.appendChild(item2);

                            // Position the menu at the click screen position.
                            menu.style.left = sx + 'px';
                            menu.style.top  = sy + 'px';

                            // Attach to document body so it floats above all Bokeh content.
                            document.body.appendChild(menu);

                            // Close the menu on the next click outside it.
                            setTimeout(function () {
                                document.addEventListener('click', function (e2) {
                                    if (!menu.contains(e2.target)) menu.remove();
                                }, { once: true });
                            }, 0);
                        });
                    }
                }
            }

            // Store click coordinates on every press for the native listener.
            window._rt_ctx_cx = cb_obj.x;
            window._rt_ctx_cy = cb_obj.y;
            window._rt_ctx_sx = cb_obj.sx;
            window._rt_ctx_sy = cb_obj.sy;
        """,
        )
        self.xy_plot.js_on_event("press", _ctx_menu_js)

        # ---- Layout ----
        self._plots = row(
            column(
                self.power_hist,
                self.speed_hist,
                self._cmd_summary,
            ),
            self.xy_plot,
        )

        self.layout = column(
            self._menu_bar,
            self._plots,
            sizing_mode="stretch_width",
        )

    @property
    def tab(self) -> TabPanel:
        """Return a Bokeh TabPanel for use in a Tabs widget."""
        return TabPanel(child=self.layout, title=self.title)

    def reset_view(self):
        """Restore the original view settings from backup."""
        (sx, ex) = self._initial_x_range
        (sy, ey) = self._initial_y_range
        if sx is not None:
            self.xy_plot.x_range.start = sx
        if ex is not None:
            self.xy_plot.x_range.end = ex
        if sy is not None:
            self.xy_plot.y_range.start = sy
        if ey is not None:
            self.xy_plot.y_range.end = ey

    @staticmethod
    def _compute_histogram(values, bins=20, range_min=0, range_max=100):
        """Compute histogram bins without numpy.

        Returns (hist, edges) where hist has `bins` counts and
        edges has `bins + 1` boundary values.
        """
        if not values:
            return [], []
        _bin_width = (range_max - range_min) / bins
        _hist = [0] * bins
        for v in values:
            if v < range_min or v >= range_max:
                continue
            _idx = min(int((v - range_min) / _bin_width), bins - 1)
            _hist[_idx] += 1
        _edges = [range_min + i * _bin_width for i in range(bins + 1)]
        return _hist, _edges

    def update_histograms(self, source: ColumnDataSource = None):
        """Rebuild power and speed histograms from source data.

        Uses persistent ColumnDataSources to avoid destroying and recreating
        renderer models, which causes UnknownReferenceError on the server.

        Parameters:
            source  Optional override source. Defaults to self.source.
        """
        if source is None:
            source = self.source

        # Power histogram — update persistent source data in-place.
        if len(source.data.get("power", [])) > 0:
            _p_hist, _p_edges = self._compute_histogram(
                source.data["power"], bins=20, range_min=0, range_max=100
            )
            _p_centers = [
                (_p_edges[i] + _p_edges[i + 1]) / 2 for i in range(len(_p_edges) - 1)
            ]
            # Map each bin's center power to a color from the LUT
            if self._color_lut:
                _p_colors = []
                for _c in _p_centers:
                    _idx = min(100, max(0, round(_c)))
                    _p_colors.append(self._color_lut[_idx])
            else:
                _p_colors = ["navy"] * len(_p_hist)
            self._power_hist_source.data = {
                "top": _p_hist,
                "center": _p_centers,
                "width": [_p_edges[1] - _p_edges[0]] * len(_p_hist),
                "color": _p_colors,
            }
        else:
            self._power_hist_source.data = {
                "top": [],
                "center": [],
                "width": [],
                "color": [],
            }

        # Speed histogram — update persistent source data in-place.
        if len(source.data.get("speed", [])) > 0:
            _s_vals = source.data["speed"]
            _s_min = min(_s_vals)
            _s_max = max(_s_vals)
            if _s_max > _s_min:
                _s_hist, _s_edges = self._compute_histogram(
                    _s_vals, bins=20, range_min=_s_min, range_max=_s_max
                )
                if _s_hist:
                    _s_centers = [
                        (_s_edges[i] + _s_edges[i + 1]) / 2
                        for i in range(len(_s_edges) - 1)
                    ]
                    self._speed_hist_source.data = {
                        "top": _s_hist,
                        "center": _s_centers,
                        "width": [_s_edges[1] - _s_edges[0]] * len(_s_hist),
                    }
            else:
                self._speed_hist_source.data = {
                    "top": [],
                    "center": [],
                    "width": [],
                }
        else:
            self._speed_hist_source.data = {
                "top": [],
                "center": [],
                "width": [],
            }

    # ---- App Integration ----

    def set_app(self, app):
        """Store a reference to the BokehApp for creating new tabs.

        Parameters:
            app  The BokehApp instance that owns this view.
        """
        self._app = app

    # ---- Range Slider ----

    def _on_range_change(self, source: str, old, new):
        """Handle changes to the start/count spinners.

        Filters the full dataset to show only vectors in the range
        [start:start+count].  Silently clamps values to valid bounds.

        Changing Start adjusts the starting index; changing Count adjusts
        how many vectors to show.  Both values are silently clamped to
        valid bounds.

        Parameters:
            source  Which spinner triggered: 'start' or 'count'.
            old     Previous value of the spinner that changed.
            new     New value of the spinner that changed.
        """
        if self._updating_range:
            return

        # Debounce: skip events that arrive faster than the minimum
        # processing interval (50ms).  During auto-repeat, events queue
        # up faster than they can be processed, causing compounding
        # backlog and eventual infinite-loop symptoms.
        _now = time.monotonic()
        if _now - self._last_range_update < 0.05:
            return
        self._last_range_update = _now

        self._updating_range = True
        try:
            _start = int(self._start_spinner.value)
            _count = int(self._count_spinner.value)

            _total = len(self._full_data.get("cmd_id", []))
            if _total == 0:
                return

            # Clamp to valid range (fail-safe: silently correct).
            _start = max(0, min(_start, _total - 1))
            _count = max(1, min(_count, _total - _start))

            # Sync spinners to clamped values (only if value changed to
            # avoid triggering recursive on_change callbacks from auto-repeat).
            if self._start_spinner.value != _start:
                self._start_spinner.value = _start
            if self._count_spinner.value != _count:
                self._count_spinner.value = _count

            _end = _start + _count
            _filtered = {}
            for _key in self._full_data:
                _filtered[_key] = self._full_data[_key][_start:_end]
            # Ensure alpha column exists for filter compatibility
            if "alpha" not in _filtered:
                _filtered["alpha"] = [1.0] * len(_filtered.get("cmd_id", []))

            self.source.data = _filtered
            self.update_histograms(self.source)
        finally:
            self._updating_range = False

    # ---- Context Menu Callbacks ----

    def _on_ctx_action(self, attr: str, old, new):
        """Handle context menu actions dispatched from CustomJS.

        Reads a JSON payload from _ctx_store.text and dispatches the
        requested action (new_tab or duplicate) to the owning BokehApp.

        Parameters:
            attr  The property name that changed ('text').
            old   Previous value.
            new   New value (JSON string).
        """
        # Guard clause: ignore empty/initial text.
        if not new:
            return

        # Reset the store immediately to prevent re-triggering.
        self._ctx_store.text = ""

        try:
            _payload = json.loads(new)
        except json.JSONDecodeError:
            return

        _action = _payload.get("action")
        if _action == "new_tab":
            _cmd_id = _payload.get("cmd_id", -1)
            if _cmd_id >= 0 and self._app is not None:
                self._app.add_tab_from_cmd_id(_cmd_id, self)
            elif self._app is not None:
                # No valid cmd_id found — duplicate instead.
                self._app.duplicate_view(self)
        elif _action == "duplicate":
            if self._app is not None:
                self._app.duplicate_view(self)

    # ---- Command Search & Filtering (Phase 5c) ----

    def _on_cmd_search(self, attr, old, new):
        """Handle command search selection.

        Parses the "cmd_id:command_name" format and highlights the
        matching vector. Uses nearest-match logic:
        1. Exact match (cmd_id == entered number)
        2. Next higher (smallest cmd_id > entered number)
        3. Highest lower (largest cmd_id < entered number)

        Parameters:
            attr  The property that changed ('value').
            old   Previous value.
            new   New value string.
        """
        # Guard clause: empty input clears highlight
        if not new:
            self._clear_highlight()
            self._cmd_open_tab_btn.disabled = True
            return

        # Parse cmd_id from "cmd_id:command_name" format at boundary
        try:
            _target = int(new.split(":")[0])
        except (ValueError, IndexError):
            return

        _data = self.source.data
        _cmd_ids = _data.get("cmd_id", [])
        if not _cmd_ids:
            self._cmd_summary.text = "No commands in current view"
            self._clear_highlight()
            self._cmd_open_tab_btn.disabled = True
            return

        # Pass 1: Exact match — cmd_id == target
        for i, cid in enumerate(_cmd_ids):
            if cid == _target:
                _summary = self._build_command_summary(i)
                self._cmd_summary.text = _summary
                self._highlight_vector(i)
                self._cmd_open_tab_btn.disabled = False
                self._searched_cmd_id = cid
                return

        # Pass 2: Nearest match — prefer next higher, fall back to highest lower
        _higher_idx = None
        _lower_idx = None
        for i, cid in enumerate(_cmd_ids):
            if cid > _target and (_higher_idx is None or cid < _cmd_ids[_higher_idx]):
                _higher_idx = i
            if cid < _target and (_lower_idx is None or cid > _cmd_ids[_lower_idx]):
                _lower_idx = i

        if _higher_idx is not None:
            _match_idx = _higher_idx
        elif _lower_idx is not None:
            _match_idx = _lower_idx
        else:
            self._cmd_summary.text = "Command not found in current view"
            self._clear_highlight()
            self._cmd_open_tab_btn.disabled = True
            return

        _summary = self._build_command_summary(_match_idx)
        self._cmd_summary.text = _summary
        self._highlight_vector(_match_idx)
        self._cmd_open_tab_btn.disabled = False
        self._searched_cmd_id = _cmd_ids[_match_idx]

    def _on_tap_select(self, attr, old, new):
        """Handle plot tap selection: populate search box with nearest vector.

        Parameters:
            attr  The property that changed ('indices').
            old   Previous indices list.
            new   New indices list (should have one index on tap).
        """
        if not new:  # empty selection (cleared by guard)
            return
        idx = new[0]
        _data = self.source.data
        _cid = _data.get("cmd_id", [])[idx]
        _cmd = _data.get("command", [])[idx]
        if _cid is not None and _cmd is not None:
            self._cmd_search.value = f"{_cid}:{_cmd}"
        # Clear selection to avoid persistent visual indicator.
        self.source.selected.indices = []

    def _build_command_summary(self, idx: int) -> str:
        """Build an HTML summary string for the vector at index idx.

        Parameters:
            idx  Index into the ColumnDataSource data arrays.

        Returns:
            An HTML string with command details for display in _cmd_summary.
        """
        _data = self.source.data
        _cmd_id = _data["cmd_id"][idx]
        _cmd_name = _data["command"][idx]
        _start_x = _data["command_start_x"][idx]
        _start_y = _data["command_start_y"][idx]
        _end_x = _data["command_end_x"][idx]
        _end_y = _data["command_end_y"][idx]
        _len = _data["length"][idx]
        _power = _data["power"][idx]
        _speed = _data["speed"][idx]
        _style = _data["style"][idx]

        return (
            f"<b>{_cmd_id}:{_cmd_name}</b><br>"
            f"start=({_start_x:.3f}mm, {_start_y:.3f}mm) \u2192 "
            f"end=({_end_x:.3f}mm, {_end_y:.3f}mm)<br>"
            f"Length: {_len:.3f}mm | Power: {_power:.1f}% | "
            f"Speed: {_speed:.1f}mm/S | Type: {_style}"
        )

    def _on_cmd_open_tab(self):
        """Open a new tab for the searched command.

        Delegates to BokehApp.add_tab_from_cmd_id and clears the
        search input on success.
        """
        # Guard clause: verify searched cmd_id and app reference
        if not hasattr(self, "_searched_cmd_id") or self._searched_cmd_id < 0:
            return
        if self._app is None:
            return

        self._app.add_tab_from_cmd_id(self._searched_cmd_id, self)
        self._cmd_search.value = ""

    def _highlight_vector(self, idx: int):
        """Highlight the vector at the given index using overlay renderer.

        Draws a thick red segment over the target vector. The overlay
        renderer sits above the main segments for visual prominence.

        Parameters:
            idx  Index of the vector to highlight in the source data.
        """
        _data = self.source.data
        # Guard clause: out-of-bounds index
        if idx < 0 or idx >= len(_data.get("cmd_id", [])):
            return
        self._highlight_source.data = {
            "x0": [_data["start_x"][idx]],
            "y0": [_data["start_y"][idx]],
            "x1": [_data["end_x"][idx]],
            "y1": [_data["end_y"][idx]],
            "color": ["#FF0000"],  # Red highlight
            "width": [3],  # Thicker line
        }

    def _clear_highlight(self):
        """Clear the vector highlight overlay.

        Resets the highlight source to empty arrays, effectively
        removing the red overlay from the plot.
        """
        self._highlight_source.data = {
            "x0": [],
            "y0": [],
            "x1": [],
            "y1": [],
            "color": [],
            "width": [],
        }

    def _on_filter_change(self, attr, old, new):
        """Apply filters and update vector alpha values.

        Evaluates the type filter (Moves/Cuts), power range, and
        speed range to compute visibility for each vector.  Invisible
        vectors are dimmed (alpha=0.2) rather than removed, preserving
        data context.

        Parameters:
            attr  The property that changed.
            old   Previous value.
            new   New value.
        """
        _data = dict(self.source.data)
        _total = len(_data.get("cmd_id", []))
        # Guard clause: no data to filter
        if _total == 0:
            return

        # Get filter states
        _active_types = self._type_filter.active  # [0]=Moves, [1]=Cuts
        _power_min, _power_max = self._power_filter.value
        _speed_min, _speed_max = self._speed_filter.value

        # Compute alpha for each vector
        _new_alpha = []
        for i in range(_total):
            _visible = True

            # Type filter
            _style = _data["style"][i] if "style" in _data else "solid"
            _is_move = _style == "dashed"
            _is_cut = _style == "solid"
            if 0 not in _active_types and _is_move:
                _visible = False
            if 1 not in _active_types and _is_cut:
                _visible = False

            # Power filter
            _power = _data["power"][i]
            if _power < _power_min or _power > _power_max:
                _visible = False

            # Speed filter
            _speed = _data["speed"][i]
            if _speed < _speed_min or _speed > _speed_max:
                _visible = False

            _new_alpha.append(1.0 if _visible else 0.2)

        _data["alpha"] = _new_alpha
        self.source.data = _data
        self._clear_highlight()

    def _update_cmd_completions(self):
        """No-op: TextInput search has no completions to maintain."""

    # ---- Save HTML Callback ----

    def _on_save_html(self):
        """Export the current view layout as a standalone interactive HTML file.

        Guard clause prevents concurrent saves.  Delegates to file_html() to
        generate the full Bokeh document with all interactive tools embedded.
        Writes the output alongside the input file with a -view.html suffix.
        """
        # Guard: prevent concurrent saves from rapid clicks
        if self._saving:
            return
        self._saving = True
        self._save_html_btn.disabled = True
        self._save_status.text = "Saving..."

        try:
            # Resolve output filename
            if self._out_stem:
                _out = Path(self._out_stem).with_suffix("")
                _path = _out.parent / f"{_out.stem}-view.html"
            elif self.args.input_file:
                _in = Path(self.args.input_file).with_suffix("")
                _path = _in.parent / f"{_in.stem}-view.html"
            else:
                _path = Path("ruida-session-view.html")

            # Generate standalone HTML from the current view layout
            _html = file_html(self.layout, CDN, title=self.title)

            # Write to file
            _path.write_text(_html, encoding="utf-8")

            self._save_status.text = f"Saved → {_path.name}"

        except Exception as _exc:
            self._save_status.text = f"Save failed: {_exc}"

        finally:
            self._save_html_btn.disabled = False
            self._saving = False
