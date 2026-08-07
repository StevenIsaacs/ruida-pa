# GlueScript Guide

**Format:** GlueScript (high-level) → rpascript (low-level)  
**Source:** `ruidadriver/rd_gluescript.py` (GlueScript class)  
**Consumed by:** `RdDriver.run_job()` via `RdDriver` which inherits from `GlueScript`  
**Status:** As-built (describes current implementation)

---

## 1. What is GlueScript?

GlueScript is a high-level scripting layer for Ruida laser controllers. It sits
between your application code and the low-level rpascript protocol, providing
a more expressive interface for defining laser jobs.

### Key Design

- **Three representations** are involved:
  - **gluescript** — the high-level commands (e.g., `move_xy_to(100.0, 50.0)`)
    that the user builds in the TUI.
  - **rpascript** — the low-level protocol commands (e.g.,
    `MOVE_FAR_XY X=100.000mm Y=50.000mm`) generated from gluescript.
  - **`.cglu`** — the on-disk persistence format for gluescript, used by
    `/gluescript save` and `/gluescript load`. The `.gs` extension was
    deliberately rejected (it conflicts with Google Apps Script).
- **Mixin pattern** — `GlueScript` is designed to be used as a mixin.
  `RdDriver` inherits from it: `class RdDriver(GlueScript)`.
- **Command registry for re-staging** — gluescript lines can be re-processed
  through the command registry to regenerate rpascript, enabling iterative
  job editing.
- **Jogs and homing are live-only** — Jog commands (`jog_*`) and homing
  commands (`home`, `home_z`, `home_u`) act on the live session and are never
  part of a saved job. Movement jogs and homing execute immediately against the
  controller; `jog_set_*` config setters configure the live jog session (speeds
  and relative distances). None of these commands are ever persisted to `.cglu`
  files nor replayed from them.

### Why GlueScript?

Writing raw rpascript requires knowledge of coordinate system management,
near/far form selection, bounding box tracking, and protocol-specific mnemonics.
GlueScript handles all of this automatically:

- **Automatic bounding box tracking** — layer and document bounding boxes are
  expanded as you add moves and cuts.
- **Automatic near/far form selection** — the right move command (MOVE_NEAR_XY
  vs MOVE_FAR_XY) is chosen based on distance from the current position.
- **Current position tracking** — the script engine maintains the virtual head
  position so moves are always relative to the last known position.
- **Deferred variable expansion** — values like bounding box coordinates can be
  referenced as `{self.doc_tr_x}` and are filled in at finalization time.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     Your Application                      │
├──────────────────────────────────────────────────────────┤
│                     RdDriver (L6)                         │
│              (inherits from GlueScript)                   │
├──────────────────────────────────────────────────────────┤
│                      GlueScript                           │
│   ┌─────────────────┐  ┌──────────────────────────────┐   │
│   │  gluescript      │  │  rpascript                    │   │
│   │  (high-level)    │  │  (low-level protocol lines)   │   │
│   ├─────────────────┤  ├──────────────────────────────┤   │
│   │ declare_job()   │  │ REF_POINT_ABSOLUTE            │   │
│   │ declare_layer() │  │ LAYER_COLOR Layer:0 ...       │   │
│   │ move_xy_to()    │  │ MOVE_NEAR_XY X=... Y=...     │   │
│   │ cut_xy_to()     │  │ CUT_FAR_XY X=... Y=...       │   │
│   │ end_job()       │  │ END_JOB                       │   │
│   └─────────────────┘  └──────────────────────────────┘   │
├──────────────────────────────────────────────────────────┤
│                   Ruida Controller                        │
└──────────────────────────────────────────────────────────┘
```

### File Layout

| Path | Role |
|------|------|
| `ruidadriver/rd_gluescript.py` | `GlueScript` class — high-level job scripting mixin |
| `ruidadriver/ruida_driver.py` | `RdDriver(GlueScript)` — combines scripting with execution |
| `rpascript/tui_adapter.py` | TUI adapter — `/gluescript` command implementation |

---

## 3. Workflow

A typical GlueScript session follows this sequence:

```
new_gluescript()
    ↓
declare_job(ref=point, ...)
    ↓
declare_layer(mode=..., ...)
    ↓
move_xy_to(...) / cut_xy_to(...) / power(...) / air_assist_on() / air_assist_off()
    ↓
declare_layer(mode=..., ...)
    ↓
move_xy_to(...) / cut_xy_to(...) / power(...) / air_assist_on() / air_assist_off()
    ↓
end_job()
    ↓
stage_gluescript()
    ↓
