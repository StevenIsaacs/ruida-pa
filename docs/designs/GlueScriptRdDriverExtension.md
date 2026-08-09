# Definitions
In the following:
- `rpascript` refers to the low level Ruida commands as defined in `ruida_protocol.py` and currently supported by `RdDriver.run`.
- `gluescript` refers to a high level script intended to be used by application adapters.
# AGENTS
The information contained in this document is intended to be used to write a plan for adding `gluescript` and `rpascript` generation methods in a new `GlueScript` class defined in a new file, `ruidadriver/rd_gluescript.py`. Therefore, this document is to be used to write a new guide named `docs/guides/gluescript-guide.md` and update the `docs/guides/integration-guide.md` document.

Implementation involves `RdDriver` being extended to include the new `GlueScript` methods. Use inheritance to do so. i.e. `def class RdDriver(GlueScript):`

NOTE: When updating the above mentioned documents include only the `gluescript` call information -- not what the call expands to.

In the following, the pattern **AGENT:** indicates something to be included in the implementation plan.
# Overview
This design document describes a new `GlueScript` class containing methods intended to be used by application adapters to avoid inline Ruida command specifics. This mitigates maintenance problems related to changing the Ruida command definitions in `ruida_protocol.py` as new discoveries are made. It also simplifies supporting other protocols such as GCODE at some time in the future.

Application adapters are strongly encouraged to use these new methods and avoid `inline()` Ruida commands (`rpascript`) as much as possible. Doing so makes it possible for application adapters to inherit new fixes and definitions by updating to a new release of RPA without having to change the adapters themselves.

The `gluescript` syntax is designed to be compatible with Python call syntax. Each gluescript command corresponds to a method name registered in an internal command registry (`dict[str, Callable]`). When re-staging, each line is tokenized using `shlex.split()`, arguments are converted via `ast.literal_eval()`, and the corresponding registered method is called. Inline `rpascript` commands are an exception and are simply appended to the `rpascript` being generated.

The concept of bounding boxes is supported of which there are three types: job, document and layer. A bounding box is the the farthest extent in which the laser head will move while processing and is represented as the top right and bottom left corners of a rectangle in which those moves are within. A bounding box is two dimensional and thus a corner is represented as an X and Y coordinate. Each layer has its own bounding box which is determined by the extent of all of the cut and move operations within the layer. The document bounding box is determined by the extend of all of the layer bounding boxes and the job bounding box is currently equal to the document bounding box.
## rpascript Structure
The final `rpascript` is comprised of the following sections:
- `job_attributes`
- `arrays`
- `layer_attributes` -- One per layer
- `layer_actions` -- One set of actions per layer
- `job_end` -- Job end marker and checksum
# Limitations
- Ruida controllers are capable of supporting multiple laser heads. This version supports only one head.
- Per pixel power settings are handled the same for both `IMAGE` and `DEPTHMAP` layer modes.
- Power settings below 8% are not allowed for a CO2 laser because the laser will not reliably fire at lower power settings.
- Power settings above 70% for a CO2 laser will issue a warning because CO2 laser tube life is reduced at higher power settings.
# The GlueScript Methods
 These methods support simultaneous on-the-fly generation of an `gluescript` and the low level `rpascript`. Properties and methods are provided which allow the application to retrieve and re-stage either of these forms.

To generate a new script the application adapter first calls the scripting methods to generate the complete job script. At the end of the job the adapter calls `stage_gluescript` to finalize the job script and prepare it to be run using the existing `RdDriver.run_job` method. Called with no `job` argument, `run_job()` runs the rpascript most recently staged by `stage_gluescript()`.

**AGENT:** All GlueScript methods documented here are exposed in the TUI and callable using RPC.
## Generating rpascript From gluescript
**AGENT:** Ruida mnemonics as defined in `ruida_protocol.py` are always UPPER CASE mnemonics. Ruida commands can occur anywhere within the script. However, doing so is discouraged. A need to insert Ruida commands indicates the possible need for a new method and corresponding function or to fix a bug. A warning should be issued after the script has been processed.

The general flow for a `GlueScript` defined `rpascript` generator method is:

```
validate parameters
IF parameters are valid:
	Append method name and parameters to the gluescript list
	Expand to the rpascript lines and append to the rpascript list
ELSE
	raise exception
```

Within a generator method script expansion involves appending a list of commands to the `rpascript`. The commands are a list of strings and support variable substitution. Substitutions use Python style formats and can reference variables internal to the `GlueScript` class using `self.<var>`. 

