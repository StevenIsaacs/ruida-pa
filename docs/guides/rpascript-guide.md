# rpascript Guide

**Format:** rpascript (`.rds`)  
**Consumed by:** `RdDriver.run(script: list[str])`  
**Status:** As-built (describes current implementation)

---

## 1. Script Format

A script is a list of strings passed to `RdDriver.run()`. Each line is one
command, comment, or directive. Lines are processed in order by the background
script runner.

### Comments

```rds
# This is an inline comment.
"""This is a block comment (triple-quote) that can span
multiple lines."""
```

- Inline comments start with `#` outside quoted strings.
- Escape `\#` for a literal `#` (e.g., color values).
- Block comments use `"""` delimiters and can span multiple lines.
- Comments and blank lines are silently skipped during execution.

### Session Meta-Commands

```rds
session start udp=192.168.1.100
session start udp=192.168.1.100 usb=ttyUSB0 to=10s
session end
```

- `session start` — Opens a transport connection. Requires at least
  `udp=<IP>` or `usb=<device>`. The optional `to=<timeout>` sets the
  connection timeout (e.g., `10s`, `5000ms`; default 5000ms).
- `session end` — Closes the active session.
- These are processed by `RdDriver.start()` / `RdDriver.stop()`, not
  sent to the controller as commands.

### Server Meta-Commands

```rds
server start host=127.0.0.1
server start host=0.0.0.0 port=18812 cert=server.pem key=server.key token=mytoken
server stop
```

- `server start` — Starts the RPyC remote control server. If `host` is
  `localhost` or `127.0.0.1`, TLS and authentication are disabled.
- `server stop` — Stops the running RPyC server.
- Parameters from previous `server start` are reused when omitted.
- These are processed by `start_rpyc_server()` / `server.stop()`, not
  sent to the controller as commands.

### Jog & Home Commands

The 16 jog commands and 3 homing commands (`home`, `home_z`, `home_u`) are bare
commands (no `/` prefix) processed by the TUI before rpascript parsing. They
are **live-only** — they act on the live session and are never persisted to, or
replayed from, `.cglu` files:

```rds
home                            # Home X and Y axes (machine origin)
home_z                          # Home Z axis
home_u                          # Home U axis (rotary)
jog_xy_to 10 20             # Jog XY to absolute position (mm)
jog_x_to 50                 # Jog X to absolute position (mm)
jog_y_to 50                 # Jog Y to absolute position (mm)
jog_z_to 5                  # Jog Z to absolute position (mm, max 2000)
jog_u_to 30                 # Jog U to absolute position (mm)
jog_xy_rel                  # Jog XY relative ([x] [y] optional — uses configured defaults)
jog_x_rel 5                 # Jog X relative ([x] optional)
jog_y_rel 5                 # Jog Y relative ([y] optional)
jog_z_rel 2                 # Jog Z relative ([z] optional)
jog_u_rel 2                 # Jog U relative ([u] optional)
jog_set_xy_speed 150        # Set XY jog speed (mm/s)
jog_set_z_speed 50          # Set Z jog speed (mm/s)
jog_set_u_speed 50          # Set U jog speed (mm/s)
jog_set_xy_rel 25           # Set relative XY jog distance (mm)
jog_set_z_rel 10            # Set relative Z jog distance (mm)
jog_set_u_rel 10            # Set relative U jog distance (mm)
```

- **Movement jogs** (`jog_*_to`, `jog_*_rel`) require a connected session and
  run immediately against the controller; with no active session they
  warn-and-ignore.
- **Setters** (`jog_set_*`) configure the live jog session (speeds and relative
  defaults) and never produce rpascript lines.
- **Homing commands** (`home`, `home_z`, `home_u`) require a connected session
  and run immediately (expanding to `HOME_XY`/`HOME_Z`/`HOME_U`); they never
  produce gluescript lines.
- These commands are not rpascript — the rpascript parser is not involved.

### new_packet Directive

```rds
new_packet
```

Marks a packet boundary. Used when the script is replayed through
`rpa-script` to generate tshark-format binary output. Does nothing
during live execution — commands are batched automatically.

### Regular Commands

```rds
MOVE MOVE_FAR_XY X=100mm Y=200mm
CORE NOP
GET_SETTING MEM_CARD_ID
END_JOB
START_JOB
```

Each line is parsed as:

```
[type_group] [CMD] <mnemonic> [param=value ...] [= expected_reply]
```

- **Optional type group**: One of `CORE`, `MOVE`, `LASER`, `CONFIG`,
  `QUERY`, `ENGRAVE`, `CUT`, `FILE`, `SYSTEM` (or suffixed `_CMD`).
- **Optional `CMD` keyword**: e.g., `CORE CMD NOP`.
- **Mnemonic**: The command name from the protocol command table.
- **Parameters**: Space-separated `KEY=VALUE` pairs.
- **Expected reply** (optional): `= value` — used for synthetic reply
  generation in tshark output. Ignored during live execution.

---

## 2. Command Reference (by Function)

### 2.1 Movement & Positioning