run_job()   # no job argument — runs the rpascript most recently staged by stage_gluescript()
```

### Step-by-Step

1. **`new_gluescript()`** — Reset all script data for a fresh job. Clears both
   gluescript and rpascript lists, resets bounding boxes and the layer
   counter. Tracked position is preserved — only a fresh GlueScript instance
   starts at (0, 0, 0, 0); subsequent calls keep the tracked position, which
   during a live session follows the controller-reported machine position.

2. **`declare_job(ref=...)`** — Declare a new job with a reference point type.
   This resets all data (like `new_gluescript()`) and emits job header commands:
   reference point setup, `START_JOB`, job copies, and bounding box placeholders.

3. **`declare_layer(mode=...)`** — Declare a new layer with its configuration:
   mode, color, speed, power, overscan, etc. Increments the internal layer
   counter and emits layer setup rpascript commands.

4. **Add operations** — Within each layer, add moves, cuts, and power
   changes. Each of these operations generates both a gluescript line and
   corresponding rpascript lines. (Jog commands are live-only: movement
   jogs execute immediately and `jog_set_*` setters configure the live jog
   session — none of them are ever part of the saved job. Homing commands
   (`home`, `home_z`, `home_u`) are live-only too: they home the machine
   immediately against the connected controller. See Section 5.)

5. **`end_job()`** — Complete the job definition. Emits `END_JOB` in the
   rpascript and marks the job ready for staging.

6. **`stage_gluescript()`** — Finalize the rpascript by expanding deferred
   variable references (like `{self.doc_tr_x}`). Returns `True` when the
   gluescript was successfully staged; the assembled rpascript is available
   via `driver.rpascript`.

7. **`run_job()`** — (Inherited from `RdDriver`) Composes head + job + tail
   scripts and queues them for background execution on the controller. Called
   with no `job` argument it runs the rpascript most recently staged by
   `stage_gluescript()`.

---

## 4. Python API Reference

### 4.1 Job Lifecycle

#### `new_gluescript()`

Reset all script data for a new job. Clears both gluescript (`self.gluescript`)
and rpascript (`self.rpascript`) lists, resets bounding boxes to their empty
(±inf) state, and the layer counter to 0. Tracked position is preserved — only
a fresh GlueScript instance starts at (0, 0, 0, 0); subsequent calls keep the
tracked position, which during a live session follows the controller-reported
machine position.

```python
driver = RdDriver()
driver.new_gluescript()
```

#### `declare_job(label: str, ref_point: str = "MACHINE", abs_xy: list[float] | None = None, columns: int = 1, rows: int = 1, xstep: float = 0.0, ystep: float = 0.0)`

Declare a new job. This resets all script data first (equivalent to calling
`new_gluescript()`), then emits the job header.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `label` | `str` | (required) | Label to identify the job |
| `ref_point` | `str` | `"MACHINE"` | Reference point: `MACHINE`, `ABSOLUTE`, `CURRENT`, `SET_POINT` |
| `abs_xy` | `list[float] \| None` | `None` | Absolute XY coordinate (required only for `ABSOLUTE` ref_point) |
| `columns` | `int` | `1` | Number of columns for job copies |
| `rows` | `int` | `1` | Number of rows for job copies |
| `xstep` | `float` | `0.0` | X step distance in mm between job copies |
| `ystep` | `float` | `0.0` | Y step distance in mm between job copies |

**Reference point types:**

| Ref Point | Description |
|-----------|-------------|
| `MACHINE` | Use machine origin (absolute coordinate system) |
| `ABSOLUTE` | Jog to specified `abs_xy` first, then use current position |
| `CURRENT` | Use current head position as reference |
| `SET_POINT` | Use the anchor point (previously set by operator) |

**Raises:** `ValueError` if `ref_point` is invalid.

```python
driver.declare_job("My Job", ref_point="MACHINE")
driver.declare_job("Panel Cut", ref_point="ABSOLUTE", abs_xy=[100.0, 50.0])
driver.declare_job("Array Job", ref_point="MACHINE", columns=2, rows=3, xstep=100.0, ystep=100.0)
```

#### `end_job()`

Complete the job definition. Marks the job as ready for staging and emits
`END_JOB` in the rpascript.

**Raises:** `RuntimeError` if called twice or before `declare_job()`.

```python
driver.end_job()
```

### 4.2 Layer Management

#### `declare_layer(label: str, color: str, mode: str = "VECTOR", overscan: str = "NONE", speed: float = 100.0, frequency: float = 20.0, min_power_1: float = 8.0, max_power_1: float = 70.0)`

Declare a new layer. Increments the internal layer counter and emits layer
configuration rpascript commands.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `label` | `str` | (required) | Label for the layer |
| `color` | `str` | (required) | Color formatted as `#rrggbb` |
| `mode` | `str` | `"VECTOR"` | Layer mode: `VECTOR`, `RASTER`, `DITHER`, `IMAGE`, `DEPTHMAP` |
| `overscan` | `str` | `"NONE"` | Overscan mode: `NONE`, `X`, `X_BI`, `Y`, `Y_BI`, `XY` |
| `speed` | `float` | `100.0` | Layer speed in mm/s |
| `frequency` | `float` | `20.0` | Laser PWM frequency in KHz |
| `min_power_1` | `float` | `8.0` | Minimum layer power percent for head 1 |
| `max_power_1` | `float` | `70.0` | Maximum layer power percent for head 1 |

