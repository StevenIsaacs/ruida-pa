# Integration Guide

**Application:** `rpa.py` / `rpa-script`  
**Source:** `ruidadriver/ruida_driver.py` (RdDriver), `ruidadriver/rd_gluescript.py` (GlueScript), `rpascript/tui_adapter.py` (TuiAdapter)  
**Status:** As-built (describes current implementation)

---

## 1. Introduction

This guide covers two integration paths for working with Ruida laser controllers programmatically:

| Audience | Path | Section |
|----------|------|---------|
| Application developers embedding controller control | Direct RdDriver + GlueScript API | [§2](#2-direct-rddriver-integration) |
| TUI/testing developers automating session workflows | TuiAdapter emulation | [§3](#3-tui-emulation-for-testing) |

Application developers should build laser jobs with the high-level GlueScript API
(`declare_job`/`declare_layer`/move/cut/`stage_gluescript`) rather than hand-writing
low-level rpascript. GlueScript simultaneously generates a high-level transcript
(persisted to `.cglu` files) and the low-level rpascript that the controller executes.

### Prerequisites

- Python 3.10+
- Basic familiarity with the GlueScript API (see [GlueScript Guide](gluescript-guide.md)) and the underlying low-level rpascript format (see [rpascript Guide](rpascript-guide.md))
- A Ruida controller on the local network (UDP) or connected via USB serial

### Companion Documents

| Document | What It Covers |
|----------|----------------|
| [RdDriver Interface](../api/RdDriver-interface.md) | Full API reference for the RdDriver class |
| [GlueScript Guide](gluescript-guide.md) | High-level job scripting API — build jobs with declare_job/declare_layer/move/cut/stage_gluescript, .cglu persistence |
| [rpascript Guide](rpascript-guide.md) | Low-level script format, command reference, flow-control directives (the substrate GlueScript generates) |
| [TUI User Guide](tui-guide.md) | Interactive terminal application usage |

---

## 2. Direct RdDriver Integration

Jobs are assembled with the GlueScript API and staged to low-level rpascript for
execution. `run()` accepts rpascript lines directly for simple commands.

### 2.1 Minimal Integration

`RdDriver` is a subclass of `GlueScript` (`class RdDriver(GlueScript)`), so the
full GlueScript API is part of the driver: `declare_job()`, `declare_layer()`,
`move_xy_to()`, `cut_xy_to()`, `end_job()`, `stage_gluescript()`, and the
jog/home commands are all directly callable as exposed `RdDriver` methods.
There is no separate `GlueScript` object to construct — the `driver` instance
itself *is* the GlueScript, holding both the scripting state (transcript lines,
job/layer storage) and the live connection.

```python
from ruidadriver.ruida_driver import RdDriver

driver = RdDriver()
driver.register_status_listener(lambda e: print(f"[STATUS] {e}"))
driver.register_error_listener(lambda e: print(f"[ERROR] {e}"))

# Build a job — GlueScript methods, inherited by RdDriver and called
# directly on the driver instance.
driver.declare_job("Panel Cut", ref_point="MACHINE")

driver.declare_layer(
    "Engrave", "#000000",
    mode="VECTOR", speed=300.0,
    min_power_1=15.0, max_power_1=60.0
)

driver.move_xy_to(10.0, 10.0)
driver.cut_xy_to(210.0, 10.0)
driver.cut_xy_to(210.0, 110.0)
driver.cut_xy_to(10.0, 110.0)
driver.cut_xy_to(10.0, 10.0)

driver.end_job()

# Stage the job into low-level rpascript (assembled from job/layer storage)
driver.stage_gluescript()

if not driver.start(udp_host="192.168.1.100"):
    print("Connection will retry in background...")

# Compose head + job + tail and execute (no job argument — runs the
# rpascript most recently staged by stage_gluescript())
driver.run_job()
driver.stop()
```

**Note:** `run()` accepts raw rpascript lines for simple commands (e.g.
`GET_SETTING MEM_CARD_ID`) — see §2.4. Building jobs by hand-writing rpascript
is discouraged; use the GlueScript API instead.

### 2.2 Full Lifecycle

A driver instance must go through a strict lifecycle:

```
__init__() → start() → [run() ... run()] → stop()
```

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `start` | `(udp_host=None, usb_device=None)` | `bool` | Create session, configure transport, open connection, start background runner. `True` if opened immediately, `False` if retry needed (retries in background). Reuses previous params when `None`. Idempotent on same params — no-op if already running. |
| `stop` | `()` | `None` | Stop runner thread (2s join timeout), disconnect session, unregister listeners. Idempotent. Connection params persist for next `start()`. |

**`start()` behavior notes:**
- If params are `None`, reuses values from the previous call.
- If a session exists with different params, calls `stop()` first, then creates a fresh session.
- If a session exists with the same params, returns `True` immediately (no-op).

**`stop()` behavior notes:**
- Sends a shutdown sentinel to the script queue, joins the runner thread with 2s timeout.
- Unregisters all session/transport listeners.
- Sets the internal session reference to `None`.

### 2.3 Listener Registration

All three methods are thread-safe and additive (no remove API).

| Method | Callback Signature | When Called |
|--------|-------------------|-------------|
| `register_status_listener` | `Callable[[RdStatusEvent \| StatusDict], None]` | Session events (CONNECTED, DISCONNECTED) and machine status changes (position, status bits) |
| `register_error_listener` | `Callable[[str], None]` | Script encoding/parsing/execution errors; VmRSS warnings |
| `register_reply_listener` | `Callable[[list[str]], None]` | Formatted reply strings for non-handled GET_SETTING commands |

**Important threading rules:**
- Listener callbacks fire from **background threads** (runner thread or handshake thread). UI applications must use thread-safe dispatch (e.g., `call_from_thread()` in Textual, `invokeLater()` in Qt).
- All listener lists are copied under `RLock` before iteration. Each callback is individually guarded — one faulty callback cannot block other listeners.
- Register listeners **before** calling `start()`. Listener registration does NOT retroactively fire for past events.

**Textual (TUI) bridge pattern:**

```python
def on_status_event(self, event: RdStatusEvent | StatusDict) -> None:
    self.call_from_thread(self._handle_status, event)

def _handle_status(self, event):
    # Runs on the asyncio thread — safe to update widgets
    self.status_log.write(f"[STATUS] {event}")
```

### 2.4 Script Execution

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `run` | `(script: list[str], auto_checksum: bool = False)` | `None` | Queue low-level rpascript lines for background execution (as staged by GlueScript's `stage_gluescript()`, or hand-written for simple commands). Raises `RuntimeError` if runner not started. Empty scripts are silent no-op. |
| `cancel_script` | `()` | `None` | Clear all queued scripts and prevent current script from requeuing on disconnect. Thread-safe. |

After `stage_gluescript()` returns the SHA-256 signature of the staged
transcript, the staged rpascript is available
via `driver.rpascript` (or `get_rpascript()` over RPC).

**Flow-control commands** are processed inline by the driver (not sent to the controller):

| Command | Syntax | Description |
|---------|--------|-------------|
| `DELAY` | `DELAY 5s` or `DELAY 500ms` | Blocking sleep in the runner thread. Interruptible by `stop()`. |
| `WAIT` | `WAIT MACHINE_STATUS_MOVING` | Poll machine status bit until active (set). |
| `WAIT !` | `WAIT !MACHINE_STATUS_JOB_RUNNING to=30s` | Wait for full lifecycle: active → then inactive. Optional `to=` timeout. |

### 2.5 Properties

| Property | Type | Description |
|----------|------|-------------|
| `is_connected` | `bool` | `True` if session exists AND controller is responding to pings. |
| `machine_status` | `dict[int, Any]` | Read-only snapshot of decoded memory values, keyed by memory address. Contains position coordinates, status bits, card ID, bed dimensions. |

### 2.6 Static Format Utilities

These pure formatting functions can be called without a driver instance:

- `format_reply_value(address, raw_reply) -> tuple[str | None, str]` — Decode a reply bytearray using the MT table.
- `format_reply(reply) -> str` — Format a GET_SETTING reply as a human-readable string (e.g., `"MEM_CARD_ID: 12345"`).
- `format_reply_list(replies) -> list[str]` — Map `format_reply` over a list of reply bytearrays.

### 2.7 Threading Model

```
┌──────────────────────────────────┐
│        Application Thread        │
│  start(), run(), stop()          │
│  register_*_listener()           │
├──────────────────────────────────┤
│   Background Script Runner (L6)  │  ← daemon thread
│  - dequeues scripts from queue   │
│  - encodes to binary             │
│  - calls transport.write()       │
│  - handles DELAY/WAIT commands   │
├──────────────────────────────────┤
│    Handshake Thread (L4)         │  ← daemon thread, inside RdTransport
│  - ACK/REPLY state machine       │
│  - unswizzles + validates data   │
│  - fires TransportEvent          │
├──────────────────────────────────┤
│   Status Monitor Thread (L5)     │  ← daemon thread, inside RdStatus
│  - ping/query scheduling         │
│  - auto-reconnect on failure     │
│  - fires RdStatusEvent           │
└──────────────────────────────────┘
```

Key threading rules:
1. **`start()` and `stop()` are blocking** — `stop()` joins the runner thread with 2s timeout and disconnects synchronously.
2. **`run()` is non-blocking** — appends to a `queue.Queue`; the runner thread processes asynchronously.
3. **Listener callbacks fire from background threads** — applications must use thread-safe dispatch for UI updates.

### 2.8 Error Handling

| Condition | Behavior |
|-----------|----------|
| `start()` with empty/unreachable host | Returns `False`; status monitor retries in background |
| `start()` with different params than prior call | Calls `stop()` first, then creates fresh session |
| `run()` before `start()` | Raises `RuntimeError("Script runner not started. Call start() first.")` |
| `run([])` (empty script) | Silent no-op |
| Script encoding error | Fires `SCRIPT_ERROR` + error listener; continues to next script |
| Transport disconnect mid-script | Re-queues full script; fires `DISCONNECTED` |
| `cancel_script()` during execution | Clears queue; current script iteration won't requeue |
| `END_JOB` mismatch + `auto_checksum=False` | Raises `ValueError` with expected/actual values |
| `END_JOB` mismatch + `auto_checksum=True` | Auto-recalculates checksum; logs warning; continues |
| Duplicate `END_JOB` | Raises `ValueError("Duplicate END_JOB")` |
| Listener callback raises exception | Caught by `except Exception: pass`; other listeners unaffected |

### 2.9 Head/Tail Script Management

The driver supports automatic preamble/postamble composition for
every job execution. This is useful for commands that must execute
before and after every job (e.g., home positioning, laser
configuration, returning to origin).

**Accessors:**

| Method          | Signature                                     | Returns   | Description                                               |
| --------------- | --------------------------------------------- | --------- | --------------------------------------------------------- |
| `set_head_script` | `(script: list[str])`                           | `None`      | Set the head script to prepend to every job. Thread-safe. |
| `set_tail_script` | `(script: list[str])`                           | `None`      | Set the tail script to append to every job. Thread-safe.  |
| `get_head_script` | `()`                                            | `list[str]` | Return a copy of the current head script. Thread-safe.    |
| `get_tail_script` | `()`                                            | `list[str]` | Return a copy of the current tail script. Thread-safe.    |
| `run_job`         | `(job: list[str] | None = None, auto_checksum: bool = False)` | `None`      | Queue head + job + tail for background execution.         |

**Composition model:**

```
head_script + job + tail_script → composed script → run()
```

`run_job()` composes the final script by concatenating head, job
body, and tail, then delegates to `run()` for background execution.
The composition happens atomically at queue time under the driver's
lock. Subsequent changes to head or tail do not affect already-queued
jobs.

When `job` is omitted, `run_job()` runs the rpascript most recently
staged by `stage_gluescript()` — the head/tail scripts wrap the entire
staged job. `run_job()` raises `RuntimeError` if no gluescript has
been staged.

**Typical usage:**

```python
driver = RdDriver()
driver.start(udp_host="192.168.1.100")

# Configure head (runs before every job)
driver.set_head_script([
    "SET_ABSOLUTE",
    "MOVE_FAR_XY X=0mm Y=0mm",
])

# Configure tail (runs after every job)
driver.set_tail_script([
    "MOVE_FAR_XY X=0mm Y=0mm",
    "END_JOB",
])

# Run a job — head and tail are prepended/appended automatically
driver.run_job([
    "MOVE_FAR_XY X=100mm Y=100mm",
    "LASER_ON Power=80%",
    "MOVE_FAR_XY X=200mm Y=200mm",
    "LASER_OFF",
], auto_checksum=True)

driver.stop()
```

**Thread safety:** All five methods are guarded by `self._lock`.
Accessors return copies to prevent callers from mutating internal
state. `run_job()` captures head/tail snapshots under the lock so
the composed script is consistent even if head/tail are modified
concurrently. When `job` is omitted, `run_job()` runs the rpascript
most recently staged by `stage_gluescript()` — that staged snapshot
is also captured under the lock.

### 2.10 File Structure Composition

The [rpascript Guide](rpascript-guide.md) defines an `.rd` file as a sequence of
logical sections (§10 File Structure). The GlueScript API generates these
sections for you when you build a job and call `stage_gluescript()`. The mapping
below shows which section each GlueScript call produces:

| rpascript Guide Section | Generated by GlueScript | via |
|-------------------------|-------------------------|-----|
| Header (§10.3) | Yes | `declare_job()` |
| Job Settings (§10.4) | Yes | `declare_job()` (except ARRAY_DIRECTION) |
| Layer Settings (§10.5) | Yes | `declare_layer()` (+ `LAST_LAYER` from `stage_gluescript`) |
| Offset Settings (§10.6) | No | `set_head_script` raw rpascript if needed |
| Array Settings (§10.7) | No | `set_head_script` raw rpascript if needed |
| Layer Actions (§10.8) | Yes | `move_xy_to()`/`cut_xy_to()`/`power()`/`air_assist_on()`/`air_assist_off()` |
| Tail (§10.9) | Yes | `stage_gluescript()` emits `END_JOB`/`EOF` (extra tail via `set_tail_script`) |

The staged output is structured as: job header (Section 1) → inline prelude →
layer attributes sorted (Section 2) → `LAST_LAYER` → per-layer `SELECT_LAYER` +
actions (Section 3) → inline epilogue → `END_JOB` → `EOF` (Section 4). Note that
GlueScript does **not** generate the Offset Settings (§10.6) or Array Settings
(§10.7) rpascript sections — those would have to come from a raw head script
(`set_head_script`) or `inline()`.

**Constructing a job with GlueScript:**

```python
from ruidadriver.ruida_driver import RdDriver

driver = RdDriver()

# Phase 1: Define the job (Header §10.3 + Job Settings §10.4)
driver.declare_job("Panel Cut", ref_point="MACHINE")

# Phase 2: Add layers (Layer Settings §10.5)
driver.declare_layer(
    "Engrave", "#000000",
    mode="VECTOR", speed=300.0,
    min_power_1=15.0, max_power_1=60.0
)

# Phase 3: Add operations to the current layer (Layer Actions §10.8)
driver.move_xy_to(10.0, 10.0)
driver.cut_xy_to(210.0, 10.0)
driver.cut_xy_to(210.0, 110.0)
driver.cut_xy_to(10.0, 110.0)
driver.cut_xy_to(10.0, 10.0)

# Phase 4: Complete the job
driver.end_job()

# Phase 5: Stage — emits header, layer attributes, actions,
# LAST_LAYER, END_JOB and EOF (Tail §10.9)
driver.stage_gluescript()
rpa = driver.rpascript
```

Head/tail (`set_head_script`/`set_tail_script`, see §2.9) remain available for
optional pre/postamble — e.g., home positioning or a return to origin.

**Constructing an optional tail script:**

```python
# Return to origin after the job. END_JOB and EOF are already
# emitted by stage_gluescript() — do not add them to the tail.
driver.set_tail_script([
    "MOVE_FAR_XY X=0mm Y=0mm",
])
```

The checksum value in `END_JOB` must match the running sum of all preceding
commands that participate in checksum calculation (see `should_include_in_checksum`
in `rpascript/encoding.py` for the exclusion rules). When using `auto_checksum=True`,
the driver recalculates and patches `END_JOB` automatically.

**Composing and executing:**

```python
driver.stage_gluescript()   # staged GlueScript output
driver.run_job(auto_checksum=True)
```

**Generating an .rd binary file programmatically:**

To export the composed script as a binary `.rd` file (compatible with RDWorks),
use the `ScriptParser` + `encode_command` pipeline:

```python
from rpascript.interpreter import ScriptParser
from rpascript.encoding import encode_command
from rpalib.ruida_transcoder import RdEncoder
from rpalib.rpa_swizzler import RpaSwizzler

# Compose full script — this is the staged GlueScript output
# (optionally wrapped with head/tail as composed by run_job)
full_script = driver.rpascript

# Parse to command dicts
parser = ScriptParser()
commands = parser.parse_lines(full_script)

# Encode to raw bytes
enc = RdEncoder()
raw = bytearray()
for cmd in commands:
    cmd_type = cmd.get("type")
    if cmd_type in ("new_packet", "SESSION_START", "SESSION_END", "DELAY", "WAIT"):
        continue
    mnemonic = cmd.get("mnemonic")
    if not mnemonic or mnemonic.startswith("GET_"):
        continue
    cmd_bytes = encode_command(cmd, parser.mnemonic_map, parser.mt_map, enc)
    raw.extend(cmd_bytes)

# Swizzle and write .rd file (magic=0x88 for RDWorks import compatibility)
swizzler = RpaSwizzler(magic=0x88)
swizzled = swizzler.swizzle(raw)

with open("output.rd", "wb") as f:
    f.write(b"RDWORKV" + b"\x00" * 3)  # 10-byte header
    f.write(swizzled)

print(f"Wrote {len(raw)} bytes ({len(swizzled)} swizzled)")
```

The same pipeline is used internally by `rpa.py --generate-rd` and the TUI
`/export` command. The `magic` byte (`0x88` for RDWorks) selects the swizzle
pattern; capture-from-controller files typically use `0x88`, while
capture-from-software may use `0x89`.

**Verification round-trip:**

1. Stage a job with GlueScript (`declare_job` → `declare_layer` → operations → `end_job` → `stage_gluescript`)
2. Generate `.rd` via the encoding pipeline
3. Run `python rpa.py output.rd` to decode and verify all sections
4. Compare command sequence against `rpascript-guide.md §10.10`

The decoded output should preserve the original section order, parameter
values, and command count. Discrepancies usually indicate incorrect
parameter encoding or omitted sections.

---

## 3. TUI Emulation for Testing

The `TuiAdapter` class in `rpascript/tui_adapter.py` wraps `RdDriver` with an emulation layer that logs operations and provides a programmatic interface outside the TUI event loop. This is useful for integration testing and automation. High-level job scripting from the TUI uses the `/gluescript` command (subcommands `new`/`show`/`stage`/`run`/`save`/`load`/`edit`/`list`) — see GlueScript Guide §5; the staged rpascript becomes the loaded script (`.rds` slot).

### 3.1 Delegated API


| Method | Signature | Notes |
|--------|-----------|-------|
| `start` | `(udp_host=None, usb_device=None) -> bool` | Creates RdDriver on first call, registers TUI listeners, delegates to `RdDriver.start()` |
| `stop` | `() -> None` | Delegates to `RdDriver.stop()`, clears driver reference |
| `run` | `(script=None, auto_checksum=False) -> Any` | Logs first 3 lines as preview, stores in `_loaded_script`, delegates to `run_script()` |
| `register_status_listener` | `(listener) -> None` | Delegates; raises `RuntimeError` if no active driver |
| `register_error_listener` | `(listener) -> None` | Delegates; raises `RuntimeError` if no active driver |
| `register_reply_listener` | `(listener) -> None` | Delegates; raises `RuntimeError` if no active driver |
| `cancel_script` | `() -> None` | Delegates; safe to call without active driver (no-op) |
| `is_connected` | *(property)* `-> bool` | Passthrough to `RdDriver.is_connected`; `False` if no active driver |
| `machine_status` | *(property)* `-> dict[int, Any]` | Passthrough to `RdDriver.machine_status`; `{}` if no active driver |
| `set_head_script` | `(script: list[str]) -> None` | Logs, stores locally, pushes to driver if active |
| `set_tail_script` | `(script: list[str]) -> None` | Logs, stores locally, pushes to driver if active |
| `get_head_script` | `() -> list[str]` | Returns a copy of local head script |
| `get_tail_script` | `() -> list[str]` | Returns a copy of local tail script |
| `run_job` | `(job: list[str] | None = None, auto_checksum=False) -> None` | Delegates to `driver.run_job()` which composes head + job + tail; `job=None` runs the staged rpascript |

> **Note:** The head/tail accessors (`set_head_script`, `set_tail_script`,
> `get_head_script`, `get_tail_script`, and `run_job`) store
> their values locally in the adapter and propagate them to the
> underlying driver when a session is active. This allows head/tail
> to be configured before `start()` is called.

### 3.2 Programmatic TuiAdapter Usage

```python
from rpascript.tui_adapter import TuiAdapter

# Create adapter without starting the TUI event loop
adapter = TuiAdapter()
adapter.start(udp_host="192.168.1.100")

adapter.run([
    "GET_SETTING MEM_CARD_ID",
    "MOVE_FAR_XY X=100mm Y=200mm",
    "END_JOB",
], auto_checksum=True)

# Access loaded script
print(adapter._loaded_script)  # ["GET_SETTING MEM_CARD_ID", ...]
print(adapter.is_connected)    # True if controller is responding
print(adapter.machine_status)  # {0x057E: (12345, "12345"), ...}

adapter.stop()
```

### 3.3 What Emulation Does NOT Do

This is critical to understand before using TuiAdapter for testing:

- **No controller response simulation** — cannot fake `CONNECTED`/`DISCONNECTED` events. The adapter requires real hardware to produce status updates.
- **No hardware timing** — emulated `DELAY`/`WAIT` commands still block via the real driver. The adapter does not accelerate or skip flow-control commands.
- **No status injection** — cannot inject fake `machine_status` values. `is_connected` and `machine_status` are passthrough properties that require a real connection.
- **No mock layer** — there is no in-memory simulation of controller behavior. All commands are sent to real hardware.

### 3.4 Checksum Discrepancy

When using `auto_checksum=True`, the auto-calculated checksum may not match checksums from LightBurn captures. There is a known ~220 byte discrepancy between this tool's checksum calculation and LightBurn's. Verify expected vs. calculated checksums manually when comparing against LightBurn output.

---

## 4. Integration Testing Patterns

### Pattern 1 — Offline Script Validation

Validate script syntax and structure before sending to hardware:

```python
from rpascript.interpreter import ScriptParser

parser = ScriptParser()
try:
    parsed = parser.parse_lines([
        "MOVE_FAR_XY X=100mm Y=200mm",
        "LASER_ON Power=80%",
        "END_JOB",
    ])
    print(f"Parsed {len(parsed)} commands successfully")
except ValueError as e:
    print(f"Script validation error: {e}")
```

To validate a gluescript transcript instead, re-stage it —
`stage_gluescript(lines)` raises `RuntimeError` on unknown commands or a missing
`end_job()`.

### Pattern 2 — Checksum Verification

Test checksum mismatch handling with `auto_checksum`:

```python
from ruidadriver.ruida_driver import RdDriver

driver = RdDriver()
# With auto_checksum=False (default), mismatch raises ValueError
try:
    driver.run(["MOVE_FAR_XY X=100mm Y=200mm", "END_JOB = 99999"])
except ValueError as e:
    print(f"Expected checksum mismatch: {e}")

# With auto_checksum=True, it auto-fixes and continues
driver.run(["MOVE_FAR_XY X=100mm Y=200mm", "END_JOB = 99999"],
           auto_checksum=True)  # no error
```

### Pattern 3 — Workflow Composition

Test job assembly via GlueScript, staged without a connection:

```python
from ruidadriver.ruida_driver import RdDriver

driver = RdDriver()

# Assemble a job with the high-level GlueScript API
driver.declare_job("Panel Cut", ref_point="MACHINE")

driver.declare_layer(
    "Engrave", "#000000",
    mode="VECTOR", speed=300.0,
    min_power_1=15.0, max_power_1=60.0
)

driver.move_xy_to(10.0, 10.0)
driver.cut_xy_to(210.0, 10.0)
driver.cut_xy_to(210.0, 110.0)
driver.cut_xy_to(10.0, 110.0)
driver.cut_xy_to(10.0, 10.0)

driver.end_job()

# Stage — validates structure without a connection
driver.stage_gluescript()
rpa = driver.rpascript
# Compose via TUI: /gluescript new → /gluescript show → /gluescript stage → /gluescript list
```

Head/tail composition via `set_head_script`/`set_tail_script` remains available
for optional pre/postamble around the staged job.

### Pattern 4 — Flow Control

Test `DELAY` and `WAIT` behavior by examining the driver's flow-control handlers:

```python
script = [
    "DELAY 500ms",
    "WAIT MACHINE_STATUS_MOVING",
    "WAIT !MACHINE_STATUS_JOB_RUNNING to=30s",
    "MOVE_FAR_XY X=100mm Y=200mm",
]
# The driver processes these inline in the runner thread:
# - DELAY: time.sleep(0.5)
# - WAIT: polls machine status bit until set
# - WAIT !: polls until bit is set then cleared (with timeout)
```

### Pattern 5 — Capture Pipeline Round-Trip

Verify end-to-end data flow through the capture/decode/generate pipeline:

```
capture → /import log → /save job rds → /load rds → /list
```

```bash
# Step 1: Capture traffic
./capture 192.168.1.100 my-job.log

# Step 2: Import into TUI (saves editable script)
# TUI command: /import my-job.log
# TUI command: /save job my-job.rds

# Step 3: Load the saved script
# TUI command: /load my-job.rds
# TUI command: /list  # verify line count matches original
```

Capture replay is inherently rpascript (the capture decodes straight to
low-level rpascript). To persist an **authored** gluescript job instead, use
`/gluescript save my-job.cglu` and `/gluescript load my-job.cglu` (see GlueScript
Guide §5.3 — 'Persistence: save and load').

### Pattern 6 — Re-queue on Disconnect

Test that `cancel_script()` correctly prevents re-queue on disconnect:

```python
from ruidadriver.ruida_driver import RdDriver
import time

driver = RdDriver()
driver.start(udp_host="192.168.1.100")
driver.run(["MOVE_FAR_XY X=100mm Y=200mm" for _ in range(100)])

# Cancel mid-execution — current iteration won't requeue
driver.cancel_script()
# On disconnect: script is NOT re-queued (cancel flag is set)
```

---

## 5. End-to-End Pipeline Walkthrough

This walkthrough traces a single capture file through the entire toolchain.

### Step 1 — Capture

```bash
./capture 192.168.1.100 my-job.log
```

Produces `my-job.log` (tshark binary output).

### Step 2 — Import and Save as Script

```bash
# Start TUI
python rpascript/tui.py

# Inside TUI, import the capture
/import my-job.log

# Decode the captured commands and save as rpascript
/save job my-job.rds
```

The TUI decodes the binary capture into human-readable rpascript format and writes `my-job.rds`.

To author a **new** job rather than replay a capture, use `/gluescript` (see GlueScript Guide §5).

### Step 3 — Replay via rpa-script

```bash
# Generate tshark output from the script
rpa-script my-job.rds -o output.tshark
```

Produces `output.tshark` with re-encoded binary packets.

### Step 4 — Verify Round-Trip

```bash
# Decode both files and compare
python rpa.py my-job.log
python rpa.py output.tshark
```

The decoded output should have identical packet sequences (timestamps may vary). This verifies that the capture → script → re-encode pipeline preserves all command data.

---

## 6. AI Agent Integration Guidelines

This section is for AI agents (e.g., OpenCode) that are tasked with integrating RdDriver/TuiAdapter into an application.

### 6.1 Prerequisite Reading Chain

Before writing any integration code, read in this order:

1. **[AGENTS.md](../../AGENTS.md)** — Project overview, commands, architecture, key conventions
2. **[This guide](#1-introduction)** — Integration paths, patterns, pitfalls
3. **Relevant source files** (see below)

### 6.2 Key Source Files

| File | What It Contains |
|------|-----------------|
| `ruidadriver/ruida_driver.py` | RdDriver class (full lifecycle, listeners, flow control) — class starts at line 54, 791 lines total |
| `ruidadriver/rd_gluescript.py` | GlueScript mixin — high-level job scripting (declare_job, declare_layer, move/cut, stage_gluescript); RdDriver inherits from it (class RdDriver(GlueScript)) |
| `rpascript/tui_adapter.py` | TuiAdapter emulation layer — `/`-command handlers (`_cmd_*`), incl. `/gluescript` |
| `rpascript/interpreter.py` | ScriptParser for offline validation |

### 6.3 What to Give an Agent

For best results, include in your prompt:
- **Specific source file paths** (use the table above)
- **Concrete integration goal** (e.g., "Write a class that connects to the controller, runs these 3 commands, and reports the response")
- **Acceptance criteria** (e.g., "Must compile, must handle RuntimeError when start() is not called first")
- **Target framework/tech stack** (e.g., "FastAPI background task", "Qt application", "CLI script")
- Use the GlueScript API (`rd_gluescript.py`) to build jobs; reserve raw rpascript for simple commands (`GET_SETTING`) or workarounds (`inline()`).

### 6.4 Agent-Friendly Patterns

These patterns from §4 require no hardware or minimal hardware:

| Pattern | Why Agent-Friendly |
|---------|-------------------|
| **Pattern 1 (Offline Validation)** | Pure logic — no hardware needed. Tests script parsing only. |
| **Pattern 3 (Workflow Composition)** | Tests script assembly logic without connection. Validates structure only. |
| **Pattern 5 (Capture Round-Trip)** | Tests data flow end-to-end using files only. Verifies no data loss. |

### 6.5 Common Pitfalls

- **`run()` requires `start()` first** — calling `run()` before `start()` raises `RuntimeError("Script runner not started...")`. Always confirm the lifecycle order.
- **Listeners do not retroactively fire** — register listeners before `start()`. Events that occur between `start()` and listener registration are lost.
- **Checksum discrepancy** — `auto_checksum=True` may not match LightBurn captures. If comparing against LightBurn output, verify checksums independently.
- **No mock layer** — `TuiAdapter` delegates to real hardware. It cannot simulate controller responses or inject fake status values. Unit tests requiring simulated hardware must implement their own mock layer.
- **`start()` returns `False` on failure** — this is not an exception. The driver retries in background. Check `is_connected` property to confirm connection status.
- **`end_job()` is required before `stage_gluescript()`** — otherwise `RuntimeError`. Re-staging a transcript missing `end_job()` also raises (unless `require_complete=False`).
- **Jog and home commands are live-only** — `jog_*` (incl. `jog_set_*`), `home`, `home_z`, `home_u` act on the live session, are never persisted to `.cglu`, and are skipped with a warning when re-staging.
- **`power()` is only valid for IMAGE/DEPTHMAP layers** — calling it on a VECTOR layer logs a warning and is ignored.

### 6.6 Verification Workflow

Since this project has no automated test infrastructure, agents should:

1. **Write a minimal smoke test** — instantiate `RdDriver`, register a listener, call `run` with an empty script, verify no crash.
2. **Run `python -m py_compile`** on all changed files to verify syntax.
3. **For cross-file changes**, also compile the files that import from changed modules.

---

## 7. Configuration Notes

- **Transport:** UDP (Ethernet) is default. USB (serial via pyserial) is optional — pass `usb_device=` instead of or in addition to `udp_host=`.
- **Ping interval:** 5000ms default. Queries every 1000ms.
- **Timeouts:** Per-command timeout 250ms, gross timeout 15s for long operations (home sequences, etc.).
- **Connection retry:** Every 1000ms when not connected.

---

## 8. Remote Control via RPyC

This section covers the RPyC (Remote Python Call) integration path, which makes the full `TuiAdapter` surface remotely callable. Beyond the lifecycle methods (`start`/`stop`/`run`), head/tail script management, status listeners, and format utilities, every functional GlueScript job-authoring and live-command method (except the Z/U move/cut stubs, see §8.9) is exposed over RPC. This enables headless control of Ruida laser controllers from external applications, CI/CD pipelines, or distributed systems.

### 8.1 Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Client Process                     │
│  ┌──────────────────────────────────────────────┐   │
│  │  RPyC Client Connection                       │   │
│  │  • Connects via TCP + optional TLS            │   │
│  │  • Sends auth token (optional)                │   │
│  │  • Calls exposed_* methods via netref         │   │
│  └────────────┬─────────────────────────────────┘   │
└───────────────┼─────────────────────────────────────┘
                │  TCP (encrypted if TLS)
┌───────────────┼─────────────────────────────────────┐
│  ┌────────────┴─────────────────────────────────┐   │
│  │  RPyC Server (ThreadedServer)                 │   │
│  │  • Binds to host:port (default 127.0.0.1:18812)│   │
│  │  • Validates auth token (optional)             │   │
│  │  • Dispatches to RpycTuiService               │   │
│  └────────────┬─────────────────────────────────┘   │
│  ┌────────────┴─────────────────────────────────┐   │
│  │  RpycTuiService                               │   │
│  │  • Wraps TuiAdapter instance                  │   │
│  │  • Delegates the full TuiAdapter surface      │   │
│  │  • Wraps netref callbacks in error handlers   │   │
│  └────────────┬─────────────────────────────────┘   │
│  ┌────────────┴─────────────────────────────────┐   │
│  │  TuiAdapter                                    │   │
│  │  • Emulates RdDriver API                       │   │
│  │  • Manages driver lifecycle                    │   │
│  │  • All calls logged with [RPC] prefix          │   │
│  └────────────┬─────────────────────────────────┘   │
│  ┌────────────┴─────────────────────────────────┐   │
│  │  RdDriver                                     │   │
│  │  • Actual controller communication            │   │
│  └──────────────────────────────────────────────┘   │
│                   Server Process                     │
└─────────────────────────────────────────────────────┘
```

### 8.2 Starting the RPC Server

The RPC server is started from within the TUI using the `server start` command:

```bash
# Minimal (localhost, no auth)
server start

# Custom port
server start port=18812

# With authentication token (remote access)
server start host=0.0.0.0 token="s3cret!t0k3n"

# With TLS encryption
server start host=0.0.0.0 \
    cert=./rpyc-certs/server-cert.pem \
    key=./rpyc-certs/server-key.pem \
    token="s3cret!t0k3n"
```

To stop the server, use `server stop` from within the TUI.

While the server is running, the `/rpclog` TUI command toggles the server's
verbose per-call logging (see the [TUI guide](tui-guide.md#6-slash-commands));
the toggle is TUI-only and is not exposed to RPC clients.

Like `session`/`server`, the 16 jog commands (`jog_*`, including the
`jog_set_*` config setters) and the 3 homing commands (`home`, `home_z`,
`home_u`) are available as bare commands in the TUI — they are live-only
(movement jogs and homing run immediately against a connected session;
setters configure the live jog session) and are never persisted
to `.cglu` files. See the [GlueScript guide](gluescript-guide.md#bare-jog--home-commands)
for the bare-command argument forms (jog/home commands are live-only and never
persisted to `.cglu` files).

| Parameter | Default      | Description                                    |
|-----------|--------------|------------------------------------------------|
| `host`    | `localhost`  | Bind address. `localhost`/`127.0.0.1` skips auth/TLS |
| `port`    | `18812`      | TCP port                                       |
| `cert`    | (none)       | TLS certificate path (ignored if localhost)    |
| `key`     | (none)       | TLS private key path (ignored if localhost)    |
| `token`   | (none)       | Auth token (ignored if localhost)              |

Parameters persist across `server start`/`stop` cycles — omitted values reuse the previous invocation's values.

### 8.3 Client Connection Example

```python
import socket
import rpyc
from rpyc.utils.factory import connect_stream
from rpyc.utils.classic import SocketStream


def connect_rpyc(host="127.0.0.1", port=18812, token=None):
    """Connect to the RPyC server and return the service root."""
    sock = socket.create_connection((host, port))

    if token:
        # Send auth token: 1 byte length + N bytes token
        token_bytes = token.encode("utf-8")
        sock.sendall(bytes([len(token_bytes)]) + token_bytes)
    else:
        # Send empty length byte for localhost
        sock.sendall(b"\x00")

    conn = connect_stream(SocketStream(sock))
    return conn.root


# --- Usage ---

# Connect
svc = connect_rpyc("127.0.0.1", 18812, token="s3cret!t0k3n")

# Start the driver
connected = svc.start(udp_host="192.168.1.100")
print(f"Connected: {connected}")

# Register a status listener (netref callback)
def on_status(event):
    print(f"[STATUS] {event}")

svc.register_status_listener(on_status)

# Run a script
svc.run(["GET_SETTING MEM_CARD_ID"], auto_checksum=True)

# Check connection
print(f"Is connected: {svc.is_connected()}")

# Stop
svc.stop()
```

### 8.4 Authentication Details

The token authentication protocol uses a simple length-prefixed exchange:

1. Client connects TCP socket
2. Client sends 1 byte (token length N) + N bytes (token UTF-8)
3. Server validates with constant-time comparison (`hmac.compare_digest`)
4. If valid, RPyC handshake proceeds normally

**Localhost exception:** Connections from `127.0.0.1`, `::1`, or `localhost` without a token (empty length byte) are allowed through. This enables local testing without auth while requiring tokens for remote connections.

**Auth failure behavior:** Invalid tokens cause the server to immediately close the connection. The client receives an `EOFError` or `ConnectionRefusedError` on the first RPyC call.

### 8.5 TLS Configuration

TLS is configured via RPyC's `ssl_ctx` parameter, which accepts a standard Python `ssl.SSLContext`:

| File                | Purpose                | Generated By                      |
|---------------------|------------------------|-----------------------------------|
| `ca-cert.pem`      | CA certificate         | `scripts/gen-rpyc-certs.sh`      |
| `ca-key.pem`       | CA private key (keep secret) | `scripts/gen-rpyc-certs.sh` |
| `server-cert.pem`  | Server certificate     | `scripts/gen-rpyc-certs.sh`      |
| `server-key.pem`   | Server private key (keep secret) | `scripts/gen-rpyc-certs.sh` |

Generate with:

```bash
./scripts/gen-rpyc-certs.sh ./rpyc-certs
```

This produces 4096-bit RSA certificates valid for 10 years with full X.509 v3 extensions (SKI, AKI, KeyUsage, ExtendedKeyUsage) required by Python 3.14+.

### 8.6 Netref Callback Caveats

Listener callbacks (`register_status_listener`, `register_error_listener`, `register_reply_listener`) are implemented as RPyC netrefs — the callback function is defined on the client side but invoked by the server.

**What works:**
- Plain functions, lambdas, and instance methods all work as callbacks
- Multiple callbacks can be registered simultaneously
- Callbacks execute on the client side in the client's RPyC connection thread

**What to watch for:**
- **Threading:** Callbacks fire from the server's dispatch thread. Long-running callbacks block the server for that connection. Keep callbacks fast or use your own thread pool.
- **Exceptions:** Exceptions raised in callbacks are caught and logged by `RpycTuiService` with a warning — they don't crash the server. Your callback should handle its own errors.
- **Serialization:** Arguments passed to callbacks must be serializable by RPyC. Basic types (`str`, `int`, `list`, `dict`), `bytearray`, and netrefs work. Custom objects may need explicit serialization.
- **No retroactive events:** Register a callback before events occur. Events between `start()` and callback registration are lost.

### 8.7 Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `ConnectionRefusedError` | Server not running | Check `host` and `port` in `server start` |
| `EOFError: connection closed by peer` | Auth token wrong or missing | Verify `token` in `server start` matches client code |
| `ssl.SSLError: certificate verify failed` | CA cert not trusted | Pass correct CA cert path to client |
| Callback not firing | Listener registered after event | Register before `start()` |
| `RuntimeError: Script runner not started` | `run()` called before `start()` | Call `svc.start()` before `svc.run()` |
| Slow RPC calls | Netref latency over network | Keep scripts small, use batch operations |

### 8.8 Head/Tail Script Management via RPC

All five head/tail methods are exposed as RPC methods:

| RPC Method              | Signature                                             | Description                                         |
| ----------------------- | ----------------------------------------------------- | --------------------------------------------------- |
| `exposed_set_head_script` | `(script: list[str]) -> None`                           | Set head script on the server's driver              |
| `exposed_set_tail_script` | `(script: list[str]) -> None`                           | Set tail script on the server's driver              |
| `exposed_get_head_script` | `() -> list[str]`                                       | Retrieve current head script from server            |
| `exposed_get_tail_script` | `() -> list[str]`                                       | Retrieve current tail script from server            |
| `exposed_run_job`         | `(job: list[str] | None = None, auto_checksum: bool = False) -> None` | Queue head + job + tail for execution on the server; `job=None` runs the staged rpascript |

**Example:**

```python
# Configure head/tail remotely
svc.set_head_script([
    "SET_ABSOLUTE",
    "MOVE_FAR_XY X=0mm Y=0mm",
])
svc.set_tail_script([
    "MOVE_FAR_XY X=0mm Y=0mm",
    "END_JOB",
])

# Verify
head = svc.get_head_script()
tail = svc.get_tail_script()
print(f"Head: {len(head)} lines, Tail: {len(tail)} lines")

# Run job with head/tail composition
svc.run_job([
    "MOVE_FAR_XY X=100mm Y=100mm",
    "LASER_ON Power=80%",
    "LASER_OFF",
], auto_checksum=True)
```

**Thread safety:** The server-side ``RpycTuiService`` logs each call
and delegates to ``TuiAdapter`` which propagates to ``RdDriver``. All
underlying accessors are thread-safe.

**Configuration before connection:** Head and tail scripts can be set
via RPC before ``start()`` is called — the ``TuiAdapter`` stores them
locally and pushes to the driver once the session is active.

### 8.9 GlueScript Job Authoring & Live Commands via RPC

All GlueScript methods are directly callable as exposed `RdDriver` methods:
`RdDriver` is a subclass of `GlueScript` (`class RdDriver(GlueScript)`), so 38 of
the 40 RPC-exposed GlueScript job-authoring and live-command methods are inherited by the
driver itself — there is no separate `GlueScript` object; the
`get_gluescript`/`get_rpascript` methods are adapter-level getters returning
copies of the driver's gluescript/rpascript state. Developers call these methods
directly on the `RdDriver` instance, which inherits them from `GlueScript`, and
the RPC service exposes them through the same **unprefixed** names on that
instance: `svc.declare_job(...)`, `svc.jog_xy_to(...)`, `svc.stage_gluescript()`.
Each `RpycTuiService.exposed_*` method delegates to the matching
`TuiAdapter.gluescript_*` method, which invokes the corresponding method on the
`RdDriver` instance — inherited from `GlueScript` for all but the
`get_gluescript`/`get_rpascript` getters; the `exposed_` prefix is a server-side
detail.
This complements the [TUI `/gluescript` usage](gluescript-guide.md#5-tui-usage-gluescript):
the same authoring and live-command methods you drive interactively are also
callable programmatically. Head/tail composition and the five head/tail RPC
methods are covered separately in [§8.8](#88-headtail-script-management-via-rpc).

The methods fall into five groups — authoring (session-less), config setters,
movement jogs, homing, and getters. The table lists each client-facing name,
its signature, return type, and whether a connected session is required.

| RPC method | Signature | Returns | Session required |
| ---------- | --------- | ------- | ---------------- |
| **Authoring (session-less)** | | | |
| `new_gluescript` | `()` | `None` | No |
| `comment` | `(comments: list[str])` | `None` | No |
| `inline` | `(commands: list[str])` | `None` | No |
| `declare_job` | `(label, ref_point="MACHINE", abs_xy=None, columns=1, rows=1, xstep=0.0, ystep=0.0)` | `None` | No |
| `end_job` | `()` | `None` | No |
| `declare_layer` | `(label, color, mode="VECTOR", overscan="NONE", speed=100.0, frequency=20.0, min_power_1=8.0, max_power_1=70.0)` | `None` | No |
| `move_xy_to` | `(x, y)` | `None` | No |
| `move_x_to` | `(x)` | `None` | No |
| `move_y_to` | `(y)` | `None` | No |
| `cut_xy_to` | `(x, y)` | `None` | No |
| `cut_x_to` | `(x)` | `None` | No |
| `cut_y_to` | `(y)` | `None` | No |
| `power` | `(percent=None)` | `None` | No |
| `air_assist_on` | `()` | `None` | No |
| `air_assist_off` | `()` | `None` | No |
| `add_layer_action` | `(layer, lines: list[str])` | `None` | No |
| `update_position` | `(x=None, y=None, z=None, u=None)` | `None` | No |
| `stage_gluescript` | `(gluescript=None, require_complete=True)` | `str` | No |
| **Config setters (session-less)** | | | |
| `jog_set_xy_speed` | `(speed)` | `None` | No |
| `jog_set_z_speed` | `(speed)` | `None` | No |
| `jog_set_u_speed` | `(speed)` | `None` | No |
| `jog_set_xy_rel` | `(delta)` | `None` | No |
| `jog_set_z_rel` | `(delta)` | `None` | No |
| `jog_set_u_rel` | `(delta)` | `None` | No |
| **Movement jogs (live)** | | | |
| `jog_xy_to` | `(x, y)` | `list[str] \| None` | Yes |
| `jog_x_to` | `(x)` | `list[str] \| None` | Yes |
| `jog_y_to` | `(y)` | `list[str] \| None` | Yes |
| `jog_z_to` | `(z)` | `list[str] \| None` | Yes |
| `jog_u_to` | `(u)` | `list[str] \| None` | Yes |
| `jog_xy_rel` | `(x=None, y=None)` | `list[str] \| None` | Yes |
| `jog_x_rel` | `(x=None)` | `list[str] \| None` | Yes |
| `jog_y_rel` | `(y=None)` | `list[str] \| None` | Yes |
| `jog_z_rel` | `(z=None)` | `list[str] \| None` | Yes |
| `jog_u_rel` | `(u=None)` | `list[str] \| None` | Yes |
| **Homing (live)** | | | |
| `home` | `()` | `list[str] \| None` | Yes |
| `home_z` | `()` | `list[str] \| None` | Yes |
| `home_u` | `()` | `list[str] \| None` | Yes |
| **Getters** | | | |
| `get_gluescript` | `()` | `list[str]` | No |
| `get_rpascript` | `()` | `list[str]` | No |
| `job_complete` | `()` | `bool` | No |

**Returns for live commands:** Movement jogs and homing return the rpascript
lines sent to the controller when connected; `None` when disconnected, or when
the command produces no lines.

**Session-less authoring:** The 18 authoring methods work with no connected
session — they build a job in the driver's GlueScript state. `stage_gluescript()`
finalizes it and returns the SHA-256 signature of the staged transcript;
the staged rpascript is available via `get_rpascript()` (or `driver.rpascript`)
and the staged gluescript via `get_gluescript()`. `run_job` (see §8.8) later
composes head + staged + tail scripts around the job — when `job` is omitted
it runs the rpascript most recently staged by `stage_gluescript()`. A job
authored over RPC is retained in the driver and can be executed after
`svc.start()`.

```python
import socket
import rpyc
from rpyc.utils.factory import connect_stream
from rpyc.utils.classic import SocketStream


def connect_rpyc(host="127.0.0.1", port=18812, token=None):
    """Connect to the RPyC server and return the service root."""
    sock = socket.create_connection((host, port))
    if token:
        token_bytes = token.encode("utf-8")
        sock.sendall(bytes([len(token_bytes)]) + token_bytes)
    else:
        sock.sendall(b"\x00")
    return connect_stream(SocketStream(sock)).root


# Author a job before any session exists — no controller required yet.
svc = connect_rpyc()
svc.new_gluescript()
svc.declare_job("plate", ref_point="MACHINE", abs_xy=[0, 0])
svc.declare_layer("outline", "black", mode="VECTOR", speed=80.0)
svc.move_xy_to(0, 0)
svc.cut_xy_to(100, 0)
svc.cut_xy_to(100, 100)
svc.cut_xy_to(0, 100)
svc.cut_xy_to(0, 0)
svc.end_job()

# Finalize — returns the SHA-256 signature of the staged gluescript
# transcript (a non-empty hex string, so the truthy guard works); copy
# the staged gluescript transcript with get_gluescript(). The job
# persists in the driver for later execution.
if svc.stage_gluescript():
    staged = svc.get_gluescript()   # gluescript transcript (DSL), not rpascript
    print(f"Staged {len(svc.get_rpascript())} rpascript lines")

# Later, once a controller is reachable, run it. run_job composes head +
# staged + tail scripts around the job; with no job argument it runs the
# rpascript most recently staged by stage_gluescript().
svc.start(udp_host="192.168.1.100")
svc.run_job()
```

**Live jogs and homing require a connected session.** Movement jogs
(`jog_xy_to`, `jog_x_to`, `jog_y_to`, `jog_z_to`, `jog_u_to` and the `jog_*_rel`
relative moves) and homing (`home`, `home_z`, `home_u`) execute immediately
against the live session. When disconnected, these calls warn and return
`None`. The `jog_set_*` config setters configure the live jog session's speed
and relative distance and work without a session.

```python
# Movement jogs and homing require a connected session.
if svc.is_connected():
    svc.jog_xy_to(50, 50)
    svc.home()
else:
    print("Disconnected — jog/home will warn and return None")

# Config setters configure the live jog session and work without one.
svc.jog_set_xy_speed(120.0)
svc.jog_set_xy_rel(5.0)
```

**Netref list arguments:** `comment`, `inline`, `declare_job(abs_xy)`, and
`stage_gluescript(gluescript)` take list arguments. Over RPyC these arrive as
netrefs and are converted to plain Python lists on the server side, so clients
can pass ordinary Python lists, e.g. `svc.inline(["LASER_ON Power=80%"])`.

**Not exposed:** `move_z_to`, `move_u_to`, `cut_z_to`, and `cut_u_to` are
not exposed over RPC and raise `NotImplementedError` when called directly on
the driver; over RPC the call fails with `AttributeError` since no `exposed_*`
method exists.

#### Batching layer actions over RPC

The direct client flow above sends one RPC per command: every
`move_*_to`/`cut_*_to` call round-trips individually, so a large job is chatty
on the wire. `rpalib/rpyc_client.RpcGlueScript` is a thin client-side wrapper
around the same service root that buffers the high-volume layer actions —
`move_xy_to`/`move_x_to`/`move_y_to`, `cut_xy_to`/`cut_x_to`/`cut_y_to`,
`power`, `air_assist_on`/`air_assist_off` — locally and flushes them to the
server in one `stage_gluescript()` call at each `declare_layer`/`end_job`
boundary. A job with thousands of moves and cuts reaches the server in a
handful of round trips instead of one per action.

Structural, non-replayable calls are forwarded immediately rather than
buffered: `new_gluescript`, `declare_job`, `declare_layer`, `comment`,
`inline`, `add_layer_action`, `update_position`, the getters
(`get_gluescript`, `get_rpascript`, `job_complete`), and the live jog/home
commands (`jog_*`, `home`, `home_z`, `home_u`) plus the `jog_set_*` config
setters. `power` is buffered only when the current layer mode is
`IMAGE`/`DEPTHMAP` and `percent` is not `None`; otherwise it is forwarded so
the server emits the same warning the driver's guard produces.

**Drift guard:** after each flush the wrapper compares the SHA-256 signature
returned by `svc.stage_gluescript()` with a locally computed signature of its
transcript; `svc.get_gluescript()` is read back ONLY when the signatures
differ. Any mismatch raises
`RuntimeError` naming the first differing line — fail fast, fail loud — which
catches client/server format drift (for example, a server upgraded to a
different line format).

**Getters report the last-flushed server state:** `get_gluescript()`,
`get_rpascript()`, and `job_complete()` reflect the server's state after the
most recent flush. Between flushes, buffered actions exist only in the
client's local transcript and are invisible to the getters until the next
boundary flush.

**Public `stage_gluescript()` passthrough:** `RpcGlueScript.stage_gluescript()`
forwards the call unchanged (including `gluescript=None`), returns the
signature, and performs no local flush and no drift check — mirroring the
direct-client flow when you want to stage explicitly.

**Server-side token requirement:** the client connect handshake sends a
1-byte empty-token length prefix (`b"\x00"`) before
`connect_stream(SocketStream(sock)).root` (see
`tests/rpyc_poc/test_auth.py`). This works ONLY when the server was started
WITH a token authenticator — `start_rpyc_server(..., token="...")`
programmatically, or `server start token="..."` in the TUI. With
`token=None` no authenticator is installed: the server never reads the token
byte, and the `b"\x00"` corrupts the RPyC stream (zlib error). With an
authenticator installed, an empty token is allowed for localhost connections. Note this refines the §8.2 statement that the token is 'ignored if localhost': the *authenticator* must still be installed even for localhost — start the server with `token=...` (any value); the empty-token prefix then passes for localhost.

```python
import socket
import rpyc
from rpyc.utils.factory import connect_stream
from rpyc.utils.classic import SocketStream
from rpalib.rpyc_client import RpcGlueScript

# The server MUST have been started with a token authenticator, e.g.
#   start_rpyc_server(..., token="s3cret!t0k3n")
# or, from the TUI,  server start token="s3cret!t0k3n"
sock = socket.create_connection(("127.0.0.1", 18812))
sock.sendall(b"\x00")  # empty-token length prefix, read by the authenticator
svc = connect_stream(SocketStream(sock)).root
rgs = RpcGlueScript(svc)

# Author a job before any session exists — no controller required yet.
# Moves and cuts are buffered locally, not round-tripped per action.
rgs.new_gluescript()
rgs.declare_job("plate", ref_point="MACHINE", abs_xy=[0, 0])
rgs.declare_layer("outline", "black", mode="VECTOR", speed=80.0)
rgs.move_xy_to(0, 0)
rgs.cut_xy_to(100, 0)
rgs.cut_xy_to(100, 100)
rgs.cut_xy_to(0, 100)
rgs.cut_xy_to(0, 0)
rgs.declare_layer("fill", "red", mode="IMAGE", speed=60.0)
rgs.power(50)
rgs.move_xy_to(10, 10)
rgs.cut_xy_to(90, 10)
rgs.cut_xy_to(90, 90)
rgs.cut_xy_to(10, 90)
rgs.cut_xy_to(10, 10)

# Each declare_layer flushed the buffer; end_job() does the final flush.
rgs.end_job()
staged = rgs.get_gluescript()   # gluescript transcript (DSL), not rpascript
print(f"Staged {len(rgs.get_rpascript())} rpascript lines")

# Later, once a controller is reachable, run it — same flow as the direct
# client above. run_job composes head + staged + tail scripts around the
# job; with no job argument it runs the rpascript most recently staged by
# stage_gluescript().
svc.start(udp_host="192.168.1.100")
svc.run_job()
```