| Mnemonic              | Parameters              | Description                           |
| --------------------- | ----------------------- | ------------------------------------- |
| `SET_ABSOLUTE`        | *(none)*                | Switch to absolute coordinate mode    |
| `MOVE_FAR_XY`         | `X=mm Y=mm`             | Rapid move to far X, Y           |
| `MOVE_NEAR_XY`         | `nearX=mm nearY=mm`        | Rapid move near from current      |
| `MOVE_NEAR_X`          | `nearX=mm`               | Rapid move near on X axis         |
| `MOVE_NEAR_Y`          | `nearY=mm`               | Rapid move near on Y axis         |
| `CUT_FAR_XY`          | `X=mm Y=mm`             | Cut (engrave) move to far X, Y   |
| `CUT_NEAR_XY`          | `nearX=mm nearY=mm`        | Cut move near from current        |
| `CUT_NEAR_X`           | `nearX=mm`               | Cut move near on X axis           |
| `CUT_NEAR_Y`           | `nearY=mm`               | Cut move near on Y axis           |
| `HOME_XY`             | *(none)*                | Home X and Y axes                     |
| `HOME_Z`              | *(none)*                | Home Z axis                           |
| `HOME_U`              | *(none)*                | Home U axis (rotary)                  |
| `REF_POINT_ABSOLUTE`  | *(none)*                | Set reference point (origin)          |
| `REF_POINT_ANCHOR`    | *(none)*                | Set reference point (alternative)     |
| `REF_POINT_SET`      | *(none)*                | Set reference point for coordinate system |
| `FOCUS_Z`             | *(none)*                | Auto-focus Z axis                     |
| `MOVE_FAR_X`         | `X=mm`                  | Single-axis X move                    |
| `MOVE_FAR_Y`         | `Y=mm`                  | Single-axis Y move                    |
| `MOVE_FAR_Z`         | `Z=mm`                  | Single-axis Z move                    |
| `MOVE_FAR_U`         | `U=mm`                  | Single-axis U move (rotary)           |
| `JOG_XY`         | `Rel={0-3} X=mm Y=mm` | Jog relative to reference point       |

### 2.2 Laser Power

| Mnemonic           | Parameters      | Description                          |
| ------------------ | --------------- | ------------------------------------ |
| `IMD_POWER_1`      | `Power=%`       | Immediate power level for laser 1    |
| `END_POWER_1`      | `Power=%`       | End-of-move power level for laser 1  |
| `IMD_POWER_2`      | `Power=%`       | Immediate power level for laser 2    |
| `END_POWER_2`      | `Power=%`       | End-of-move power level for laser 2  |
| `MIN_POWER_1`      | `Power=%`       | Minimum power for laser 1            |
| `MAX_POWER_1`      | `Power=%`       | Maximum power for laser 1            |
| `MIN_POWER_2`      | `Power=%`       | Minimum power for laser 2            |
| `MAX_POWER_2`      | `Power=%`       | Maximum power for laser 2            |
| `MIN_POWER_1_LAYER` | `Layer={n} Power=%` | Min power for a specific layer/part |
| `MAX_POWER_1_LAYER` | `Layer={n} Power=%` | Max power for a specific layer/part |
| `MIN_POWER_2_LAYER` | `Layer={n} Power=%` | Min power for laser 2 per layer |
| `MAX_POWER_2_LAYER` | `Layer={n} Power=%` | Max power for laser 2 per layer |
| `THROUGH_POWER_1`  | `Power=%`       | Through (pierce) power for laser 1   |
| `LASER_INTERVAL`   | `{:.3f}mS`      | Laser pulse interval                 |
| `LASER_ON_DELAY`   | `{:.3f}mS`      | Delay before laser fires             |
| `LASER_OFF_DELAY`  | `{:.3f}mS`      | Delay after laser stops              |
| `ADD_DELAY`        | `{:.3f}mS`      | Additional delay per segment         |
| `FREQUENCY_LAYER`   | `Laser={n} Layer={n} Freq=KHz` | Frequency for a specific layer |

### 2.3 Speed

| Mnemonic              | Parameters             | Description                      |
| --------------------- | ---------------------- | -------------------------------- |
| `SPEED_LASER_1`       | `Speed=mm/S`           | Cutting/engraving speed for laser 1 |
| `SPEED_LASER_1_LAYER`  | `Layer={n} Speed=mm/S`  | Speed for a specific layer       |
| `SPEED_AXIS`          | `Speed=mm/S`           | Axis movement speed              |
| `SPEED_AXIS_MOVE`     | `Speed=mm/S`           | Axis rapid move speed            |
| `FORCE_ENG_SPEED`     | `Speed=mm/S`           | Force engraving speed override   |

### 2.4 File & Job Control

| Mnemonic                  | Parameters              | Description                          |
| ------------------------- | ----------------------- | ------------------------------------ |
| `START_JOB`           | *(none)*                | Begin processing the current job     |
| `STOP_JOB`            | *(none)*                | Stop processing                      |
| `PAUSE_JOB`           | *(none)*                | Pause processing                     |
| `RESUME_JOB`         | *(none)*                | Resume paused processing             |
| `BLOCK_END`               | *(none)*                | End of a block                       |
| `END_JOB`            | *(none)* or `= value`   | File checksum (see §7)               |
| `SET_FILE_NAME`           | `File:string`           | Set the file name for upload         |
| `DOCUMENT_FILE_UPLOAD`    | `FNum={n} {val} {val}`  | Upload document data                 |
| `DOCUMENT_FILE_END`       | *(none)*                | End of file upload                   |
| `DOCUMENT_NUMBER`         | `{n}`                   | Select document by number            |
| `SELECT_DOCUMENT`         | `{n}`                   | Select document by index             |
| `DELETE_DOCUMENT`         | `{val} {val}`           | Delete a document                    |
| `FILE_TRANSFER`           | *(none)*                | Enter file transfer mode             |