**Power validation:**

- `min_power_1` below 8% raises `ValueError` — CO2 lasers will not reliably
  fire below this threshold.
- `max_power_1` above 70% logs a warning — CO2 laser tube life is reduced
  at higher power settings.

**Raises:** `ValueError` if mode or overscan is invalid.

```python
driver.declare_layer("Engrave", "#000000", mode="VECTOR", speed=300.0, min_power_1=15.0, max_power_1=60.0)
driver.declare_layer("Cut", "#FF0000", mode="VECTOR", speed=50.0, min_power_1=20.0, max_power_1=85.0)
driver.declare_layer("Grayscale", "#808080", mode="IMAGE", overscan="X")
```

### 4.3 Layer Actions

#### `move_xy_to(x: float, y: float)`

Move the laser head to absolute XY coordinates relative to the job reference
point, without firing the laser. Automatically selects near or far form based
on distance from the current position. The move is routed to the currently
active layer (the last declared layer); there is no layer argument.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `x` | `float` | Absolute X coordinate in mm |
| `y` | `float` | Absolute Y coordinate in mm |

```python
driver.move_xy_to(100.0, 50.0)
# Produces: MOVE_NEAR_XY X=100.000mm Y=50.000mm  (or MOVE_FAR_XY)
```

#### `cut_xy_to(x: float, y: float)`

Move the laser head to absolute XY coordinates while cutting (laser enabled).
Same near/far form selection as `move_xy_to`. The cut is routed to the
currently active layer.

```python
driver.cut_xy_to(200.0, 100.0)
# Produces: CUT_NEAR_XY X=200.000mm Y=100.000mm  (or CUT_FAR_XY)
```

#### `power(percent: float | None = None)`

Set the immediate laser power percentage for the currently active layer. Only
valid for `IMAGE` and `DEPTHMAP` layer modes. In other modes, a warning is
logged and the call is ignored.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `percent` | `float` | Power percentage (0–100) |

```python
driver.power(45.0)
# Produces: IMD_POWER_1 Power:45.0%
```

#### `air_assist_on()`

Enable air assist for the current layer. Expands to `AIR_ASSIST_ON` in the
layer's action block.

```python
driver.air_assist_on()
# Produces: AIR_ASSIST_ON
```

#### `air_assist_off()`

Disable air assist for the current layer. Expands to `AIR_ASSIST_OFF` in the
layer's action block.

```python
driver.air_assist_off()
# Produces: AIR_ASSIST_OFF
```

#### `jog_xy_to(x: float, y: float)`

Jog the laser head to an absolute XY coordinate. Generates a speed command
followed by a `JOG_XY` command relative to the machine origin.

```python
driver.jog_xy_to(x=50.0, y=50.0)
# Produces:
#   SPEED_LASER_1 100
#   JOG_XY Rel:MACHINE X=50.0mm Y=50.0mm
```

### 4.4 Single-Axis Operations

GlueScript also provides single-axis variants for each operation:

| Method | Description |
|--------|-------------|
| `move_x_to(x)` | Move on X axis only |
| `move_y_to(y)` | Move on Y axis only |
| `cut_x_to(x)` | Cut on X axis only |
| `cut_y_to(y)` | Cut on Y axis only |
| `jog_x_to(x)` | Jog on X axis only |
| `jog_y_to(y)` | Jog on Y axis only |

### 4.5 Jog Configuration

Configure jog speeds and relative distances:

```python
driver.jog_set_xy_speed(150.0)    # XY jog speed in mm/s
driver.jog_set_z_speed(50.0)      # Z jog speed in mm/s
driver.jog_set_u_speed(50.0)      # U jog speed in mm/s
driver.jog_set_xy_rel(25.0)       # Relative XY jog distance
driver.jog_set_z_rel(10.0)        # Relative Z jog distance
driver.jog_set_u_rel(10.0)        # Relative U jog distance
```

The setters configure the live jog session only — they never produce
gluescript lines (they are live-only, see Section 5).

Relative jog methods use the configured distances as defaults when called
without arguments:

```python
driver.jog_xy_rel()               # Uses x_rel and y_rel (default 10.0 mm)
driver.jog_xy_rel(x=20.0, y=15.0)  # Override for this call
driver.jog_z_rel(z=5.0)
```

### 4.5.1 Homing

Home the machine axes. Homing commands are bare no-parameter mnemonics
expanding to a single rpascript line each:

```python
driver.home()     # Produces: HOME_XY
driver.home_z()   # Produces: HOME_Z
driver.home_u()   # Produces: HOME_U
```