Internal `GlueScript` variable references (`self.<var>`) are deferred, meaning not expanded until the script is finalized. 

For example:
If the method prototype is:
`generator_method(param1, param2)`
and the expansion list is:
```
COMMAND_1 {param1}
COMMAND_2 {param2}
COMMAND_3 {self.var}
```
When `self.var` equals 1 the `gluescript` command `generator_method("a", "b")` expands to:
```
COMMAND_1 a
COMMAND_2 b
COMMAND_3 {self.var}
```
When the script is finalized the final form is:
```
COMMAND_1 a
COMMAND_2 b
COMMAND_3 1
```

Variable expansion occurs when the command string contains a `{` character. Deferred expansion (during staging) occurs when the command string contains `{self.` which means only `GlueScript` class variables can be deferred.
## Attributes
The generated scripts are available as attributes which simplifies retrieval or passing to `GlueScript` for encoding and execution. These are:
- `gluescript: list[str]` The generated or re-staged `gluescript`.
- `rpascript: list[str]` The generated `rpascript` which can be passed to `RdDriver` for encoding and execution.
Additional attributes are:
- `jog_xy_speed`: Current jog speed setting for the XY axes.
- `jog_z_speed`: Current jog speed setting for the Z axis.
- `jog_u_speed`: Current jog speed setting for the U axis.
## Reference Points
Jogging and job coordinates are relative to defined reference points. The reference points and their definitions are:
- `MACHINE` = Machine home position (0,0)
  All coordinates are relative to the machine home position.
- `ANCHOR` = An absolute coordinate anchor point
  All coordinates are relative to an absolute coordinate. The head is moved to the coordinate and then relative moves are used.
- `CURRENT` = The current position
  All coordinates are relative to the current head position. The user is expected to manually move the head before starting the job.
- `SET_POINT` = The user set point.
  All coordinates are relative to a user set point. The user set point is set at the machine. All moves are then relative moves. On a Ruida controller the set point is set using the `Origin` button.

**AGENT:** Valid reference points are defined in `ruida_protocol.py`. The valid reference point mnemonics are defined by the `RELT` table and the format specifier is `REL`.
## Jogging Moves
Jogging moves are to be used to move the laser when a job is not running. Movement jogs are **live-only**: in the TUI they are executed immediately against the controller and are never appended to the `gluescript`, and movement jog lines in a persisted file are ignored with a warning on load. The jog settings methods (`jog_set_*`) are live-only as well — they configure the live jog session (speeds and relative distances) without transmitting anything, never append to the `gluescript`, and their lines in a persisted file are ignored with a warning on load. The homing methods (`home`, `home_z`, `home_u` — machine homing actions) are live-only too: they run against a connected session, never append to the `gluescript`, and their lines in a persisted file are ignored with a warning on load. The on-disk GlueScript format is `.cglu` (the `.gs` extension is deliberately unused because it conflicts with Google Apps Script); `/gluescript save` and `/gluescript load` read and write this format.
### Homing methods
Home the machine axes. Homing methods are live-only like movement jogs — they run immediately against a connected session and never append to the `gluescript`:
#### home(...)
Home the X and Y axes (machine origin).

Prototype:
```
home() -> list[str] | None
```

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to and returns:
```
["HOME_XY"]
```
#### home_z(...)
Home the Z axis.

Prototype:
```
home_z() -> list[str] | None
```

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to and returns:
```
["HOME_Z"]
```
#### home_u(...)
Home the U axis (rotary).

Prototype:
```
home_u() -> list[str] | None
```

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to and returns:
```
["HOME_U"]
```
### Jog Settings methods
These methods set variables which are then used by the jog methods to generate the corresponding `rpascript` for jogging.
#### jog_set_xy_speed(...)
Set the jog speed in mm/S for both the X and Y axis. This setting is readable using the `jog_xy_speed` property.

Prototype:
```
jog_set_xy_speed(speed: float)
```

Parameters:
- `speed`: The speed in mm/S.
#### jog_set_z_speed(...)
Set the jog speed in mm/S for Z axis. This setting is readable using the `jog_z_speed` property.

Prototype:
```
jog_set_z_speed(speed: float)
```

Parameters:
- `speed`: The speed in mm/S.
#### jog_set_u_speed(...)
Set the jog speed in mm/S for U axis. This setting is readable using the `jog_u_speed` property.