### 2.5 Settings & Queries

| Mnemonic                  | Parameters              | Description                          |
| ------------------------- | ----------------------- | ------------------------------------ |
| `GET_SETTING`             | `MEM_*`                 | Read a controller memory address     |
| `SET_SETTING`             | `MEM_* {val} {val}`     | Write to a controller memory address |
| `ENQ`                     | *(none)*                | Send keep-alive                      |
| `CORE NOP`                | *(none)*                | No operation                         |
| `PEN_OFFSET_AXIS`        | `Axis:X/Y REL=mm`       | Pen compensation offset          |
| `LAYER_OFFSET_AXIS`      | `Axis:X/Y REL=mm`       | Layer offset                     |
| `DISPLAY_OFFSET`         | `X=mm Y=mm`             | Display coordinate offset        |

### 2.6 System & Layer Control

| Mnemonic                  | Parameters              | Description                          |
| ------------------------- | ----------------------- | ------------------------------------ |
| `LAYER_END`               | *(none)*                | End of current layer                 |
| `WORK_MODE_1`—`6`         | *(none)*                | Select work mode                     |
| `LASER_DEVICE_0`—`1`      | *(none)*                | Select laser device                  |
| `AIR_ASSIST_ON`           | *(none)*                | Enable air assist                    |
| `AIR_ASSIST_OFF`          | *(none)*                | Disable air assist                   |
| `DB_HEAD`                 | *(none)*                | Dual-head mode                       |
| `SELECT_LAYER`       | `Layer={n}`              | Select layer by number               |
| `LAYER_COLOR`             | `Color:\#RRGGBB`        | Set layer color                      |
| `LAYER_COLOR`        | `Layer={n} Color:\#RRGGBB` | Set color for a specific layer     |
| `EN_LASER_TUBE_START`     | `State=ON/OFF`         | Enable laser tube at start           |
| `LAYER_ATTRIBUTES`        | `Layer={n} {n}`          | Layer attribute flags            |
| `LAST_LAYER`              | `Layer={n}`              | Indicates the last layer index   |
| `OVERSCAN_START`          | *(none)*                 | Overscan at start only           |
| `OVERSCAN_END`            | *(none)*                 | Overscan at end only             |
| `OVERSCAN_ALL`            | *(none)*                 | Overscan at both start and end   |
| `EN_LASER_2_OFFSET_0`     | *(none)*                 | Enable laser 2 position offset   |
| `EN_EX_IO`                | `{n}`                    | Enable external I/O              |

### 2.7 Array Operations

| Mnemonic                  | Parameters              | Description                          |
| ------------------------- | ----------------------- | ------------------------------------ |
| `ARRAY_START`             | `{n}`                   | Begin array definition               |
| `ARRAY_END`               | *(none)*                | End array definition                 |
| `ARRAY_DIRECTION`         | `Dir={n}`               | Array direction                      |
| `ARRAY_COPIES`            | `Columns={n} Rows={n} XStep={n}mm YStep={n}mm` | Array repeat copies                  |
<!-- table not formatted: invalid structure -->
| `ARRAY_ADD`               | `X=mm Y=mm`             | Array step offset                    |
| `ARRAY_MIRROR`            | `{n}`                   | Mirror mode                          |
| `ARRAY_TOP_RIGHT`          | `X=mm Y=mm`             | Array boundary top-right              |
| `ARRAY_BOTTOM_LEFT`      | `X=mm Y=mm`             | Array boundary bottom-left          |
| `ELEMENT_MAX_INDEX`       | `{n}`                   | Maximum element index            |
| `ELEMENT_NAME_MAX_INDEX`  | `{n}`                   | Maximum element name index       |
| `ELEMENT_NAME_INDEX`      | `{n}`                   | Element name index               |
| `ELEMENT_ARRAY_TOP_RIGHT`  | `X=mm Y=mm`             | Element array bounding box top-right |
| `ELEMENT_ARRAY_BOTTOM_LEFT` | `X=mm Y=mm`           | Element array bounding box bottom-left |
| `ELEMENT_COPIES`          | `Columns={n} Rows={n} XStep={n}mm YStep={n}mm` | Element copy count and step offset |
| `ELEMENT_ARRAY_ADD`       | `X=mm Y=mm`             | Element array step offset        |
| `ELEMENT_ARRAY_MIRROR`    | `{n}`                   | Element array mirror mode        |
| `ARRAY_EVEN_DISTANCE`     | `XStep={n}mm YStep={n}mm` | Even distance between array copies |

### 2.8 USB Rotary / Z / U Axis

| Mnemonic                  | Parameters              | Description                          |
| ------------------------- | ----------------------- | ------------------------------------ |
| `ENABLE_BLOCK_CUTTING`    | `State=ON/OFF`         | Enable block (rotary) cutting        |
| `FEED_AUTO_CALC`          | `{n}`                   | Auto-calculate feed length           |
| `FEED_LENGTH`             | `{val}`                 | Set feed length                      |
| `FEED_REPEAT`             | `{val} {val}`           | Feed repeat counts                   |
| `FEED_INFO`               | `{val}`                 | Feed information                     |
| `JOG_Z`              | `Rel={0-3} Z=mm`     | Jog Z-axis relative to reference point                 |
| `JOG_U`              | `Rel={0-3} U=mm`     | Jog U-axis relative to reference point (rotary)        |
| `ELEMENT_INDEX`           | `{n}`                   | Select element by index              |
| `ELEMENT_NAME`            | `String:string`         | Set element name                     |
| `SET_FEED_AUTO_PAUSE`     | `State=ON/OFF`         | Enable/disable auto-pause on feed |
| `SET_CURRENT_ELEMENT_INDEX` | `{n}`                 | Set the current element index    |