| Method | Expands to | Description |
|--------|------------|-------------|
| `home()` | `HOME_XY` | Home X and Y axes (machine origin) |
| `home_z()` | `HOME_Z` | Home Z axis |
| `home_u()` | `HOME_U` | Home U axis (rotary) |

Homing commands are **live-only**, like movement jogs: they execute
immediately against a connected controller, are never appended to the
gluescript, and their lines in a `.cglu` file are ignored with a warning
on load (see Section 5).

### 4.6 Utilities

#### `comment(comments: list[str])`

Append comment lines to the rpascript. Each comment is prefixed with `# `.

```python
driver.comment(["Setup complete", "Starting engrave pass"])
```

#### `inline(commands: list[str])`

Insert raw rpascript commands directly into the assembled output at the call
point. Intended for working around issues or experimentation — a need for
`inline()` suggests a new GlueScript method may be needed.

Inline commands land positionally, exactly where they were called:

- **Before the first layer is declared** — right after the job header (after
  the `JOB_COPIES ...` line), before the first layer's attribute lines.
- **Inside a declared layer** — in that layer's action block, between the
  surrounding actions at the call position.
- **After `end_job()`** — just before the closing `END_JOB` line.

For example, an `inline()` call between two actions keeps that position in
the assembled rpascript:

```python
driver.declare_job("Plate", "MACHINE")
driver.declare_layer("Outline", "#0000FF", mode="VECTOR", overscan="NONE", speed=120.0)
driver.move_xy_to(50.0, 50.0)
driver.inline(["AIR_ASSIST_ON", "LASER_ON"])   # inserted between the two actions
driver.cut_xy_to(150.0, 50.0)
driver.end_job()
```

stages to:

```
MOVE_FAR_XY X=50.000mm Y=50.000mm
AIR_ASSIST_ON
LASER_ON
CUT_FAR_XY X=150.000mm Y=50.000mm
```

A warning is logged during staging if `inline()` was used.

### 4.7 Staging and Execution

#### `stage_gluescript(gluescript: list[str] | None = None) -> bool`

Finalize the rpascript, expanding deferred variable references.

- When `gluescript` is `None`: finalizes the currently generated rpascript.
- When `gluescript` is provided: re-generates rpascript by processing each
  gluescript command line through the command registry, then finalizes.

**Returns:** `bool` — `True` when the gluescript was successfully staged
(the finalized rpascript is available via `driver.rpascript`).

**Raises:** `RuntimeError` if `end_job()` has not been called.

```python
driver.stage_gluescript()
rpa_lines = driver.rpascript
print(f"Generated {len(rpa_lines)} rpascript lines")
```

#### `run_job(job: list[str] | None = None, auto_checksum: bool = False)`

(Inherited from `RdDriver`) Queue a job for execution, composing head + job +
tail scripts, then sending the result to the controller. When `job` is
omitted, the rpascript most recently staged by `stage_gluescript()` is run.

```python
# Stage then execute
driver.stage_gluescript()
driver.run_job()
# Or use the combined approach via the TUI
```

### 4.7.1 Getters

These getters return **copies** of the driver's scripting state — never
aliases — so mutating the returned list cannot corrupt the driver.

#### `get_gluescript() -> list[str]`

Return a copy of the gluescript transcript (the DSL lines, e.g.
`declare_job(...)`, `move_xy_to(...)`). Adapter-level getter exposed over
RPC (also accessible as the `driver.gluescript` attribute on the driver);
returns `[]` when no driver.

#### `get_rpascript() -> list[str]`

Return a copy of the assembled rpascript. Adapter-level getter exposed over
RPC (also accessible as the `driver.rpascript` attribute); returns `[]` when
no driver.

> **Note:** Over RPC these getters report the server's last-flushed state
> (see the integration guide §8.9 batching section).

---

## 5. TUI Usage (`/gluescript`)

The TUI provides interactive access to GlueScript via the `/gluescript` command.