Prototype:
```
jog_set_u_speed(speed: float)
```

Parameters:
- `speed`: The speed in mm/S.
### Jogging `rpascript` Generators
On `RdDriver`, these methods generate the rpascript lines AND send them immediately (single call); on a standalone `GlueScript` instance they remain pure generators that return the lines. Do not pass the returned lines to `RdDriver.run()` — they are already sent.
#### jog_xy_to(...)
Move the laser head to an absolute X,Y coordinate relative to the machine home.

Prototype:
```
jog_xy_to(x: float, y: float) -> list[str] | None
```

Parameters:
- `x`: The X coordinate relative to the machine home.
- `y`: The Y coordinate relative to the machine home.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to and returns:
```
SPEED_LASER_1 {self.jog_xy_speed}
JOG_XY Rel:MACHINE X={x}mm Y={y}mm
```
#### jog_x_to(...)
Move the laser head to an absolute X coordinate relative to the machine home.

Prototype:
```
jog_x_to(x: float) -> list[str] | None
```
Parameters:
- `x`: The X coordinate relative to the machine home.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_xy_speed}
JOG_X Rel:MACHINE X={x}mm
```
#### jog_y_to(...)
Move the laser head to an absolute Y coordinate relative to the machine home.

Prototype:
```
jog_y_to(y: float) -> list[str] | None
```

Parameters:
- `y`: The Y coordinate relative to the machine home.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_xy_speed}
JOG_Y Rel:MACHINE Y={y}mm
```
#### jog_z_to(...)
Move the laser head to an absolute Z coordinate relative to the machine home.

**AGENT:** The power on home position is 3000.000mm. This may not be true for all lasers so this may need a future configuration option. Because of this, a Z home is recommended to "learn" the correct home coordinate which sets the Z home to 0. So, a general rule can be if the current Z coordinate is greater than 2000 then do not move. Instead, issue a warning saying a Z home is required.

Prototype:
```
jog_z_to(z: float) -> list[str] | None
```

Parameters:
- `z`: The Z coordinate relative to the machine home.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_z_speed}
JOG_Z Rel:MACHINE Z={z}mm
```
#### jog_u_to(...)
Move the laser head to an absolute U coordinate relative to the machine home.

Prototype:
```
jog_u_to(u: float) -> list[str] | None
```

Parameters:
- `u`: The U coordinate relative to the machine home.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).
#### jog_set_xy_rel(...)
Set the relative jog distance for both the X and Y axis. These default to 10.000mm.

Prototype:
```
jog_set_xy_rel(delta: float) -> None
```

Parameters:
- `delta`: The distance relative to the current position.
#### jog_set_z_rel(...)
Set the relative jog distance for Z axis. This defaults to 10.000mm.

Prototype:
```
jog_set_z_rel(delta: float) -> None
```

Parameters:
- `delta`: The distance relative to the current position.
#### jog_set_u_rel(...)
Set the relative jog distance for U axis. This defaults to 10.000.mm.

Prototype:
```
jog_set_u_rel(delta: float) -> None
```

Parameters:
- `delta`: The distance relative to the current position.
#### jog_xy_rel(...)
Move the laser head to an absolute X,Y coordinate relative to the current position.

Prototype:
```
jog_xy_rel(x: float=None, y: float=None) -> list[str] | None
```

Parameters:
- `x`: The X coordinate relative to the current position. If this is `None` then the configured jog distance (`x_rel`) is used.
- `y`: The Y coordinate relative to the current position. If this is `None` then the configured jog distance (`y_rel`) is used.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_xy_speed}
JOG_XY Rel:CURRENT X={x}mm Y={y}mm
```
#### jog_x_rel(...)
Move the laser head to an absolute X coordinate relative to the current position.

Prototype:
```
jog_x_rel(x: float=None) -> list[str] | None
```
Parameters:
- `x`: The X coordinate relative to the current position. If this is `None` then the configured jog distance (`x_rel`) is used.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_xy_speed}
JOG_X Rel:CURRENT X={x}mm
```
#### jog_y_rel(...)
Move the laser head to an absolute Y coordinate relative to the current position.

Prototype:
```
jog_y_rel(y: float=None) -> list[str] | None
```

Parameters:
- `y`: The Y coordinate relative to the job current position. If this is `None` then the configured jog distance (`y_rel`) is used.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_xy_speed}
JOG_Y Rel:CURRENT Y={y}mm
```
#### jog_z_rel(...)
Move the laser head to an absolute Z coordinate relative to the current position. A positive distance moves the table down.