### 2.9 Job & Layer Bounding Boxes

| Mnemonic                  | Parameters              | Description                          |
| ------------------------- | ----------------------- | ------------------------------------ |
| `JOB_TOP_RIGHT`           | `X=mm Y=mm`             | Job bounding box top-right corner    |
| `JOB_BOTTOM_LEFT`         | `X=mm Y=mm`             | Job bounding box bottom-left corner  |
| `DOCUMENT_TOP_RIGHT`      | `X=mm Y=mm`             | Document bounding box top-right      |
| `DOCUMENT_BOTTOM_LEFT`    | `X=mm Y=mm`             | Document bounding box bottom-left    |
| `JOB_COPIES`              | `Columns={n} Rows={n} XStep={n}mm YStep={n}mm` | Job copy count and step offset |
| `LAYER_TOP_RIGHT`         | `Layer={n} X=mm Y=mm`   | Layer bounding box top-right corner  |
| `LAYER_BOTTOM_LEFT`       | `Layer={n} X=mm Y=mm`   | Layer bounding box bottom-left corner |
| `LAYER_EX_TOP_RIGHT`      | `Layer={n} X=mm Y=mm`   | Extended layer bounding box top-right |
| `LAYER_EX_BOTTOM_LEFT`    | `Layer={n} X=mm Y=mm`   | Extended layer bounding box bottom-left |

---

## 3. Parameter Types & Units

When writing parameter values, the parser strips the unit suffix and
converts the numeric portion to the appropriate internal encoding.

| Parameter     | Example Values              | Unit/Format        | Notes                             |
| ------------- | --------------------------- | ------------------ | --------------------------------- |
| Coordinate    | `100.500`, `100.500mm`      | `{:.3f}mm`           | Micrometer precision internally   |
| Power         | `80`, `80.0%`               | `{:.1f}%`           | 0–100% range                      |
| Speed         | `150`, `150.000mm/S`        | `{:.3f}mm/S`        | Millimeters per second            |
| Frequency     | `20`, `20.000KHz`           | `{:.3f}KHz`         | Kilohertz                         |
| Time/Delay    | `5`, `5.000mS`, `500ms`     | `{:.3f}mS`           | Milliseconds. Also accepts `MS`.  |
| Switch/State  | `ON`, `OFF`, `1`, `0`, `TRUE`, `FALSE` | Boolean | Case-insensitive for words.        |
| Color         | `#FF0000`                   | `#RRGGBB`           | Hex color code                     |
| Part/Layer    | `1`, `2`, ...               | integer             | Layer number (index).              |
| Memory addr   | `MEM_CARD_ID`, `0x057E`    | mnemonic or hex     | Mnemonic from MT table (see §4).   |
| Integer       | `42`, `0x1A`                | plain / hex        | Signed or unsigned per command.    |
| File number   | `FNum:1`, `1`               | `uint14`            | Document file index.               |
| Rel           | `0`, `1`, `2`, `3`          | integer (0–3)       | RELT: MACHINE, ANCHOR, CURRENT, SET_POINT |

### Suffix tolerance

All unit suffixes are case-insensitive and optional:
- `mm`, `MM` — coordinates
- `%` — power
- `mm/S`, `mm/s`, `MM/S` — speed
- `KHz`, `kHz` — frequency
- `mS`, `ms`, `MS` — time

A plain number without a suffix uses the command's default unit.

---

## 4. Memory Addresses

Use `GET_SETTING` and `SET_SETTING` with mnemonics from the MT table.
Mnemonics resolve to 2-byte memory addresses (MSB, LSB).

> **Protect mode**: By default, `SET_SETTING` commands are blocked by the driver's
> protect mode to prevent accidental hardware damage. Use `/protect off` in the TUI
> to disable protection, or `/protect on` to re-enable it.

### Machine Status

| Mnemonic                    | Address   | Value Type      | Description                    |
| --------------------------- | --------- | --------------- | ------------------------------ |
| `MEM_MACHINE_STATUS`        | `0x0400` | `int` (bitmask) | bit 0=Moving, bit 1=Part End, bit 2=Job Running |
| `MEM_CURRENT_POSITION_X`    | `0x0421` | `float`/`int`   | Current X position             |
| `MEM_CURRENT_POSITION_Y`    | `0x0431` | `float`/`int`   | Current Y position             |
| `MEM_CURRENT_POSITION_Z`    | `0x0441` | `float`/`int`   | Current Z position             |
| `MEM_CURRENT_POSITION_U`    | `0x0451` | `float`/`int`   | Current U position (rotary)    |

### Card & Bed

| Mnemonic                    | Address   | Value Type      | Description                    |
| --------------------------- | --------- | --------------- | ------------------------------ |
| `MEM_CARD_ID`               | `0x057E` | `int`           | Card identifier                |
| `MEM_BED_SIZE_X`            | `0x0026` | `float`/`int`   | Bed width (X)                  |
| `MEM_BED_SIZE_Y`            | `0x0036` | `float`/`int`   | Bed height (Y)                 |

### Axis Configuration (Laser 1)

