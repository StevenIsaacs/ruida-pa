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
- **Jogs are live-only** — Jog commands (`jog_*`) act on the live session and are
  never persisted to `.cglu` files nor replayed from them. Movement jogs execute
  immediately against the controller; `jog_set_*` config setters configure the
  live jog session (speeds and relative distances).

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
│   │ declare_layer() │  │ LAYER_COLOR Layer:1 ...       │   │
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
stage_rpascript()
    ↓
run_job()
```

### Step-by-Step

1. **`new_gluescript()`** — Reset all script data for a fresh job. Clears both
   gluescript and rpascript lists, resets position tracking, bounding boxes,
   and the layer counter.

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
   session — none of them are ever part of the saved job — see Section 5.)

5. **`end_job()`** — Complete the job definition. Emits `END_JOB` in the
   rpascript and marks the job ready for staging.

6. **`stage_rpascript()`** — Finalize the rpascript by expanding deferred
   variable references (like `{self.doc_tr_x}`). Returns the finalized list
   of rpascript command lines.

7. **`run_job()`** — (Inherited from `RdDriver`) Composes head + job + tail
   scripts and queues them for background execution on the controller.

---

## 4. Python API Reference

### 4.1 Job Lifecycle

#### `new_gluescript()`

Reset all script data for a new job. Clears both gluescript (`self.gluescript`)
and rpascript (`self.rpascript`) lists, resets current position to (0, 0),
bounding boxes to 0, and the layer counter to 0.

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

#### `move_xy_to(layer: int, x: float, y: float)`

Move the laser head to absolute XY coordinates relative to the job reference
point, without firing the laser. Automatically selects near or far form based
on distance from the current position.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layer` | `int` | The layer this move belongs to (0-indexed) |
| `x` | `float` | Absolute X coordinate in mm |
| `y` | `float` | Absolute Y coordinate in mm |

```python
driver.move_xy_to(layer=0, x=100.0, y=50.0)
# Produces: MOVE_NEAR_XY X=100.000mm Y=50.000mm  (or MOVE_FAR_XY)
```

#### `cut_xy_to(layer: int, x: float, y: float)`

Move the laser head to absolute XY coordinates while cutting (laser enabled).
Same near/far form selection as `move_xy_to`.

```python
driver.cut_xy_to(layer=0, x=200.0, y=100.0)
# Produces: CUT_NEAR_XY X=200.000mm Y=100.000mm  (or CUT_FAR_XY)
```

#### `power(layer: int, power: float)`

Set the immediate laser power level. Only valid for `IMAGE` and `DEPTHMAP`
layer modes. In other modes, a warning is logged and the call is ignored.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `layer` | `int` | The layer this power command belongs to |
| `power` | `float` | Power percentage (0–100) |

```python
driver.power(layer=0, power=45.0)
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
#   JOG_XY Rel:MACHINE X=50.000mm Y=50.000mm
```

### 4.4 Single-Axis Operations

GlueScript also provides single-axis variants for each operation:

| Method | Description |
|--------|-------------|
| `move_x_to(layer, x)` | Move on X axis only |
| `move_y_to(layer, y)` | Move on Y axis only |
| `cut_x_to(layer, x)` | Cut on X axis only |
| `cut_y_to(layer, y)` | Cut on Y axis only |
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

### 4.6 Utilities

#### `comment(comments: list[str])`

Append comment lines to the rpascript. Each comment is prefixed with `# `.

```python
driver.comment(["Setup complete", "Starting engrave pass"])
```

#### `inline(commands: list[str])`

Append raw rpascript commands directly. Intended for working around issues or
experimentation — a need for `inline()` suggests a new GlueScript method may
be needed.

```python
driver.inline(["AIR_ASSIST_ON", "LASER_OFF"])
```

A warning is logged during staging if `inline()` was used.

### 4.7 Staging and Execution

#### `stage_rpascript(gluescript: list[str] | None = None) -> list[str]`

Finalize the rpascript, expanding deferred variable references.

- When `gluescript` is `None`: finalizes the currently generated rpascript.
- When `gluescript` is provided: re-generates rpascript by processing each
  gluescript command line through the command registry, then finalizes.

**Returns:** `list[str]` — the finalized rpascript lines.

**Raises:** `RuntimeError` if `end_job()` has not been called.

```python
rpa_lines = driver.stage_rpascript()
print(f"Generated {len(rpa_lines)} rpascript lines")
```

#### `run_job(job: list[str], auto_checksum: bool = False)`

(Inherited from `RdDriver`) Queue a job for execution, composing head + job +
tail scripts, then sending the result to the controller.

```python
# Stage then execute
driver.stage_rpascript()
driver.run_job(driver.rpascript)
# Or use the combined approach via the TUI
```

---

## 5. TUI Usage (`/gluescript`)

The TUI provides interactive access to GlueScript via the `/gluescript` command.

### Available Subcommands