Prototype:
```
jog_z_rel(z: float=None) -> list[str] | None
```

Parameters:
- `z`: The Z coordinate relative to the current position. If this is `None` then the configured jog distance (`z_rel`) is used.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_z_speed}
JOG_Z Rel:CURRENT Z={z}mm
```
#### jog_u_rel(...)
Move the laser head to an absolute U coordinate relative to the current position.

Prototype:
```
jog_u_rel(u: float = None) -> list[str] | None
```

Parameters:
- `u`: The U coordinate relative to the current position. If this is `None` then the configured jog distance (`u_rel`) is used.

Returns: the sent rpascript lines, or `None` if nothing was sent (e.g. no active session).

Expands to:
```
SPEED_LASER_1 {self.jog_xy_speed}
JOG_U Rel:CURRENT U={u}mm
```
## Script Management Methods
This section defines the methods provided for retrieving and running generated lists.
### **stage_gluescript(...)**
Use this method to finalize a generated `rpascript` or to re-stage a previously generated `gluescript`. Deferred variable expansion occurs at this time.

Method:
```
stage_gluescript(gluescript: list[str]=None) -> str
```
Finalizes a freshly generated script or re-stages an existing script.

Re-staging a `gluescript` script generates and stages a new `rpascript`.

The resulting `rpascript` can then be passed to `RdDriver.run` for encoding and transmission to the controller.

NOTE: Only a `gluescript` can be re-staged. To run the `rpascript` form use the existing `RdDriver.run` method.

Part of the finalization process involves a second pass on the `rpascript` to expand the deferred variables (i.e. `{self.<var>}` references). This must be a separate pass because some variable values are not known until all of the `gluescript` has been processed. This is particularly true for the job and layer bounding boxes. This second pass also inserts the layers section with one set of layer definitions for each layer.

Parameters:
	`gluescript` = When equal to None the generated script is finalized. Otherwise re-generate and finalize the `rpascript`.
## Script Generator Methods
This section defines the essential methods and corresponding `gluescript` commands. The `gluescript` command parameters mirror those of the underlying generator   method and in the following are shown only as `<function>(...)`.

Each of these methods append themselves to the `self.gluescript` attribute and the corresponding generated script to the `self.rpascript` attribute. The jog methods (`jog_*`, including the `jog_set_*` config setters) and the homing methods (`home`, `home_z`, `home_u`) are the exception — they are live-only and never append to the `gluescript`.

NOTE: When re-staging a `gluescript`, calling a `gluescript` command involves tokenizing the line with `shlex.split()`, converting arguments with `ast.literal_eval()`, and looking up the method name in an internal command registry (`_command_registry` mapping method name strings to bound methods). This avoids the security and fragility issues of `exec()`.

Generating scripts requires calls in this order:
```
new_gluescript(...)
declare_job(...)
for each layer:
	declare_layer(...) # 1 or more
stage_gluescript(...)
```

**AGENT:** Use internal classes to generate `rpascript` segments which `stage_gluescript` can then be update and assemble into a complete `rpascript` as part of the staging process.
### new_gluescript(...)
Initializes new `self.gluescript` and `self.rpascript` lists. This also initializes the document and job bounding box variables.

NOTE: At some point in the future an `gluescript` can contain more than one job. Currently, only one job is supported.
### comment(...)
Append a list of `rpascript` comment lines. NOTE: This behaves virtually the same as the `inline` method but is provided for readability and to clarify intent.

Prototype:
```
comment(comments: list[str])
```

Parameters:
- `comments` = The list of commands to append.

For example:
```
comment([
	"# This is a comment.",
	"# This is to expose a variable: {var}",
	])
```

### inline(...)
Append a list of `rpascript` commands. This appends the list of commands to the `rpascript` being generated. The lines are inserted at the call point in the assembled rpascript. It is highly recommended this method not be used for appending `rpascript` commands except in cases where it is necessary to work around a problem or for experimentation.

Prototype:
```
inline(commands: list[str])
```

Parameters:
- `commands` = The list of commands to append.

For example:
```
inline([
	"# Example rpascript command to move to absolute coordinate.",
	"MOVE_FAR_XY {x} {y}",
	])