| Mnemonic                    | Address   | Description                        |
| --------------------------- | --------- | ---------------------------------- |
| `MEM_G0_VELOCITY`           | `0x005`  | Rapid (G0) velocity                |
| `MEM_HOME_VELOCITY`         | `0x00C`  | Home sequence velocity             |
| `MEM_LASER_PWM_FREQUENCY_1` | `0x011`  | PWM frequency for laser 1          |
| `MEM_LASER_MIN_POWER_1`     | `0x012`  | Minimum power setting for laser 1  |
| `MEM_LASER_MAX_POWER_1`     | `0x013`  | Maximum power setting for laser 1  |
| `MEM_AXIS_PRECISION_1`      | `0x021`  | Axis 1 precision (steps/mm)        |
| `MEM_AXIS_MAX_VELOCITY_1`   | `0x023`  | Axis 1 maximum velocity            |
| `MEM_AXIS_MAX_ACC_1`        | `0x025`  | Axis 1 maximum acceleration        |
| `MEM_BED_SIZE_X`            | `0x026`  | Bed width (X)                      |
| `MEM_BED_SIZE_Y`            | `0x036`  | Bed height (Y)                     |

Axis 2 and Axis 3 follow the same pattern at offsets `0x030`+ and
`0x040`+ respectively.

### Example

```rds
# Read card identity
GET_SETTING MEM_CARD_ID

# Read current position
GET_SETTING MEM_CURRENT_POSITION_X
GET_SETTING MEM_CURRENT_POSITION_Y
```

---

## 5. Flow Control

### DELAY

Pauses script execution for a specified duration. The delay runs in the
background runner thread and is interruptible by `cancel_script()` or
`stop()`.

```rds
DELAY 5s       # Wait 5 seconds
DELAY 500ms    # Wait 500 milliseconds
```

### WAIT

Polls a machine status bit until it becomes active. The runner thread
blocks during polling but remains responsive to shutdown signals.

```rds
# Wait for the machine to start moving
WAIT MACHINE_STATUS_MOVING

# Wait for the machine to stop moving
WAIT !MACHINE_STATUS_MOVING

# Wait for a job to complete (active → then inactive)
WAIT !MACHINE_STATUS_JOB_RUNNING     # No timeout
WAIT !MACHINE_STATUS_JOB_RUNNING to=30s  # With timeout
```

The `!` prefix means "wait for active, then wait for inactive" — it
handles the full lifecycle. Without `!`, it waits only for the bit to
become set.

The optional `to=` parameter sets a timeout (e.g., `30s`, `10000ms`).
If the timeout expires before the condition is met, the script continues
with an error event.

### Common Patterns

```rds
# Home and wait for completion
HOME_XY
WAIT !MACHINE_STATUS_MOVING to=30s

# Cut a line and wait for it to finish
SET_ABSOLUTE
MOVE_FAR_XY X=10mm Y=10mm
LASER_ON Power=80%
SPEED_LASER_1 Speed=150mm/S
CUT_FAR_XY X=110mm Y=10mm
LASER_OFF
WAIT !MACHINE_STATUS_MOVING to=30s
```

---

## 6. Checksum

The file checksum is a running sum of all engrave/cut command bytes.
It ensures the file was transmitted without corruption.

### Auto-Calculate (Placeholder Mode)

Write `END_JOB` with no value and the runner fills in the correct
checksum after all preceding commands are encoded:

```rds
START_JOB
... engrave / cut commands ...
BLOCK_END
END_JOB
```

The checksum is calculated from all commands between `START_JOB` and
`BLOCK_END` that are related to engraving, cutting, and layer
configuration. Memory commands (`GET_SETTING`, `SET_SETTING`), keyboard
commands, and `END_JOB` itself are excluded.

### Verify Mode

Provide the expected checksum value when you know it:

```rds
END_JOB = 9763961
```

The runner accumulates its own checksum and compares. A mismatch raises
`ValueError` (unless `auto_checksum=True`, in which case it logs a
warning and auto-corrects).

### Rules

- At most **one** `END_JOB` per script.
- `END_JOB` must come near the end — after all commands whose bytes
  contribute to the checksum.
- Excluded from checksum: `0xA7` (keypress), `0xDA` (SETTING/GET_SETTING),
  and `0xE5/0x05` (`END_JOB` itself).

---

## 7. Complete Examples

### Homing and Position Query

```rds
# Home all axes
HOME_XY
HOME_Z
WAIT !MACHINE_STATUS_MOVING to=30s

# Read the resulting position
GET_SETTING MEM_CURRENT_POSITION_X
GET_SETTING MEM_CURRENT_POSITION_Y
GET_SETTING MEM_CURRENT_POSITION_Z
```

### Rectangle Cut with Power and Speed

```rds
# Setup
SET_ABSOLUTE
SPEED_LASER_1 Speed=120mm/S
IMD_POWER_1 Power=85%

# Navigate to start position
MOVE_FAR_XY X=50mm Y=50mm

# Cut a 100mm × 100mm rectangle
LASER_ON Power=85%
CUT_FAR_XY X=150mm Y=50mm
CUT_FAR_XY X=150mm Y=150mm
CUT_FAR_XY X=50mm Y=150mm
CUT_FAR_XY X=50mm Y=50mm
LASER_OFF

WAIT !MACHINE_STATUS_MOVING to=60s
```

### Multipower Cut with Layers