| Subcommand | Description |
|------------|-------------|
| `new` | Reset all gluescript data for a new job |
| `show` | Display current state summary (line counts, layer, position) |
| `declare_job ref=<point>` | Declare a new job with reference point |
| `end_job` | Finalize and complete the current job |
| `declare_layer mode=<mode>` | Declare a new layer |
| `layer <N> move_xy_to <x> <y>` | Add XY move to layer N |
| `layer <N> cut_xy_to <x> <y>` | Add XY cut to layer N |
| `layer <N> power <p>` | Add power action to layer N (IMAGE/DEPTHMAP only) |
| `layer <N> air_assist_on` | Enable air assist for layer N |
| `layer <N> air_assist_off` | Disable air assist for layer N |
| `layer <N> jog_xy_to <x> <y>` | Jog XY on layer N (live-only — executes immediately, never persisted) |
| `stage` | Generate rpascript from gluescript (re-stage if already staged) |
| `run` | Stage and execute the job |
| `save <path>` | Persist the current gluescript to a `.cglu` file |
| `load <path>` | Load a `.cglu` file, validate it, and stage it |
| `list` | Display high-level gluescript commands |
| `list_rpa` | Display generated low-level rpascript commands |

### Persistence: `save` and `load`

The current gluescript — the DSL lines built with `declare_job`, `declare_layer`,
and `layer` actions — can be persisted to disk and reloaded:

- **`/gluescript save <path>`** writes the gluescript to `<path>`. If the path's
  basename contains no `.`, the tool auto-appends `.cglu`; an explicit path
  (e.g. `myfile.custom`) is used as-is. Logs
  `GlueScript saved to <path> (N lines)`.
- **`/gluescript load <path>`** reads a `.cglu` file (same auto-append rule for
  the extension), validates it on a throwaway `GlueScript` instance, and — only
  if validation passes — applies it via `driver.stage_rpascript(lines)`. Logs
  `Loaded N gluescript lines from <path>, staged M rpascript lines`.

Load **auto-stages** the file: after a successful load the rpascript is ready,
but finalization still requires `end_job()` in the file (the job is only marked
complete when `end_job()` is replayed). Load errors fail loud without corrupting
live state and cover: file not found, permission denied, non-text file,
empty/blank-only file, "no stageable commands" (all-jog or comments-only files),
and a validation failure reported as `Load failed: ...`.

### Jog Commands Are Live-Only

All 16 jog commands (`jog_xy_to`, `jog_x_to`, `jog_y_to`, `jog_z_to`,
`jog_u_to`, `jog_xy_rel`, `jog_x_rel`, `jog_y_rel`, `jog_z_rel`, `jog_u_rel`,
plus the `jog_set_*` config setters: `jog_set_xy_speed`, `jog_set_z_speed`,
`jog_set_u_speed`, `jog_set_xy_rel`, `jog_set_z_rel`, `jog_set_u_rel`) are
live-only:

- **In the TUI**, `/gluescript layer <N> jog_xy_to <x> <y>` never appends to the
  gluescript. It immediately runs the returned rpascript lines against the
  controller when there is an active session (`driver.is_connected`). With no
  active session it warns and ignores the jog; if the background script runner
  is dead it warns `not sent — <reason>`.
- **In a `.cglu` file**, jog lines are ignored with a warning on load
  (`ignoring live-only jog line on load`) and are never used for position
  tracking — the re-stage loop skips all `LIVE_ONLY_COMMANDS`.
- **`jog_set_*` config setters** (speed / relative distance) are live-only too —
  they configure defaults for the live jog session, are never appended to the
  gluescript, and lines in a `.cglu` file are ignored with a warning on load.

### Bare Jog Commands

All 16 jog commands are also available in the TUI as **bare commands** (no
`/gluescript layer` wrapper), alongside `session`/`server`:

```
jog_xy_to 10 20            # Jog XY to absolute position (mm)
jog_z_to 5                 # Jog Z to absolute position (mm, max 2000)
jog_xy_rel                 # Jog XY relative, using configured defaults
jog_set_xy_speed 150       # Set XY jog speed (mm/s) — applies live
jog_set_xy_rel 25          # Set relative XY jog distance (mm) — applies live
```

- Bare jog commands are live-only: movement jogs run immediately against a
  connected controller; `jog_set_*` setters configure the live jog session and
  never produce gluescript lines.
- Typing a `jog` prefix brings up autocomplete with usage text; `/help` lists
  all 16 under "Jog commands (live-only)".
- Movement jogs without an active session warn-and-ignore; `jog_z_to` with
  `z > 2000` is refused (no rpascript lines are produced).

### Example Session

