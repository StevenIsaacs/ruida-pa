# TUI User Guide

**Application:** `rpa-script`  
**Source:** `rpascript/tui.py` → `rpascript/tui_adapter.py` (TuiAdapter class)

---

## 1. Overview

The TUI (Terminal User Interface) is an interactive application for working with
Ruida laser controllers directly from the terminal. It combines:

- **Session management** — connect to and disconnect from controllers over UDP or USB
- **Script execution** — run rpascript (`.rds`) files as jobs in real time
- **Capture import** — decode captured tshark traffic from other applications
  (LightBurn, RDWorks, MeerK40t) into editable scripts
- **Script generation** — save decoded sessions as `.rds` files for playback
- **Real-time monitoring** — live memory usage, GC object counts, and controller
  status updates
- **Visualization** — interactive Bokeh plots of head moves and cut paths

---

## 2. Launching

```bash
# If installed
rpa-script

# From source directory
python rpascript/tui.py
```

No arguments needed — the TUI starts immediately. If a script file is provided
as an argument, it is processed in batch mode instead (see `rpa-script --help`).

---

## 3. Layout

```
┌────────────────────────────────────────────────────┐
│  Ruida Script TUI                       [Header]   │
├───────────────────────────┬────────────────────────┤
│                           │  [STATUS] CONNECTED     │
│  Log Area                 │  [STATUS] PING_SENT     │
│  (RichLog, 1000 lines)    │  [REPLY] 0x057E: 42   │
│                           │                        │
│  [SCRIPT] session start   │  ── status/reply ──    │
│  [INFO] Connected...      │  side panel             │
│                           │                        │
│                           │  VmRSS  VmSize  ...    │
│                           │  Mem:    47100   ...    │
│                           │  Total:   +100   ...    │
│                           │                        │
│                           │  Class     Count  ...   │
│                           │  TransportEv   9  ...   │
├───────────────────────────┴────────────────────────┤
│  > Enter command...                     [Input]    │
│  Connected | UDP 192.168.1.100:50200  [StatusBar]  │
├────────────────────────────────────────────────────┤
│  Ctrl+C Quit                           [Footer]    │
└────────────────────────────────────────────────────┘
```

Three main areas:

**Left panel (log area)**
- Main `RichLog` widget showing commands sent, replies received, system messages,
  and error logs. Scrolling history of the last 1000 lines.

**Right panel (side panel)**
- **Top: status/reply log** — real-time controller status updates (connection
  state changes, ping events, query replies) with dim text styling.
- **Bottom: monitor panel** — two tables updated every 15 seconds:
  - **Memory stats**: VmRSS, VmSize, VmPeak, and Thread count for the TUI process
    with per-interval changes (yellow highlight) and cumulative totals.
  - **GC object counts**: Per-class instance count and deep memory footprint for
    Ruida protocol objects tracked by the garbage collector. Class names appear
    in **[orange]** when the measurement hit the recursion depth limit (500 levels),
    indicating the reported size is a partial count.

**Bottom bar**
- **Command input**: text input for entering commands
- **Status bar**: connection state and transport info (e.g., `Connected | UDP 192.168.1.100:50200`)

---

## 4. Session Management

### Connecting

```
session start udp=192.168.1.100
session start udp=192.168.1.100 usb=ttyUSB0 to=10s
```

Parameters:
- `udp=<IP>` — Controller IP address (required if no USB)
- `usb=<device>` — USB serial device (e.g., `ttyUSB0`, `/dev/ttyACM0`).
  Can be combined with UDP; USB is preferred when both are specified.
- `to=<timeout>` — Connection timeout. Formats: `5s`, `5000ms`. Default: 5000ms.

The TUI remains responsive while connecting. Use `/stop` or **Escape** to cancel
a pending connection.

### Disconnecting

```
session end
```

Stops the background script runner, disconnects the transport, and cleans up
all background threads.

### Connection Lifecycle

1. **Connecting** — Pinging controller, waiting for reply
2. **Connected** — Controller responding, status monitor active
3. **Disconnected** — Lost connection, auto-reconnect in progress
4. **Terminated** — Session explicitly shut down via `session end`

When a connection is lost unexpectedly, the TUI automatically retries
connection in the background. A `DISCONNECTED` event is logged, and the
status bar updates to reflect the state.

---

## 5. Command Input

The TUI classifies each line of input into one of several categories,
processed in this order:

### 5.1 Session Meta-Commands

```
session start udp=192.168.1.100
session end
```

Directives that control the connection lifecycle, handled internally
without involving the controller.

### 5.2 Jog & Home Commands

The 16 jog commands and 3 homing commands are bare commands (no `/` prefix)
that act on the live session:

```
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

- **Live-only semantics** — movement jogs and homing (`home`, `home_z`,
  `home_u`) run immediately against a connected controller (requiring an active
  session) and are never persisted to `.cglu` files; `jog_set_*` setters
  configure the live jog session (speeds and relative distances) and never
  produce gluescript lines.
- **Autocomplete** — typing a `jog` or `home` prefix in the command input shows
  suggestions with usage text.
- **Help** — `/help` lists all 19 under "Jog & Home commands (live-only)".

### 5.3 Slash Commands

```
/help
/load my-script.rds
/exec job
```

TUI meta-commands starting with `/` (see Section 6 for full reference).

### 5.4 rpascript Commands

Any valid rpascript command line:

```
HOME_XY
SET_ABSOLUTE
MOVE_FAR_XY X=100mm Y=200mm
CUT_FAR_XY X=200mm Y=100mm
```

Sent to the controller as a single-line script. Requires an active session.

### 5.5 Flow Control Commands

These are special directives recognized within loaded scripts:

```
DELAY 5s              # Pause execution for 5 seconds
WAIT !MACHINE_STATUS_MOVING to=30s   # Wait for move to complete (30s timeout)
WAIT !MACHINE_STATUS_JOB_RUNNING     # Wait for job to finish (no timeout)
```

- `DELAY` — blocks the script runner for the specified duration (interruptible).
- `WAIT` — polls a status bit until it matches. Prefix `!` means "wait for
  active, then wait for inactive" (full lifecycle). Optional `to=` timeout.
- Available status bits: `MACHINE_STATUS_MOVING`, `MACHINE_STATUS_LAYER_END`,
  `MACHINE_STATUS_JOB_RUNNING`.

---

## 6. Slash Commands

| Command               | Description                                                                  |
| --------------------- | ---------------------------------------------------------------------------- |
| `/help`               | Display formatted help text covering all command categories.                 |
| `/load <path>`        | Load a `.rds` script file into memory for editing or execution.              |
| `/head <path>`        | Load a `.rds` file as head (prepended to future `/exec job` and `/list job`).  |
| `/tail <path>`        | Load a `.rds` file as tail (appended to future `/exec job` and `/list job`).   |
| `/exec`               | Execute the composed job (head + job + tail) as a batch.                     |
| `/exec script`        | Execute the loaded script as raw commands (no job extraction).               |
| `/export <path> [magic=0xNN]` | Export the loaded script as a binary `.rd` file. Default path: `<source>.rd`. Supports `magic=0xNN` to override swizzle byte. |
| `/import <path>`      | Import a tshark capture file (`.log`/`.txt`/`.rd`) and decode into a script. |
| `/save job <path>`    | Save the pure job body (START_JOB to EOF, no head/tail) to a `.rds` file. |
| `/list`               | Show the composed job with section markers (`# --- Head ---` / `# --- Job ---` / `# --- Tail ---`). |
| `/list job`           | Same as `/list`.                                                             |
| `/list script`        | Show only the loaded script (without head/tail).                             |
| `/list head`          | Show the head script.                                                        |
| `/list tail`          | Show the tail script.                                                        |
| `/plot`               | Open an interactive Bokeh visualization of the loaded script.                |
| `/clear`              | Clear all log panels, loaded script, head/tail, and monitor totals.          |
| `/stop`               | Cancel pending session connection or stop script execution. Also on Escape.  |
| `/log on`             | Enable reply logging (controller responses shown in log).                    |
| `/log off`            | Disable reply logging.                                                       |
| `/log status`         | Show whether reply logging is currently enabled.                             |
| `/quit`               | Exit the TUI. Also on Ctrl+C.                                                |

### Error Behavior