```rds
# Layer 1: Light engrave
CORE CMD LAYER_END           # End previous layer
SPEED_LASER_1 Speed=300mm/S
IMD_POWER_1 Power=30%
CUT_FAR_XY X=100mm Y=100mm

# Layer 2: Deep cut
CORE CMD LAYER_END
SPEED_LASER_1 Speed=50mm/S
IMD_POWER_1 Power=90%
CUT_FAR_XY X=200mm Y=100mm
```

### Job with File Checksum

```rds
HOME_XY
WAIT !MACHINE_STATUS_MOVING to=30s

SET_ABSOLUTE
START_JOB

SPEED_LASER_1 Speed=200mm/S
IMD_POWER_1 Power=75%

MOVE_FAR_XY X=10mm Y=10mm
LASER_ON
CUT_FAR_XY X=200mm Y=10mm
LASER_OFF

BLOCK_END
END_JOB

GET_SETTING MEM_CARD_ID
```

### Array (Step-and-Repeat)

```rds
# Define a 2×3 array with 50mm spacing
ARRAY_START 1
ARRAY_COPIES Columns=2 Rows=3 XStep=0.000mm YStep=0.000mm
ARRAY_ADD X=50mm Y=50mm
ARRAY_DIRECTION 1

# The element to repeat
START_JOB
SPEED_LASER_1 Speed=200mm/S
IMD_POWER_1 Power=80%
CUT_FAR_XY X=20mm Y=20mm

BLOCK_END
ARRAY_END
END_JOB
```

---

## 8. Passing Scripts to RdDriver

Scripts are passed as lists of strings:

```python
from ruidadriver.ruida_driver import RdDriver

driver = RdDriver()
driver.start(udp_host="192.168.1.100")

script = [
    "HOME_XY",
    "WAIT !MACHINE_STATUS_MOVING to=30s",
    "GET_SETTING MEM_CURRENT_POSITION_X",
    "GET_SETTING MEM_CURRENT_POSITION_Y",
]

driver.run(script)

# ... later ...
driver.stop()
```

### auto_checksum Parameter

```python
# Warn on checksum mismatch instead of raising
driver.run(script, auto_checksum=True)
```

### Script Lifecycle

1. `run()` queues the script for background execution.
2. The runner thread parses and encodes each line.
3. Encoded commands are sent to the transport layer.
4. On disconnect, the script is re-queued automatically.
5. Call `cancel_script()` to abort.

---

## 9. Error Handling

| Condition                   | Behavior                                   |
| --------------------------- | ------------------------------------------ |
| Runner not started          | `RuntimeError("Call start() first")`     |
| Unknown mnemonic            | `ValueError` at parse time                 |
| Invalid parameter value     | `ValueError` at encode time                |
| Checksum mismatch           | `ValueError` raised (unless auto_checksum) |
| Duplicate `END_JOB`    | `ValueError` raised                        |
| Transport disconnect        | Script re-queued, `DISCONNECTED` event fired |
| All other parse/encode errors | `SCRIPT_ERROR` event fired, runner continues |
| Empty script (empty list)   | Silent no-op                               |

Errors during parsing or encoding are caught by the runner, a
`SCRIPT_ERROR` event is fired to registered listeners, and the runner
continues to the next command. The script is not aborted unless the
controller disconnects.

---

## 10. File Structure (.rd Files)

This section describes the structure of an `.rd` file as a sequence of commands that define
a complete laser job. An `.rd` file consists of several major sections which appear in the
following order:

- 10.3 Header — initial setup and checksum start
- 10.4 Job settings — job-level configuration and bounding boxes
- 10.5 Layer settings — per-layer configuration (one block per layer)
- 10.6 Offset settings — pen and display offset compensation
- 10.7 Array settings — element and array definitions for step-and-repeat
- 10.8 Layer actions — the actual move and cut commands for each layer
- 10.9 Tail — job termination and checksum
- 10.10 Complete Example — complete `.rd` file structure combining all sections

### 10.1 Coordinate System

All coordinates in an `.rd` file are consistent with the bed as displayed by RDWorks:

- Origin (0, 0) is the **top-right** corner of the bed.
- Increasing the **X** coordinate moves the laser head **leftward**.
- Increasing the **Y** coordinate moves the laser head **downward**.

### 10.2 Bounding Boxes

A bounding box defines the limits of head movement for a job or individual layers.
Bounding boxes are expressed as **top-right** and **bottom-left** coordinate pairs.
The job bounding box is the union of all layer bounding boxes; if a layer extends
beyond the current job bounds, the job bounding box expands to accommodate it.

### 10.3 Header

The header section sets initial or known states for the job and identifies the
beginning of the commands to be included in the file checksum.

```
REF_POINT_ABSOLUTE
SET_ABSOLUTE
REF_POINT_SET
ENABLE_BLOCK_CUTTING State:OFF
START_JOB
FEED_REPEAT 0 0
SET_FEED_AUTO_PAUSE State:OFF
```

All commands between `START_JOB` and `BLOCK_END` that are related to engraving,
cutting, and layer configuration are included in the file checksum. Memory commands
(`GET_SETTING`, `SET_SETTING`), keyboard commands, and `END_JOB` itself are excluded.

### 10.4 Job Settings

This section defines settings and bounding boxes which apply to the entire job.

The job bounding box defines the movement limits for the entire job. These limits
are determined by the bounding boxes of all individual layers. If a layer bounding
box falls outside the current job bounding box, the job bounding box is expanded
to accommodate the layer.