```
> /gluescript new
GlueScript: New job started.

> /gluescript declare_job ref=MACHINE
GlueScript: Job declared (ref=MACHINE).

> /gluescript declare_layer mode=VECTOR speed=300 min_power=15 max_power=60
GlueScript: Layer 1 declared (mode=VECTOR).

> /gluescript layer 0 move_xy_to 100 50
GlueScript: Layer 0 move_xy_to(100.000, 50.000)

> /gluescript layer 0 cut_xy_to 200 100
GlueScript: Layer 0 cut_xy_to(200.000, 100.000)

> /gluescript end_job
GlueScript: Job ended.

> /gluescript stage
GlueScript: Staged 28 rpascript lines (fresh).

> /gluescript list
   0: declare_job('My Job', 'MACHINE', [0.0, 0.0], 1, 1, 0.0, 0.0)
   1: declare_layer('Layer 1', '#000000', mode='VECTOR', overscan='NONE', speed=300.0, ...)
   2: move_xy_to(100.0, 50.0)
   3: cut_xy_to(200.0, 100.0)
   4: end_job()

> /gluescript list_rpa
   0: # Job: My Job
   1: # Generated by: GlueScript 1.0.0
   2: REF_POINT_ABSOLUTE
   3: SET_ABSOLUTE
   4: REF_POINT_SET
   5: START_JOB
   ...
```

### declare_job Parameters

```
/gluescript declare_job ref=MACHINE
/gluescript declare_job ref=ABSOLUTE columns=2 rows=3 xstep=100 ystep=100
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `ref` | `str` | Reference point: `MACHINE`, `ABSOLUTE`, `CURRENT`, `SET_POINT` |
| `columns` | `int` | Number of columns for job copies (default: 1) |
| `rows` | `int` | Number of rows for job copies (default: 1) |
| `xstep` | `float` | X step distance in mm (default: 0.0) |
| `ystep` | `float` | Y step distance in mm (default: 0.0) |

### declare_layer Parameters

```
/gluescript declare_layer mode=VECTOR speed=300 min_power=15 max_power=60 color=#000000
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `mode` | `str` | Layer mode (required): `VECTOR`, `RASTER`, `DITHER`, `IMAGE`, `DEPTHMAP`, `CUT`, `PRINT`, `CUT_SCAN`, `PRINT_SCAN` |
| `speed` | `float` | Layer speed in mm/s (default: 100.0) |
| `frequency` | `float` | Laser PWM frequency in KHz (default: 20.0) |
| `min_power` / `min_power_1` | `float` | Minimum power percent (default: 8.0) |
| `max_power` / `max_power_1` | `float` | Maximum power percent (default: 70.0) |
| `color` | `str` | Layer color as `#rrggbb` (default: `#000000`) |
| `overscan` | `str` | Overscan mode: `NONE`, `X`, `X_BI`, `Y`, `Y_BI`, `XY` (default: `NONE`) |
| `label` | `str` | Layer label (default: auto-generated) |

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

if abs(delta_x) <= 8.191 and abs(delta_y) <= 8.191:
    use NEAR form (MOVE_NEAR_XY / CUT_NEAR_XY)
else:
    use FAR form (MOVE_FAR_XY / CUT_FAR_XY)
```

For single-axis operations (`move_x_to`, `move_y_to`, `cut_x_to`, `cut_y_to`),
the check is performed on the single axis delta.

### Why Two Forms?

Near-form commands use relative offsets from the current position, which
requires fewer bytes on the wire and is more efficient for closely-spaced
operations. Far-form commands use absolute coordinates and can address any
point on the bed.

GlueScript selects the appropriate form automatically — you always use
absolute target coordinates in your code.

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

Re-staging (calling `stage_rpascript()` with a gluescript list) parses each
gluescript command line and replays it through the command registry:

- Standard commands are replayed via their corresponding methods
- Jog commands (`jog_*`, including `jog_set_*` config setters) are skipped —
  they are live-only and are never replayed or used for position tracking
  during re-staging
- `inline()` commands are passed through verbatim — they are stored as-is and
  not re-parsed
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
# Generated by: GlueScript 1.0.0
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
# Layer 1: Outline
LAYER_COLOR Layer:1 Color:#0000FF
OVERSCAN_OFF
LAYER_SPEED_LASER_1 Layer:1 Speed:120.000mm/S
LAYER_MIN_POWER_1 Layer:1 Power:20.0%
LAYER_MAX_POWER_1 Layer:1 Power:80.0%
LAYER_ATTRIBUTES Layer:1 0
LAYER_TOP_RIGHT Layer:1 X=150.000mm Y=150.000mm
LAYER_BOTTOM_LEFT Layer:1 X=50.000mm Y=50.000mm
SELECT_LAYER Layer:1
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

# Phase 3: Add operations to layer 0
driver.move_xy_to(layer=0, x=10.0, y=10.0)
driver.cut_xy_to(layer=0, x=210.0, y=10.0)
driver.cut_xy_to(layer=0, x=210.0, y=110.0)
driver.cut_xy_to(layer=0, x=10.0, y=110.0)
driver.cut_xy_to(layer=0, x=10.0, y=10.0)

# Phase 4: Complete the job
driver.end_job()

# Phase 5: Stage and execute
rpa = driver.stage_rpascript()
print(f"Generated {len(rpa)} rpascript lines")

# Phase 6: Connect and run (requires RdDriver session)
# driver.start(udp_host="192.168.1.100")
# driver.run_job(rpa)
```

---

## 11. Deferred Variable Expansion

GlueScript uses deferred variable references in rpascript lines. These are
expanded at stage time (when `stage_rpascript()` is called), not at generation
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