| Condition                                            | Message                                                              |
| ---------------------------------------------------- | -------------------------------------------------------------------- |
| Unknown `/` command                                  | `Unknown TUI command: /<cmd>. Type /help or ? for available commands.` |
| `/load` / `/head` / `/tail` with no path             | `Usage: /load <path>` (with appropriate command name)                |
| `/load` / `/head` / `/tail` file not found           | `File not found: <path>`                                              |
| `/load` / `/head` / `/tail` permission denied        | `Permission denied: <path>`                                           |
| `/load` / `/head` / `/tail` binary file              | `File is not a valid text file: <path>`                               |
| `/load` / `/head` / `/tail` empty file               | `File is empty or contains only blank lines: <path>`                   |
| `/import` with no path                               | `Usage: /import <path> [magic=0xNN]`                                 |
| `/import` file not found                             | `File not found: <path>`                                              |
| `/import` decode failure                             | `Decode error: <details>`                                             |
| `/exec` with no script loaded                        | `No script loaded. Use /load <path> first.`                           |
| `/exec` with no session                              | `No active session. Use 'session start udp=...' first.`               |
| `/exec` with no job markers                          | `No job commands found (no START_JOB/EOF markers).`               |
| `/save job` with no script loaded                    | `No script loaded. Use /load <path> first.`                           |
| `/save job` with no job markers                      | `No job commands to save (no START_JOB/EOF markers).`             |
| `/save job` permission denied                        | `Permission denied: <path>`                                           |
| `/save job` write error                              | `Error writing <path>: <ErrorType>: <message>`                        |
| `/list script` with no script loaded                 | `No script loaded. Use /load <path> first.`                           |
| `/list job` with no job markers                      | `No job commands found (no START_JOB/EOF markers).`               |
| `/plot` with no script loaded                        | `No script loaded. Use /load <path> first.`                           |
| `/plot` with no bokeh installed                      | `Bokeh is not installed. Install with: pip install bokeh`            |

### Job Composition

Head and tail scripts are stored by `RdDriver` and applied at execution time.
Each command has a different role:

- **`/exec job`** — extracts the job body (START_JOB → EOF), then calls
  `driver.run_job()` which composes `head + job_body + tail` atomically and
  queues the result for execution. The composition happens inside the driver,
  not in the TUI.
- **`/list job`** — uses `_format_job_with_markers()` to display the composed
  script with section comment markers:
  ```
  # --- Head ---
  <head_script lines>
  # --- Job ---
  <job body lines>
  # --- Tail ---
  <tail_script lines>
  ```
  Empty sections show `# (empty)` for clarity.
- **`/save job`** — saves only the pure job body (START_JOB → EOF).
  Head and tail are **not** included, making the output round-trippable:
  it can be reloaded with `/load` and re-executed without double-appending
  head/tail.

If no `START_JOB`/`EOF` markers exist in the loaded script, the job body
is empty. This allows modular workflow: separate head (homing, initialization),
job body, and tail (cleanup, shutdown) scripts. Use `/exec script` to run
scripts that don't follow the job-marker structure.

---

## 7. File Browser

Commands that take a file path (`/load`, `/head`, `/tail`, `/import`,
`/save job`) trigger an interactive file browser when you type a space
after the command:

- The tree filters to show only matching file types:
  - `.rds` for `/load`, `/head`, `/tail`
  - `.log`, `.txt`, `.rd` for `/import`
  - All files for `/save job`
- **Tab** toggles focus between the command input and the file tree
- **Enter** on a selected file backfills the command with the full path
- **Escape** dismisses the tree
- The tree follows partial paths (e.g., typing `/load /tmp/` starts
  browsing at `/tmp`)
- Navigating into subdirectories is preserved when typing additional
  characters in the same directory

---

## 8. Importing Captures

The `/import` command converts a tshark packet capture into an editable
rpascript script. This is the primary way to turn traffic from other
applications into reusable scripts.

### The Capture → Import → Save Pipeline

```
LightBurn / RDWorks / MeerK40t
        ↓ (UDP traffic to controller)
./capture <controller_ip> <basename>
        ↓ (.log file with tshark fields)
/import <basename>.log   [magic=0xNN]
        ↓ (RPA decode pipeline → _ImportCollector)
.rds script lines in memory
        ↓
/save job <basename>.rds
        ↓ (.rds file on disk)
```

### Step 1: Capture Traffic

Use the `capture` script to record traffic between a laser application and
the controller:

```bash
# Linux / macOS
./capture 192.168.1.100 my-job

# Windows (PowerShell)
.\capture.ps1 -if Ethernet -ip 192.168.1.100 -out my-job
```

This runs `tshark` in the background, filtering on UDP traffic to/from the
controller's IP address. The output is written to `my-job.log` in tshark
fields format (tab-delimited: time delta, port, length, hex payload).