```
JOB_TOP_RIGHT X=\<min X of layers\>mm Y=\<min Y of layers\>mm
JOB_BOTTOM_LEFT X=\<max X of layers\>mm Y=\<max Y of layers\>mm
DOCUMENT_TOP_RIGHT X=\<min X of layers\>mm Y=\<min Y of layers\>mm
DOCUMENT_BOTTOM_LEFT X=\<max X of layers\>mm Y=\<max Y of layers\>mm
JOB_COPIES Columns=1 Rows=1 XStep=0.000mm YStep=0.000mm
ARRAY_DIRECTION Dir:0
```

NOTE: The DOCUMENT bounding box is currently equal to the JOB bounding box.

### 10.5 Layer Settings

This section defines layer-specific settings and bounding boxes for each layer of
the job. Layers (`<layer>`) are numbered starting with 0.

```
SPEED_LASER_1_LAYER Layer:<layer> Speed:100.000mm/S
MIN_POWER_1_LAYER Layer:<layer> Power:19.995%
MAX_POWER_1_LAYER Layer:<layer> Power:19.995%
MIN_POWER_2_LAYER Layer:<layer> Power:19.995%
MAX_POWER_2_LAYER Layer:<layer> Power:19.995%
LAYER_COLOR Layer:<layer> Color:\#000000
LAYER_ATTRIBUTES Layer:<layer> 3
LAYER_TOP_RIGHT Layer:<layer> X=\<X\>mm Y=\<Y\>mm
LAYER_BOTTOM_LEFT Layer:<layer> X=\<X\>mm Y=\<Y\>mm
LAYER_EX_TOP_RIGHT Layer:<layer> X=\<X\>mm Y=\<Y\>mm
LAYER_EX_BOTTOM_LEFT Layer:<layer> X=\<X\>mm Y=\<Y\>mm
```

NOTE: `<layer>` is initialized to -1 and incremented before emitting the layer
settings. This ensures `LAST_LAYER` will report the index of the last layer processed.

Following all layer settings, the total number of layers is indicated:

```
LAST_LAYER Layer:<layer>
```

### 10.6 Offset Settings

This section defines offsets for the job. Currently, all offsets are set to 0.

```
PEN_OFFSET_AXIS Axis:X REL=0.000mm
PEN_OFFSET_AXIS Axis:Y REL=0.000mm
LAYER_OFFSET_AXIS Axis:X REL=0.000mm
LAYER_OFFSET_AXIS Axis:Y REL=0.000mm
DISPLAY_OFFSET X=0.000mm Y=0.000mm
```

### 10.7 Array Settings

The Ruida controller supports array processing (step-and-repeat) for duplicating
elements across the bed. There are two sub-sections: ELEMENT and ARRAY.

Variables:
- `<xstep>` = `<sub>_BOTTOM_LEFT:X` - `<sub>_BOTTOM_RIGHT:X`
- `<ystep>` = `<sub>_BOTTOM_LEFT:Y` - `<sub>_BOTTOM_RIGHT:Y`

```
ELEMENT_MAX_INDEX 0
ELEMENT_NAME_MAX_INDEX 0
ELEMENT_INDEX 0
ELEMENT_NAME_INDEX 0
ELEMENT_NAME String:"UNNAMED "
ELEMENT_ARRAY_TOP_RIGHT X=\<X\>mm Y=\<Y\>mm
ELEMENT_ARRAY_BOTTOM_LEFT X=\<X\>mm Y=\<Y\>mm
ELEMENT_COPIES Columns=1 Rows=1 XStep=<xstep>mm YStep=<ystep>mm
ELEMENT_ARRAY_ADD X=0.000mm Y=0.000mm
ELEMENT_ARRAY_MIRROR 0
ARRAY_START 0
SET_CURRENT_ELEMENT_INDEX 0
ARRAY_TOP_RIGHT X=\<X\>mm Y=\<Y\>mm
ARRAY_BOTTOM_LEFT X=\<X\>mm Y=\<Y\>mm
ARRAY_ADD X=\<X\>mm Y=\<Y\>mm
ARRAY_MIRROR 0
ARRAY_EVEN_DISTANCE XStep=\<X\>mm YStep=\<Y\>mm
ARRAY_COPIES Columns=1 Rows=1 XStep=<xstep>mm YStep=<ystep>mm
```

NOTE: The ELEMENT and ARRAY bounding boxes `<X>` and `<Y>` are currently equal
to the JOB bounding boxes.

### 10.8 Layer Actions

Layer actions define the processing for each layer. Processing settings are defined
first, followed by a series of move and cut commands. There is one block of layer
actions for each layer in the job.

Variables:
- `<mode>` = The overscan mode (START, END, or ALL).
- `<layer>` = The layer to which these commands apply.
- `<speed>` = The cut speed for the layer.
- `<power>` = The power at which to cut.
- `<assist>` = Air assist switch (ON or OFF).
- `<min_power>` = The minimum power to use.
- `<max_power>` = The maximum power to use.

```
OVERSCAN_<mode>
SELECT_LAYER Layer:<layer>
EN_LASER_2_OFFSET_0
LASER_DEVICE_0
AIR_ASSIST_<assist>
SPEED_LASER_1 Speed:<speed>mm/S
LASER_ON_DELAY 0.000mS
LASER_OFF_DELAY 0.000mS
THROUGH_POWER_1 Power:<max_power>%
THROUGH_POWER_2 Power:<max_power>%
MIN_POWER_1 Power:<min_power>%
MAX_POWER_1 Power:<max_power>%
MIN_POWER_2 Power:<min_power>%
MAX_POWER_2 Power:<max_power>%
EN_LASER_TUBE_START State:ON
EN_EX_IO 0
(MOVE and CUT commands)...
```