Every GlueScript method documented below is also callable over RPyC — the
server exposes all 40 authoring, config-setter, live-jog, homing, and getter methods, so a
remote client can build and stage a job and drive live commands without the TUI. See the [integration guide §8.9](integration-guide.md#89-gluescript-job-authoring--live-commands-via-rpc)
for the full method table, session-less authoring example, and the `exposed_`-prefix note.

### Available Subcommands

| Subcommand | Description |
|------------|-------------|
| `new [label]` | Reset and declare a new job (MACHINE ref, optional label) |
| `show` | Display current state summary (line counts, layer, position) |
| `stage` | Finalize (if needed) and generate rpascript from gluescript (re-stage if already staged); the generated rpascript becomes the loaded script (`.rds` view) |
| `run` | Finalize (if needed), stage, and execute the job |
| `save <path>` | Persist the current gluescript to a `.cglu` file |
| `load <path>` | Load a `.cglu` file, validate it, and stage it |
| `edit` | Open the gluescript in a full-screen editor; on save, validate and re-stage it |
| `list` | Display high-level gluescript commands |

### Working Without an Active Session

The `/gluescript` subcommands `new`, `show`, `list`, `stage`,
`save`, `load`, and `edit` work with **no active session** — the GlueScript
transcript is held in-memory on the `RdDriver`, so a job can be built, edited,
and staged without any controller connection. Only `/gluescript run` requires a
live session and fails with `No active session to run gluescript.` when the
driver is absent or the controller is not connected. (Jog and home commands
remain live-only, as described above.)

- `stage`, `run`, `load`, and `edit` (on save) make the generated rpascript
  the loaded script — the same `.rds` slot `/load` fills — so it can be
  viewed with `/list script` and edited with `/edit`.

- The transcript survives session end/disconnect — it is preserved on teardown
  and restored automatically the next time a session-less subcommand (re)creates
  the driver. An incomplete transcript (e.g. saved mid-job without `end_job()`)
  also restores fine.
- If a preserved transcript cannot be restored, it is dumped to
  `tmp/gluescript-recovery-<timestamp>.cglu` with an error log line pointing to
  the file.

### Persistence: `save` and `load`

The current gluescript — the DSL lines built up with `new` and
edited through the editor — can be persisted to disk and reloaded:

- **`/gluescript save <path>`** writes the gluescript to `<path>`. If the path's
  basename contains no `.`, the tool auto-appends `.cglu`; an explicit path
  (e.g. `myfile.custom`) is used as-is. Logs
  `GlueScript saved to <path> (N lines)`.
- **`/gluescript load <path>`** reads a `.cglu` file (same auto-append rule for
  the extension), validates it on a throwaway `GlueScript` instance, and — only
  if validation passes — applies it via `driver.stage_gluescript(lines)`. Logs
  `Loaded N gluescript lines from <path>, staged M rpascript lines`.

Load **auto-stages** the file: after a successful load the rpascript is ready,
but finalization still requires `end_job()` in the file (the job is only marked
complete when `end_job()` is replayed). Load errors fail loud without corrupting
live state and cover: file not found, permission denied, non-text file,
empty/blank-only file, "no stageable commands" (all-live-only or comments-only
files), and a validation failure reported as `Load failed: ...`.

> **Caution — two accepted footguns.** After `stage`, `run`, `load`, or
> `edit`, a bare `/save` still opens the file browser preselected on the last
> `/load`-ed `.rds` path (`_loaded_script_path`), so saving there could
> overwrite that `.rds` file with gluescript output — pick a new path. And
> until a file is loaded, `/plot`'s title and `/export`'s default path may
> show the last `/load`-ed basename (stale `_plot_source` label).

### Editing: `edit`

`/gluescript edit` opens the current gluescript transcript in the TUI's
full-screen editor (Ctrl+S saves, Esc cancels). The editor shows the same
lines that `/gluescript list` displays — the canonical `.cglu` format that
`save` writes.

On save the edited lines go through the same pipeline as `load`: live-only
jog/home lines are ignored with a warning (`ignoring live-only command line
after edit`), the result is validated on a throwaway `GlueScript` instance,
and only then applied via `driver.stage_gluescript(lines)` — which rebuilds
both the rpascript and the transcript. A failed validation reports
`Edit failed: ...` and leaves the live state untouched. Success logs
`GlueScript: Edited — N gluescript lines, staged M rpascript lines`.
Cancelling logs `GlueScript: Edit cancelled.` With nothing to edit the
command reports `Nothing to edit (gluescript is empty). Use /gluescript new
or /gluescript load first.`

### Jog & Home Commands Are Live-Only

All 16 jog commands (`jog_xy_to`, `jog_x_to`, `jog_y_to`, `jog_z_to`,
`jog_u_to`, `jog_xy_rel`, `jog_x_rel`, `jog_y_rel`, `jog_z_rel`, `jog_u_rel`,
plus the `jog_set_*` config setters: `jog_set_xy_speed`, `jog_set_z_speed`,
`jog_set_u_speed`, `jog_set_xy_rel`, `jog_set_z_rel`, `jog_set_u_rel`) and the
3 homing commands (`home`, `home_z`, `home_u`) are live-only:

- **In the TUI**, there is no `/gluescript layer` wrapper anymore — jog commands
  are used as bare TUI commands only (see next paragraph). A bare jog never
  appends to the gluescript. It immediately runs the returned rpascript lines
  against the controller when there is an active session (`driver.is_connected`).
  With no active session it warns and ignores the jog; if the background script
  runner is dead it warns `not sent — <reason>`. Homing behaves the same way:
  `home`/`home_z`/`home_u` run immediately against a connected controller.
- **In a `.cglu` file**, jog and home lines are ignored with a warning on load
  (`ignoring live-only command line on load`) and are never used for position
  tracking — the re-stage loop skips all `LIVE_ONLY_COMMANDS`.
- **`jog_set_*` config setters** (speed / relative distance) are live-only too —
  they configure defaults for the live jog session, are never appended to the
  gluescript, and lines in a `.cglu` file are ignored with a warning on load.

### Bare Jog & Home Commands

All 16 jog commands and the 3 homing commands are also available in the TUI as
**bare commands**, alongside `session`/`server`:

```
home                         # Home X and Y axes (machine origin)
home_z                       # Home Z axis
home_u                       # Home U axis (rotary)
jog_xy_to 10 20            # Jog XY to absolute position (mm)
jog_z_to 5                 # Jog Z to absolute position (mm, max 2000)
jog_xy_rel                 # Jog XY relative, using configured defaults
jog_set_xy_speed 150       # Set XY jog speed (mm/s) — applies live
jog_set_xy_rel 25          # Set relative XY jog distance (mm) — applies live
```

- Bare jog and home commands are live-only: movement jogs and homing run
  immediately against a connected controller; `jog_set_*` setters configure
  the live jog session and never produce gluescript lines.
- Typing a `jog` or `home` prefix brings up autocomplete with usage text;
  `/help` lists all 19 under "Jog & Home commands (live-only)".
- Movement jogs without an active session warn-and-ignore; `jog_z_to` with
  `z > 2000` is refused (no rpascript lines are produced).

### Example Session

```
> /gluescript new My Job
GlueScript: New job started (label='My Job', ref=MACHINE).

> /gluescript edit
(opens the full-screen editor with the transcript; add the `declare_layer`, move/cut, and `end_job` lines, Ctrl+S saves)

> /gluescript stage
GlueScript: Staged 28 rpascript lines.
(`/gluescript stage` logs `Job finalized.` only when it auto-finalizes a transcript that has no `end_job()` line yet.)

> /gluescript list
   0: declare_job('My Job', 'MACHINE', [0.0, 0.0], 1, 1, 0.0, 0.0)
   1: declare_layer('Layer 1', '#000000', 'VECTOR', 'NONE', 300.0, 20.0, 15.0, 60.0)
   2: move_xy_to(100.0, 50.0)
   3: cut_xy_to(200.0, 100.0)
   4: end_job()

> /gluescript stage
GlueScript: Staged 28 rpascript lines.
(The staged rpascript is now the loaded script — the same slot /load fills.)

> /list script
   0: # Job: My Job
   1: # Generated by: GlueScript 0.14.0
   2: REF_POINT_ABSOLUTE
   3: SET_ABSOLUTE
   4: REF_POINT_SET
   5: START_JOB
   ...

> /edit
(opens the staged rpascript in the full-screen editor; Ctrl+S saves, Esc cancels)
```

---

## 6. Status Bar Indicators

In the TUI, the status bar displays GlueScript state when rpascript has been
staged:

```
Connected | UDP 192.168.1.100:50200  |  ...  |  GS:5 RPA:28 S
```

| Indicator | Meaning |
|-----------|---------|
| `GS:N` | Number of gluescript (high-level) lines |
| `RPA:N` | Number of rpascript (low-level) lines |
| `S` (yellow) | Rpascript has been staged but not yet executed |
| `R` (green) | Rpascript has been executed (via `/gluescript run`) |

The indicators only appear when rpascript lines exist. They are hidden when
no rpascript has been generated.

---

## 7. Near/Far Form Selection

GlueScript automatically chooses between near and far movement forms based on
the distance from the current head position.

### The Threshold

The Ruida protocol encodes near-form coordinates as signed 14-bit integers
divided by 1000, giving a range of approximately ±8.192mm:

| Form | Command | Encoding | Range |
|------|---------|----------|-------|
| Near | `MOVE_NEAR_XY` | Signed 14-bit / 1000 | ±8.192mm relative |
| Far | `MOVE_FAR_XY` | Absolute position (full bed) | Full bed coordinates |

### Selection Logic

```
delta = target_position - current_position

if -8.192 <= delta_x <= 8.191 and -8.192 <= delta_y <= 8.191:
    use NEAR form (MOVE_NEAR_XY / CUT_NEAR_XY)
else:
    use FAR form (MOVE_FAR_XY / CUT_FAR_XY)
```

For single-axis operations (`move_x_to`, `move_y_to`, `cut_x_to`, `cut_y_to`),
the check is performed on the single axis delta.

The boundary is the signed range -8.192 mm to +8.191 mm — a signed 14-bit
value divided by 1000, so it is asymmetric by 0.001 mm (near-form fits values
from -8.192 inclusive through +8.191 inclusive).

### Why Two Forms?

Near-form commands use relative offsets from the current position, which
requires fewer bytes on the wire and is more efficient for closely-spaced
operations. Far-form commands use absolute coordinates and can address any
point on the bed.

GlueScript selects the appropriate form automatically — you always use
absolute target coordinates in your code.

### Position Tracking During a Live Session

During an active live session, GlueScript's tracked position (X/Y/Z/U) is
updated from the controller's `MEM_CURRENT_POSITION` replies received by the
reply listener — but **only between jobs**. While a job is being assembled
(`declare_job` → `end_job`) or re-staged, the tracked position is the
trajectory cursor and replies are ignored.

Tracked position is preserved across `new_gluescript()` and re-stage; only a
fresh GlueScript instance starts at (0, 0, 0, 0). A consequence of this is
that near/far form selection for the first move of a new job is computed from
the machine's actual position — a jog to (100, 50) followed by
`move_xy_to(1, 0)` selects **FAR**, not a wrong NEAR.

After homing (`home`/`home_z`/`home_u`), replies resync the tracked position
to the controller-reported home position (0, 0, 0, 0 after homing).

---

## 8. Limitations

### Z and U Axis Movement/Cut

Z-axis (table height) and U-axis (rotary) moves and cuts are not implemented
in the initial release. Calling `move_z_to()`, `move_u_to()`, `cut_z_to()`,
or `cut_u_to()` raises `NotImplementedError`.

Z and U jog operations (`jog_z_to`, `jog_u_to`, `jog_z_rel`, `jog_u_rel`)
**are** implemented and functional.

### Ruida Coordinate System

The Ruida controller uses a coordinate system where the origin (0,0) is at the
**top-right** of the work area. The X axis increases to the **left** and the Y
axis increases **downward**. This is the opposite of the conventional
bottom-left Cartesian origin.

This means:
- **Top-right corner** = smallest X and Y values
- **Bottom-left corner** = largest X and Y values

Bounding box methods (`LAYER_TOP_RIGHT`, `LAYER_BOTTOM_LEFT`) emit the
geometrically smallest X and Y values for top-right and the largest for
bottom-left.

Per-layer bounding boxes are emitted as concrete values at layer boundaries
(next `declare_layer()` call or `end_job()`), only for layers that contain
at least one move or cut operation. Empty layers (no content) skip bounding
box emission entirely.

### Re-Staging

Re-staging (calling `stage_gluescript()` with a gluescript list) parses each
gluescript command line and replays it through the command registry:

- Standard commands are replayed via their corresponding methods
- Jog commands (`jog_*`, including `jog_set_*` config setters) and homing
  commands (`home`, `home_z`, `home_u`) are skipped — they are live-only and
  are never replayed or used for position tracking during re-staging
- `inline()` commands are passed through verbatim — they are stored as-is and
  not re-parsed, and land positionally in the output, exactly where they were
  called
- The command registry maps method names to bound methods; if a gluescript line
  references an unknown command, a `RuntimeError` is raised

**Comment lines:** Raw `#` full-line comments are tolerated as no-ops during
re-staging — they are skipped and generate nothing in the rpascript output.
Inline comments (`move_xy_to(285, 175) # note`) are stripped before parsing,
with `#` characters inside quoted arguments (e.g., a `'#00FF00'` color) left
intact. To emit comments into the generated rpascript, use the `comment()`
method instead.

### Power Command Validity

`power()` is only valid for `IMAGE` and `DEPTHMAP` layer modes. Calling it in
`VECTOR`, `RASTER`, `DITHER`, or other modes logs a warning and the call is
ignored. This matches the Ruida protocol where immediate power commands are
only meaningful when processing raster image data between moves.

### Power Range Warnings

- Minimum power below 8% raises `ValueError` — CO2 lasers will not reliably
  fire below this threshold.
- Maximum power above 70% logs a warning — operating above 70% reduces CO2
  laser tube life.

---

## 9. Dual Representation Example

Each GlueScript method generates a corresponding line in both `gluescript`
and `rpascript`. Here is a complete example showing both representations:

### GlueScript (high-level)

```
declare_job('Demo', 'MACHINE', [0.0, 0.0], 1, 1, 0.0, 0.0)
declare_layer('Outline', '#0000FF', mode='VECTOR', overscan='NONE', speed=120.0, frequency=20.0, min_power_1=20.0, max_power_1=80.0)
move_xy_to(50.0, 50.0)
cut_xy_to(150.0, 50.0)
cut_xy_to(150.0, 150.0)
cut_xy_to(50.0, 150.0)
cut_xy_to(50.0, 50.0)
end_job()
```

### Generated rpascript (low-level)

```
# Job: Demo
# Generated by: GlueScript 0.14.0
REF_POINT_ABSOLUTE
SET_ABSOLUTE
REF_POINT_SET
START_JOB
FEED_REPEAT 0 0
SET_FEED_AUTO_PAUSE State:OFF
# Job settings
JOB_TOP_RIGHT X=150.000mm Y=150.000mm
JOB_BOTTOM_LEFT X=50.000mm Y=50.000mm
DOCUMENT_TOP_RIGHT X=150.000mm Y=150.000mm
DOCUMENT_BOTTOM_LEFT X=50.000mm Y=50.000mm
JOB_COPIES Columns=1 Rows=1 XStep=0.000mm YStep=0.000mm
# Layer 0: Outline
LAYER_COLOR Layer:0 Color:\#0000FF
OVERSCAN_OFF
LAYER_SPEED_LASER_1 Layer:0 Speed:120.000mm/S
LAYER_MIN_POWER_1 Layer:0 Power:20.0%
LAYER_MAX_POWER_1 Layer:0 Power:80.0%
LAYER_ATTRIBUTES Layer:0 0
LAYER_TOP_RIGHT Layer:0 X=150.000mm Y=150.000mm
LAYER_BOTTOM_LEFT Layer:0 X=50.000mm Y=50.000mm
LAST_LAYER Layer:0
SELECT_LAYER Layer:0
MOVE_FAR_XY X=50.000mm Y=50.000mm
CUT_FAR_XY X=150.000mm Y=50.000mm
CUT_FAR_XY X=150.000mm Y=150.000mm
CUT_FAR_XY X=50.000mm Y=150.000mm
CUT_FAR_XY X=50.000mm Y=50.000mm
END_JOB
```

The rpascript has three sections: (1) job header with reference point setup and
document settings, (2) all layer attributes sorted by layer number, and (3) layer
actions where each layer's block is prefixed with `SELECT_LAYER Layer:{n}`.
Emitted layer indices are 0-based — the Ruida controller numbers layers from 0
(gluescript's internal numbering stays 1-based). `LAST_LAYER Layer:{n}` is
emitted between the attributes and the actions, reporting the 0-based index of
the last layer in the job.

---

## 10. Complete Python Example

```python
from ruidadriver.ruida_driver import RdDriver

driver = RdDriver()

# Phase 1: Define the job
driver.declare_job("Panel Cut", ref_point="MACHINE")

# Phase 2: Add layers
driver.declare_layer(
    "Engrave", "#000000",
    mode="VECTOR", speed=300.0,
    min_power_1=15.0, max_power_1=60.0
)

# Phase 3: Add operations to the current layer
driver.move_xy_to(10.0, 10.0)
driver.cut_xy_to(210.0, 10.0)
driver.cut_xy_to(210.0, 110.0)
driver.cut_xy_to(10.0, 110.0)
driver.cut_xy_to(10.0, 10.0)

# Phase 4: Complete the job
driver.end_job()

# Phase 5: Stage and execute
driver.stage_gluescript()
rpa = driver.rpascript
print(f"Generated {len(rpa)} rpascript lines")

# Phase 6: Connect and run (requires RdDriver session)
# driver.start(udp_host="192.168.1.100")
# driver.run_job()
```

---

## 11. Deferred Variable Expansion

GlueScript uses deferred variable references in rpascript lines. These are
expanded at stage time (when `stage_gluescript()` is called), not at generation
time. This allows bounding box coordinates to be collected incrementally and
filled in at the end.

### Syntax

Deferred references use the format `{self.<attribute>}` where `<attribute>`
is a Python attribute path on the GlueScript instance:

```
JOB_TOP_RIGHT X={self.doc_tr_x}mm Y={self.doc_tr_y}mm
```

### Supported Attributes

| Reference | Description |
|-----------|-------------|
| `{self.doc_tr_x}` | Document top-right X (expanded from `doc_tr_x`) |
| `{self.doc_tr_y}` | Document top-right Y (expanded from `doc_tr_y`) |
| `{self.doc_bl_x}` | Document bottom-left X (expanded from `doc_bl_x`) |
| `{self.doc_bl_y}` | Document bottom-left Y (expanded from `doc_bl_y`) |

Floating point values are formatted with 3 decimal places. Other types use
`str()` conversion.

---

## 12. Command Registry

GlueScript maintains an internal command registry that maps method names to
bound methods. This registry is used during re-staging to replay gluescript
commands.

### Registered Commands

```
new_gluescript
comment
inline
declare_job
end_job
declare_layer
move_xy_to, move_x_to, move_y_to
cut_xy_to, cut_x_to, cut_y_to
power
air_assist_on, air_assist_off
jog_set_xy_speed, jog_set_z_speed, jog_set_u_speed
jog_set_xy_rel, jog_set_z_rel, jog_set_u_rel
jog_xy_to, jog_x_to, jog_y_to, jog_z_to, jog_u_to
jog_xy_rel, jog_x_rel, jog_y_rel, jog_z_rel, jog_u_rel
home, home_z, home_u
```

### Re-Staging Flow

```
gluescript commands (list of strings)
    ↓
parse each line into (method_name, args)
    ↓
look up method_name in command registry
    ↓
call method(*args)
    ↓
method regenerates rpascript lines
    ↓
expand deferred variables
    ↓
finalized rpascript
```