The script first pings the IP address to verify reachability (warning if
unreachable — capturing with the machine off may be intentional for
diagnosing lost-connection behavior).

### Step 2: Import in the TUI

```bash
# In TUI:
/import my-job.log
```

The `/import` command:
1. Opens the `.log` file and runs the RPA decode pipeline in-process
2. Decodes each binary packet into commands with parameters
3. Collects decoded commands as rpascript lines, preserving reply values
4. Loads the result into `_loaded_script`

If the capture uses a non-default swizzle, specify the magic number:

```bash
/import my-job.log magic=0x9A
```

On success:
```
Imported 847 lines from my-job.log
```

On failure, descriptive error messages are shown (decode errors, file not
found, etc.).

### Step 3: Review and Save

```bash
# Review the full script
/list

# Review the job portion only (between START_JOB and EOF)
/list job

# Save the composed job as a reusable script
/save job my-job.rds
```

The saved `.rds` file can be loaded back into the TUI, passed to
`RdDriver.run()`, or played back with `rpa-script`.

### Importing Binary `.rd` Files

The `/import` command also supports RDWorks binary `.rd` files directly:

```bash
# In TUI:
/import capture.rd
```

This feeds the binary bytes through the same parser pipeline without needing a tshark capture layer. Header comments (`# Source: <filename>`) are added automatically.

On success:
```
Imported 847 lines from capture.rd
```

---

## 9. Working with Scripts

### Loading Scripts

```
/load my-job.rds
```

Loads a `.rds` file into memory. The script is stored as `_loaded_script`
and can be executed or saved.

### Head and Tail

For modular workflow, you can split your script into three parts:

```bash
/head setup.rds          # Commands to prepend (e.g., homing, initialization)
/tail cleanup.rds        # Commands to append (e.g., shutdown, air assist off)
```

Head and tail are automatically included in `/exec job` and `/list job`.
`/save job` saves only the pure job body — head/tail are applied at
execution time by the driver.

### Viewing

```bash
/list           # Show composed job with section markers
/list script    # Show loaded script only
/list head      # Show head script
/list tail      # Show tail script
```

### Executing

```bash
/exec job        # Execute the composed job as a batch
/exec script     # Execute the loaded script as raw commands
```

`/exec job` extracts only the portion between `START_JOB` and end-of-file
markers (or `BLOCK_END`), then delegates to `driver.run_job()` which composes
head + job + tail atomically at queue time. This ensures only the job commands
are sent, with setup/teardown wrapped around them.

`/exec script` sends the entire loaded script as-is, without job extraction
or head/tail wrapping. Use this for scripts that don't follow the
START_JOB/BLOCK_END structure.

Both modes require an active session.

### Saving

```
/save job my-output.rds
```

Saves only the pure job body (START_JOB to EOF) as a `.rds` file.
Head and tail are NOT included — the output is the same as the job portion
shown by `/list job` between the section markers. The saved file is
compatible with `rpa-script` playback, `RdDriver.run()`, and can be
reloaded with `/load` without double-appending head/tail.

### Plotting

```
/plot
```

Opens an interactive Bokeh visualization in your browser showing all
individual head moves from the loaded script:

- **Hover** over a vector for a tooltip with move command ID, endpoint
  coordinates, length, power, and speed.
- **Filter** by move type (moves/cuts), power range, and speed range.
- **Right-click** for a context menu, including opening a new tab filtered
  from that command.

![Example:](example-moves.png)

Requires `bokeh` to be installed (`pip install bokeh`) and a virtual
environment active.

### Clearing

```
/clear
```

Clears all log panels, the loaded script, head and tail scripts, and
resets all memory monitor totals.

---

## 10. Monitor Panel

The bottom-right panel displays two tables, updated every 15 seconds:

### Memory Stats

```
        VmRSS KB  VmSize KB  VmPeak KB  Threads
Mem:       47100     541348     542264       15
Change:        0       +100          0        0
Total:         0      +1000          0        0
```

- **Mem**: Current values from `/proc/self/status` (VmRSS, VmSize, VmPeak, Threads)
- **Change**: Difference since the previous update (yellow highlight for non-zero)
- **Total**: Cumulative change since the TUI started (no highlight — stable baseline)

### GC Object Counts