NOTES:
- Multiple laser heads are supported by the Ruida controller but only one is
  currently supported in rpascript.
- A valid min and max power is actually in the range of 8% to 70%. Values outside
  this range should issue a warning. Power levels below 8% may not be sufficient
  to fire the laser. Power levels above 70% can reduce the life of the laser tube.
- Grayscale pixel images may require power settings between move and cut commands.
  The actual commands for this case are currently unknown.
- Frequency can be controlled but is currently the default (typically 30 KHz).

### 10.9 Tail

The tail signals the end of the job.

Variables:
	`<sum>` = The calculated checksum for the job.

```
ARRAY_END
BLOCK_END
SET_SETTING
END_JOB Sum:<sum>
EOF
```

### 10.10 Complete Example

Below is a complete `.rd` file structure combining all sections:

```
# ── Header ──
REF_POINT_ABSOLUTE
SET_ABSOLUTE
REF_POINT_SET
ENABLE_BLOCK_CUTTING State:OFF
START_JOB
FEED_REPEAT 0 0
SET_FEED_AUTO_PAUSE State:OFF

# ── Job Settings ──
JOB_TOP_RIGHT X=0.000mm Y=0.000mm
JOB_BOTTOM_LEFT X=400.000mm Y=300.000mm
DOCUMENT_TOP_RIGHT X=0.000mm Y=0.000mm
DOCUMENT_BOTTOM_LEFT X=400.000mm Y=300.000mm
JOB_COPIES Columns=1 Rows=1 XStep=0.000mm YStep=0.000mm
ARRAY_DIRECTION Dir:0

# ── Layer Settings (Layer 0) ──
SPEED_LASER_1_LAYER Layer:0 Speed:100.000mm/S
MIN_POWER_1_LAYER Layer:0 Power:19.995%
MAX_POWER_1_LAYER Layer:0 Power:19.995%
MIN_POWER_2_LAYER Layer:0 Power:19.995%
MAX_POWER_2_LAYER Layer:0 Power:19.995%
LAYER_COLOR Layer:0 Color:\#000000
LAYER_ATTRIBUTES Layer:0 3
LAYER_TOP_RIGHT Layer:0 X=0.000mm Y=0.000mm
LAYER_BOTTOM_LEFT Layer:0 X=400.000mm Y=300.000mm
LAYER_EX_TOP_RIGHT Layer:0 X=0.000mm Y=0.000mm
LAYER_EX_BOTTOM_LEFT Layer:0 X=400.000mm Y=300.000mm

LAST_LAYER Layer:0

# ── Offset Settings ──
PEN_OFFSET_AXIS Axis:X REL=0.000mm
PEN_OFFSET_AXIS Axis:Y REL=0.000mm
LAYER_OFFSET_AXIS Axis:X REL=0.000mm
LAYER_OFFSET_AXIS Axis:Y REL=0.000mm
DISPLAY_OFFSET X=0.000mm Y=0.000mm

# ── Array Settings ──
ELEMENT_MAX_INDEX 0
ELEMENT_NAME_MAX_INDEX 0
ELEMENT_INDEX 0
ELEMENT_NAME_INDEX 0
ELEMENT_NAME String:"UNNAMED "
ELEMENT_ARRAY_TOP_RIGHT X=0.000mm Y=0.000mm
ELEMENT_ARRAY_BOTTOM_LEFT X=400.000mm Y=300.000mm
ELEMENT_COPIES Columns=1 Rows=1 XStep=0.000mm YStep=0.000mm
ELEMENT_ARRAY_ADD X=0.000mm Y=0.000mm
ELEMENT_ARRAY_MIRROR 0
ARRAY_START 0
SET_CURRENT_ELEMENT_INDEX 0
ARRAY_TOP_RIGHT X=0.000mm Y=0.000mm
ARRAY_BOTTOM_LEFT X=400.000mm Y=300.000mm
ARRAY_ADD X=0.000mm Y=0.000mm
ARRAY_MIRROR 0
ARRAY_EVEN_DISTANCE XStep=0.000mm YStep=0.000mm
ARRAY_COPIES Columns=1 Rows=1 XStep=0.000mm YStep=0.000mm

# ── Layer Actions (Layer 0) ──
OVERSCAN_START
SELECT_LAYER Layer:0
EN_LASER_2_OFFSET_0
LASER_DEVICE_0
AIR_ASSIST_ON
SPEED_LASER_1 Speed:100.000mm/S
LASER_ON_DELAY 0.000mS
LASER_OFF_DELAY 0.000mS
THROUGH_POWER_1 Power:19.995%
THROUGH_POWER_2 Power:19.995%
MIN_POWER_1 Power:19.995%
MAX_POWER_1 Power:19.995%
MIN_POWER_2 Power:19.995%
MAX_POWER_2 Power:19.995%
EN_LASER_TUBE_START State:ON
EN_EX_IO 0

# Move and cut commands
MOVE_FAR_XY X=50.000mm Y=50.000mm
CUT_FAR_XY X=150.000mm Y=50.000mm
CUT_FAR_XY X=150.000mm Y=150.000mm
CUT_FAR_XY X=50.000mm Y=150.000mm
CUT_FAR_XY X=50.000mm Y=50.000mm

# ── Tail ──
ARRAY_END
BLOCK_END
SET_SETTING
END_JOB Sum:0x0000050CF4
EOF
```