```

### Flow Control

Runner-directive methods. The emitted `delay`/`wait` lines are executed inline
by the runner thread — they are never encoded or sent to the controller.
Unlike jog/home live commands, they are part of a saved job and are replayed
from a persisted gluescript. Position-aware placement matches `inline()`:
before any layer is declared the line lands right after the job header; inside
a declared layer it lands in that layer's action block at the call position;
after `end_job()` it lands just before the closing `END_JOB` line. Calls
before `declare_job()` are discarded by the job reset. The rpascript
interpreter matches the mnemonics case-sensitively in lowercase; uppercase
`DELAY`/`WAIT` lines are dropped with an "Unknown command mnemonic" warning.

#### delay(...)
Append a pause between rpascript commands at the call point.

Prototype:
```
delay(time: str | int | float) -> None
```

Parameters:
- `time` = Seconds as a number (e.g. `30`, `0.5`) or a unit-suffixed string (e.g. `'500ms'`, `'30s'`). Numeric seconds are normalized to a unit-suffixed token (`30` → `30s`, `0.5` → `0.5s`); strings must carry a unit suffix and are compacted to a whitespace-free token (`'1.5 ms'` → `1.5ms`). Invalid values log a warning and no-op.

Returns: `None`.

Emits a runner-directive line, e.g. `delay 30s`.

#### wait(...)
Wait for a machine status bit at the call point.

Prototype:
```
wait(status: str, to: str | int | float | None = None) -> None
```

Parameters:
- `status` = A `MACHINE_STATUS_*` name passed through verbatim; a leading `!` waits for the full active→inactive lifecycle (the status must first become active, then clear). The name is validated at run time by the runner, not at authoring time.
- `to` = Optional timeout in seconds (number) or as a unit-suffixed string (e.g. `'30s'`); normalized like `delay()`'s time.

Returns: `None`.

Emits a runner-directive line, e.g. `wait MACHINE_STATUS_MOVING` or `wait !MACHINE_STATUS_JOB_RUNNING to=30s`.

### declare_job(...)
Initializes new `gluescript` and `rpascript` lists and adds the job declaration.

Prototype:
```
declare_job(label: str, ref_point: str="MACHINE", abs_xy: list[float]=[0.0, 0.0], columns: int=1, rows: int=1, xstep: float=0.0, ystep: float=0.0)
```

Parameters:
- `label` = the label to identify the job with.
- `ref_point` = The reference point the job is relative to.
- `abs_xy` = Required only for "ABSOLUTE". This is the absolute relative XY coordinate.
- `columns` = The number of columns for job copies.
- `rows` = The number of rows for job copies.
- `xstep` = The X step distance in mm between job copies.
- `ystep` = The Y step distance in mm between job copies.

The `rel` parameter expansion uses a dictionary named `_ref_points` to get the expansion lines for a `rel` type. This dictionary is defined as:
```
self._ref_points = {
	"MACHINE": [
		"REF_POINT_ABSOLUTE",
		"SET_ABSOLUTE",
		],
	"ABSOLUTE": [
		"JOG_XY Rel=MACHINE X={abs_xy[0]} Y={abs_xy[1]}",
		"REF_POINT_CURRENT",
		],
	"CURRENT": [
		"REF_POINT_CURRENT",
		],
	"SET_POINT": [
		"REF_POINT_ANCHOR",
		],
}
```

Expands to:
```
# Job: {label}
# Generated by: GlueScript {self._version}
{self._ref_points[rel]}
REF_POINT_SET
START_JOB
FEED_REPEAT 0 0
SET_FEED_AUTO_PAUSE State:OFF
# Job settings
JOB_TOP_RIGHT X={self.doc_tr_x}mm Y={self.doc_tr_y}mm
JOB_BOTTOM_LEFT X={self.doc_bl_x}mm Y={self.doc_bl_y}mm
DOCUMENT_TOP_RIGHT X={self.doc_tr_x}mm Y={self.doc_tr_y}mm
DOCUMENT_BOTTOM_LEFT X={self.doc_bl_x}mm Y={self.doc_bl_y}mm
JOB_COPIES Columns={columns} Rows={rows} XStep={xstep}mm YStep={ystep}mm
```

Note: These lines form the **job header** section of the final rpascript,
emitted first by `stage_gluescript()` before any layer attributes or actions.

### declare_layer(...)
Initializes a new layer. A layer declaration is expected to be followed by layer actions.

Prototype:
```
declare_layer(label: str, color: str, mode: str="VECTOR", overscan: str="NONE", speed:float=100.00, frequency: float=20.000, min_power_1: float=8.0, max_power_1: float=70.0)
```
Parameters:
- `label` = The label for the layer.
- `color` = The color of the layer formatted as `#rrggbb`. This color is displayed on the Ruida controller display.
- `mode` = The layer mode. This controls how moves and cuts are handled within the layer. Valid modes are:
	- `VECTOR`: Line cuts or engraves. This ignores the `overscan` mode parameter and instead uses `none`.
	- `RASTER`: A raster filled object.
	- `DITHER`: A dithered image. This requires a single power setting for all dots.
	- `IMAGE`: A bit mapped image. This supports a different power setting per pixel.
	- `DEPTHMAP`: Also a bit mapped image but is used to vary the depth of the laser cut. Currently, this behaves the same as `image`.