```
Class                 Count         Mem      Change       Total
TransportEvent            9         432       +96        +192
RpaArea                   5         240           0           0
RdStatusEvent            11         528           0         +48
```

- **Class**: Name of the Ruida protocol class tracked by the garbage collector.
  Shown in **[orange]** when the recursive memory measurement hit the depth
  limit (500 levels), meaning the reported size is partial.
- **Count**: Number of live instances.
- **Mem**: Deep memory footprint in bytes (sum of all reachable objects, not
  just the shell size).
- **Change**: Memory delta since the previous update (yellow for non-zero).
- **Total**: Cumulative delta since the TUI started.

The depth limit prevents stack overflow from deeply nested object graphs.
Classes marked in orange are those where at least one instance had a
deeper-than-500 tree, so the reported memory is a lower bound.

Use `/clear` to reset all monitor totals.

---

## 11. Introspection

The TUI provides interactive inspection of internal objects:

```
?              # List available introspection objects
?driver        # Inspect the current RdDriver state
?session       # Inspect the current RdSession state
?transport     # Inspect the transport layer
?status        # Inspect the status monitor
?parser        # Inspect the ScriptParser
?decoder       # Inspect the RdDecoder
?self          # Inspect the TuiAdapter itself
```

Advanced method calls use `!` prefix:

```
!driver.is_connected
!session.status.is_blocked
!decoder.decode_address(0xDA, 0x01)
```

See the built-in help (`/help`) for full syntax details.

---

## 12. Example Workflows

### Example A: Capture from LightBurn → Convert to Script

```bash
# Terminal 1: capture controller traffic from LightBurn
./capture 192.168.1.100 my-job
```
*(Run the laser job from LightBurn while capture is running)*

```bash
# Terminal 2: import and save in the TUI
rpa-script
```
```
/import my-job.log
Imported 847 lines from my-job.log

/list job
[displays first 3 lines of the job]

/save job my-job.rds
Job saved to my-job.rds (652 lines)
```

Result: `my-job.rds` contains the decomposed job, ready for replay or
modification.

### Example B: Capture from RDWorks → Load → Execute

```bash
# Capture the session
./capture 192.168.1.100 rdworks-test
```
```bash
# Launch TUI and import
rpa-script
/import rdworks-test.log
Imported 1423 lines from rdworks-test.log

# Connect and execute
session start udp=192.168.1.100
[STATUS] PING_REPLIED
[STATUS] CONNECTED

/exec job
[SCRIPT] Executing composed job (478 lines)...
[replies appear as controller processes]
```

### Example C: Import → Visualize with /plot

```bash
./capture 192.168.1.100 panel-cut
rpa-script
```
```
/import panel-cut.log
Imported 320 lines from panel-cut.log

/plot
```
*(Bokeh server opens in browser showing the toolpath visualization)*

![Example:](example-moves.png)

### Example D: Full Workflow with Head/Tail Composition

```bash
./capture 192.168.1.100 front-panel
rpa-script
```
```
/import front-panel.log
Imported 950 lines from front-panel.log

# Add homing preamble
/head home.rds
Loaded 3 lines from home.rds

# Add shutdown sequence
/tail finish.rds
Loaded 2 lines from finish.rds

# Review the full composition with section markers
/list job
# --- Head ---
SET_ORIGIN
MOVE_FAR_XY X=0mm Y=0mm
LASER_OFF
# --- Job ---
START_JOB
LAYER_PROMPT "Default"
...
EOF
# --- Tail ---
MOVE_FAR_XY X=0mm Y=0mm

# Save pure job body (head/tail not included)
/save job front-panel-complete.rds
Job saved to front-panel-complete.rds (795 lines)
# Note: /save job includes only the job body (795 lines).
# Head (3) and tail (2) are applied at execution time by the driver.
```

### Example E: Export a Script as Binary `.rd`

After importing or loading a script, export it as a binary `.rd` file:

```
/import capture.log
Imported 847 lines from capture.log

/export
Wrote 883 bytes to capture.rd
```

The exported `.rd` is compatible with RDWorks and can be re-imported with `/import`.

---

## 13. Integration

Scripts produced by `/save job` are compatible with:

- **`rpa-script` playback**: `rpa-script my-job.rds -o output.tshark`
- **`RdDriver.run()`**: Machine-consumed by the library API
- **Re-import**: Load back into the TUI for modification

The `.rds` format is documented in detail in the
[rpascript guide](rpascript-guide.md).