- `overscan`: This is the layer overscan mode. Valid modes are:
	- `NONE`: Expands to `OVERSCAN_OFF`. This is the mode used by `vector` layers.
	- `X`: Expands to `OVERSCAN_H_UNI`. Overscan on the X axis in one direction.
	- `X_BI`: Expands to `OVERSCAN_H_BI`. Overscan on the X axis in both directions.
	- `Y`: Expands to `OVERSCAN_V_UNI`. Overscan on the Y axis in one direction.
	- `Y_BI`: Expands to `OVERSCAN_V_BI`. Overscan on the Y axis in both directions.
	- `XY`: Both the X and Y axis (diagonal). NOTE This also expands to `OVERSCAN_OFF` because the Ruida controller does not support diagonal overscan.
- `speed`: Layer speed in mm/S.
- `frequency`: Laser PWM frequency in KHz.
- `min_power_1`: Minimum layer power percent for laser head 1.
- `max_power_1`: Maximum layer power percent for laser head 1.

The `mode` parameter expansion uses a dictionary named `_layer_modes` to override the `overscan` parameter. An entry that is a non empty string is an override. This dictionary is defined as:
```
self._layer_modes = {
	"VECTOR": "NONE",
	"RASTER": "",
	"DITHER": "",
	"IMAGE": "",
	"DEPTHMAP": "NONE",
}
```

The `overscan` parameter expansion uses a dictionary named `_overscan_modes` to get the expansion lines for a `overscan` mode. This may be overridden by the `mode` parameter. This dictionary is defined as:
```
self._overscan_modes = {
	"NONE": [
		"OVERSCAN_OFF",
		],
	"X": [
		"OVERSCAN_H_UNI",
		],
	"X_BI": [
		"OVERSCAN_H_BI",
		],
	"Y": [
		"OVERSCAN_V_UNI",
		],
	"Y_BI": [
		"OVERSCAN_V_BI",
		],
	"XY": [
		"# Diagonal overscan is not supported.",
		"OVERSCAN_OFF"
		],
}
```

Expands to:
```
# Layer {self._layer}: {label}
LAYER_COLOR Layer:{self._layer} Color:{color}
{self._overscan_modes[overscan]}
LAYER_SPEED_LASER_1 Layer:{self._layer} Speed:{speed}mm/S
LAYER_MIN_POWER_1 Layer:{self._layer} Power:{min_power_1}%
LAYER_MAX_POWER_1 Layer:{self._layer} Power:{max_power_1}%
LAYER_ATTRIBUTES Layer:{self._layer} 0
# Per-layer bounding boxes are emitted as concrete values at the next
# declare_layer() call or end_job(), only for layers that have content.
```

Note: The '#' in the color value is escaped to '\\#' on emission (e.g. Color:\\#0000FF) so the rpascript interpreter's inline-comment stripping does not eat it; parse_value un-escapes it on read.

Note: These lines form the **layer attributes** for one layer, stored in
`_layer_attributes`. All layers' attribute blocks are emitted together
in the second section of the final rpascript (before any `SELECT_LAYER`
commands), sorted by layer number.

### end_job(...)
Ends the job and prepares the job to be staged using `stage_gluescript`. After this method has been called all accumulated layer actions are sent and the job is ready to be staged using `stage_gluescript` which MUST be called in order to run the job.

Prototype:
```
end_job()
```

Note: `end_job()` also emits the final layer's bounding box with concrete
values (`LAYER_TOP_RIGHT` and `LAYER_BOTTOM_LEFT`), provided the layer
has any move/cut content. Empty layers skip bbox emission.

### stage_gluescript(...)
Assembles the final rpascript from structured storage in three sections
and expands deferred variables. The generated rpascript has the following
structure:

1. **Job header** — reference point setup, START_JOB, JOB_TOP_RIGHT/JOB_BOTTOM_LEFT,
   DOCUMENT_TOP_RIGHT/DOCUMENT_BOTTOM_LEFT, JOB_COPIES
2. **Layer attributes** — all layers' LAYER_COLOR, LAYER_SPEED, LAYER_POWER,
   LAYER_ATTRIBUTES, and bounding box lines, sorted by layer number
3. **Layer actions** — each layer's actions preceded by `SELECT_LAYER Layer:{n}`,
   sorted by layer number
4. **END_JOB** — job terminator

This method must be called before the job can be executed. When called
with a gluescript list argument, the gluescript is replayed through the
command registry to regenerate the structured storage before assembly.

Prototype:
```
stage_gluescript(gluescript: list[str] | None = None) -> str
```

Parameters:
- `gluescript`: Optional gluescript to re-stage. When None (default),
  uses the current structured state.

Returns:
- The SHA-256 signature (hex) of the staged gluescript transcript;
  failures raise `RuntimeError`.

### Layer Actions
Once a layer has been declared a series of actions for the layer are expected to
follow. During generation, layer actions are stored in an internal dict keyed by
layer number. During `stage_gluescript()`, these are assembled into the final
rpascript with `SELECT_LAYER Layer:{n}` prefixing each layer's action block:

```
SELECT_LAYER Layer:1
MOVE_FAR_XY X=50.000mm Y=50.000mm
CUT_FAR_XY X=150.000mm Y=50.000mm
...
SELECT_LAYER Layer:2
MOVE_NEAR_XY X=10.000mm Y=10.000mm
...
```

The `SELECT_LAYER` command tells the controller which layer the subsequent
actions belong to. Layer actions can include moves, cuts, and power
settings.

The following are the supported layer actions. Note that which actions can be used for a layer depends upon the layer `mode`.
#### Moves and Cuts
The move and cut actions on the X and/or Y axis use the job reference point and distance moved from current position to determine which form of move to use. All moves and cuts represent a line beginning at the current position and ending at the specified coordinate (**AGENT:** This implies that the coordinates are maintained internal to the class). With moves the laser is off and with cuts the laser is on. The forms are:
**Far Form:**
Used for moves and cuts having a distance greater than what can be represented using a near form.
**Near Form:** 
Used for moves and cuts having a distance less than what can be represented using a signed 14 bit integer / 1000 (in the range -8.192mm to 8.191mm).

**AGENT:** Cut coordinates are used to determine the current layer bounding box as well as the overall job bounding box. Add code to test when a coordinate is beyond the limit of either bounding box and expand the bounding box as needed.
**AGENT:** Because the number of layer actions can quickly become unwieldy for large projects add a means, when RDP is used, to accumulate all actions and send them all at one time to the TUI RDP server. Sending the accumulated actions should be triggered when starting a new layer (i.e. `declare_layer`) or ending the job (i.e. `end_job`). 
#### power(...)
Set the laser power to a percentage. This is a valid layer action only if the layer mode is `IMAGE` or `DEPTHMAP`.

Prototype:
```
power(percent: float=None)
```

Parameters:
- `percent`: The percentage of power to set.
Expands to:
```
IMD_POWER_1 Power:{percent:.1f}%
```
#### energy(...)
**AGENT:** Do not implement this method. It still requires further definition.

Set the laser power to a percentage based upon the amount of energy per dot. Energy is a function of frequency, duty cycle, speed and power settings. For a given speed, energy increases as the power setting increases. This is a valid layer action only if the layer mode is `IMAGE` or `DEPTHMAP`.
##### Formulas
1. Average Power

Average Power (Watts) = Maximum Power (Watts) x Power Percentage (as decimal) x Duty Cycle (as decimal)

2. Line Energy Density (Energy per Unit Length)

Energy per Millimeter (J/mm) = Average Power (Watts) / Head Speed (mm/s)

**Full expanded version:**  
Energy per Millimeter (J/mm) = [Maximum Power x Power Percentage x Duty Cycle] / Head Speed (mm/s)

3. Energy Per Pulse

Energy per Pulse (Joules) = Average Power (Watts) / PWM Frequency (Hz)

**Full expanded version:**  
Energy per Pulse (Joules) = [Maximum Power x Power Percentage x Duty Cycle] / PWM Frequency (Hz)

#### air_assist_on(...)
Enable air assist for the current layer.

Prototype:
```
air_assist_on()
```

Expands to:
```
AIR_ASSIST_ON
```

#### air_assist_off(...)
Disable air assist for the current layer.

Prototype:
```
air_assist_off()
```

Expands to:
```
AIR_ASSIST_OFF
```

#### move_xy_to(...)
Move the laser head to an absolute X,Y coordinate relative to the job reference point.

Prototype:
```
move_xy_to(x: float, y: float)
```
Parameters:
- `x`: The X coordinate relative to the job reference point.
- `y`: The Y coordinate relative to the job reference point.

Far form expands to:
```
MOVE_FAR_XY X={x:.3f}mm Y={y:.3f}mm
```
Near form expands to:
```
MOVE_NEAR_XY X={x:.3f}mm Y={y:.3f}mm
```
#### move_x_to(...)
Move the laser head to an absolute X coordinate relative to the job reference point.

Prototype:
```
move_x_to(x: float)
```
Parameters:
- `x`: The X coordinate relative to the job reference point.

Far form expands to:
```
MOVE_FAR_X X={x:.3f}mm
```
Near form expands to:
```
MOVE_NEAR_X X={x:.3f}mm
```
#### move_y_to(...)
Move the laser head to an absolute Y coordinate relative to the job reference point.

Prototype:
```
move_y_to(y: float)
```
Parameters:
- `y`: The Y coordinate relative to the job reference point.

Far form expands to:
```
MOVE_FAR_Y Y={y:.3f}mm
```
Near form expands to:
```
MOVE_NEAR_Y Y={y:.3f}mm
```
#### move_z_to(...)
Move the laser bed to an absolute Z coordinate relative to the job reference point.

Prototype:
```
move_z_to(z: float)
```
Parameters:
- `z`: The Z coordinate relative to the job reference point.
Note: Z-axis moves are not implemented in the initial release. Calling `move_z_to()` raises `NotImplementedError("Z-axis moves not yet implemented")`.

#### move_u_to(...)
Move the rotation device to an absolute U coordinate relative to the job reference point.

Prototype:
```
move_u_to(u: float)
```
Parameters:
- `u`: The U coordinate relative to the job reference point.

Note: U-axis moves are not implemented in the initial release. Calling `move_u_to()` raises `NotImplementedError("U-axis moves not yet implemented")`.
#### cut_xy_to(...)
With the laser turned on move the laser head to an absolute X,Y coordinate relative to the job reference point.

Prototype:
```
cut_xy_to(x: float, y: float)
```
Parameters:
- `x`: The X coordinate relative to the job reference point.
- `y`: The Y coordinate relative to the job reference point.

Far form expands to:
```
CUT_FAR_XY X={x:.3f}mm Y={y:.3f}mm
```
Near form expands to:
```
CUT_NEAR_XY X={x:.3f}mm Y={y:.3f}mm
```
#### cut_x_to(...)
With the laser turned on move the laser head to an absolute X coordinate relative to the job reference point.

Prototype:
```
cut_x_to(x: float)
```
Parameters:
- `x`: The X coordinate relative to the job reference point.

Far form expands to:
```
CUT_FAR_X X={x:.3f}mm
```
Near form expands to:
```
CUT_NEAR_X X={x:.3f}mm
```
#### cut_y_to(...)
With the laser turned on move the laser head to an absolute Y coordinate relative to the job reference point.

Prototype:
```
cut_y_to(y: float)
```
Parameters:
- `y`: The Y coordinate relative to the job reference point.

Far form expands to:
```
CUT_FAR_Y Y={y:.3f}mm
```
Near form expands to:
```
CUT_NEAR_Y Y={y:.3f}mm
```
#### cut_z_to(...)
With the laser turned on move the laser head to an absolute Z coordinate relative to the job reference point.

Prototype:
```
cut_z_to(z: float)
```
Parameters:
- `z`: The Z coordinate relative to the job reference point.
Note: Z-axis cuts are not implemented in the initial release. Calling `cut_z_to()` raises `NotImplementedError("Z-axis cuts not yet implemented")`.

#### cut_u_to(...)
With the laser turned on move the laser head to an absolute U coordinate relative to the job reference point.

Prototype:
```
cut_u_to(u: float)
```
Parameters:
- `u`: The U coordinate relative to the job reference point.

Note: U-axis cuts are not implemented in the initial release. Calling `cut_u_to()` raises `NotImplementedError("U-axis cuts not yet implemented")`.
