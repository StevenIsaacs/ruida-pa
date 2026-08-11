# RdDriver Interface Specification

**Package:** `ruidadriver.ruida_driver`  
**Class:** `RdDriver`  
**Layers:** L6 (Driver) → delegates to L5 (RdSession/RdStatus) → L4 (RdTransport)  
**Status:** As-built (describes current implementation)

---

## 1. Purpose

`RdDriver` provides a high-level API for communicating with Ruida laser
controllers. It manages the full lifecycle: connection, background script
execution, real-time status monitoring, and event notification. Applications
integrate by registering listeners and queuing rpascript commands.

---

## 2. Lifecycle

A driver instance must go through a strict lifecycle:

```
__init__() → start() → [run() ... run()] → stop()
```

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `start` | `(udp_host: str \| None = None, usb_device: str \| None = None)` | `bool` | Create session, configure transport, open connection, start background runner. `True` if opened immediately, `False` if retry needed (retries in background). Reuses previous params when `None`. Idempotent on same params — no-op if already running. |
| `stop` | `()` | `None` | Stop runner thread (2s join timeout), disconnect session, unregister listeners. Idempotent. Connection params persist for next `start()`. |

### 2.1 `start()` — Connection Details

```python
def start(self, udp_host: str | None = None, usb_device: str | None = None) -> bool
```

1. If `udp_host`/`usb_device` are `None`, reuses values from previous call.
2. If a session already exists with different params, calls `stop()` first.
3. If a session already exists with same params, returns `True` immediately (no-op).
4. Creates `RdSession()`, calls `transport.configure()`.
5. Calls `transport.open(udp_host=..., usb_device=...)` — UDP and/or USB.
6. Starts the background script runner (registers listeners, configures ping/query commands, starts status monitor thread).

**Return value:** `True` if transport opened successfully on first attempt.  
`False` if open failed — the status monitor will retry in background.  
The application can check `is_connected` later to confirm.

### 2.2 `stop()` — Clean Teardown

```python
def stop(self) -> None
```

1. Sends shutdown sentinel to script queue, joins runner thread (2s timeout).
2. Unregisters all session/transport listeners.
3. Calls `session.disconnect()`.
4. Sets `_session = None`.

**Idempotent:** Safe to call multiple times. Second call is a no-op.

---

## 3. Script Execution

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `run` | `(script: list[str], auto_checksum: bool = False)` | `None` | Queue rpascript-formatted lines for background execution. Raises `RuntimeError` if runner not started. Empty scripts are silent no-op. |
| `run_job` | `(job: list[str] \| None = None, auto_checksum: bool = False)` | `None` | Compose head + job + tail under the driver lock and queue via `run()`. `job=None` runs the rpascript most recently staged by `stage_gluescript()`. Raises `RuntimeError` if `job` is `None` and nothing has been staged. |
| `stage_gluescript` | `(gluescript: list[str] \| None = None, require_complete: bool = True)` | `str` | Finalize the rpascript from GlueScript state, or re-stage a transcript. Returns the SHA-256 signature of the staged transcript; failures raise `RuntimeError`. |
| `stage_gluescript_delta` | `(flushed_count: int, delta_lines: list[str], require_complete: bool = True)` | `str` | Incrementally replay only the newly appended transcript lines onto the existing staged state (no reset). Raises `GlueScriptDeltaMismatchError` when `flushed_count` does not equal the current transcript length. Returns the SHA-256 signature of the staged transcript. |
| `set_head_script` | `(script: list[str])` | `None` | Set the head script prepended to every job execution. Thread-safe. |
| `set_tail_script` | `(script: list[str])` | `None` | Set the tail script appended to every job execution. Thread-safe. |
| `get_head_script` | `()` | `list[str]` | Return a copy of the current head script. Thread-safe. |
| `get_tail_script` | `()` | `list[str]` | Return a copy of the current tail script. Thread-safe. |
| `cancel_script` | `()` | `None` | Clear all queued scripts and prevent current script from requeuing on disconnect. Thread-safe. |

### 3.1 Script Format

Each line is an rpascript command string. Examples:

```
GET_SETTING MEM_CARD_ID
MOVE_FAR_XY X=100mm Y=200mm
LASER_ON Power=80%
MOVE_FAR_XY X=200mm Y=300mm
LASER_OFF
END_JOB
DELAY 5s
WAIT MACHINE_STATUS_MOVING
WAIT !MACHINE_STATUS_JOB_RUNNING to=30s
```

### 3.2 Flow-Control Commands

In addition to standard controller commands, scripts support flow-control:

| Command | Syntax | Description |
|---------|--------|-------------|
| `DELAY` | `DELAY 5s` or `DELAY 500ms` | Blocking sleep in the runner thread. Interruptible by `stop()`. |
| `WAIT` | `WAIT MACHINE_STATUS_MOVING` | Poll machine status bit until active (set). |
| `WAIT !` | `WAIT !MACHINE_STATUS_JOB_RUNNING to=30s` | Wait for full lifecycle: active → then inactive. Optional `to=` timeout. |

### 3.3 Checksum Handling

- `END_JOB` without a value: auto-calculates the file checksum from all preceding commands.
- `END_JOB = 12345` with value: verifies accumulated checksum against the value.
- `auto_checksum=False` (default): raises `ValueError` on mismatch.
- `auto_checksum=True`: auto-recalculates with a warning and continues.
- Duplicate `END_JOB`: raises `ValueError`.

### 3.4 Re-queue on Disconnect

If the transport disconnects mid-script, the entire script is re-queued and
a `DISCONNECTED` event is fired. When the connection is restored, the script
executes from the beginning. Call `cancel_script()` to abort.

### 3.5 `run_job()` — Composed Job Execution

```python
def run_job(self, job: list[str] | None = None, auto_checksum: bool = False) -> None
```

Composes the final script as `head + job + tail` atomically under the driver
lock, then queues it via `run()` for background execution.

- `job` — list of rpascript-formatted command lines (the job body only).
  When omitted (`None`), the rpascript most recently staged by
  `stage_gluescript()` (see 3.6 below) is run instead.
- `job=[]` (empty list) runs head + tail only.
- `auto_checksum` — forwarded to `run()`; see Checksum Handling above.

When `job` is `None`, the staged rpascript is snapshotted under the lock
before composition, so a later re-stage cannot mutate a job mid-composition.

**Raises:** `RuntimeError("No gluescript staged. Call stage_gluescript() first.")`
when `job` is `None` and no gluescript has been staged. `run()` itself raises
`RuntimeError` if the script runner is not started.

### 3.6 `stage_gluescript()` — Staging

```python
def stage_gluescript(self, gluescript: list[str] | None = None, require_complete: bool = True) -> str
```

Finalize the rpascript from the structured GlueScript state, or re-stage a
gluescript transcript. The assembled rpascript order is: job header,
inline prelude, layer attributes, `LAST_LAYER`, per-layer actions with
`SELECT_LAYER`, inline epilogue, `END_JOB`, `EOF`.

- `gluescript` — when provided, resets all state and replays the transcript
  through the command registry (re-staging path). When `None` (default),
  finalizes the job built via the GlueScript methods (`declare_job`,
  `declare_layer`, move/cut, `end_job`).
- `require_complete` — on the re-staging path, raises when the replayed
  transcript never called `end_job()`. Pass `False` to tolerate an
  in-progress job (used when restoring a preserved transcript across
  session teardown, where the user may still be editing).

**Returns:** `str` — the SHA-256 signature (hex) of the staged gluescript
transcript. Failures raise `RuntimeError` instead of returning.

**Raises:** `RuntimeError` on failure — failures never return, they raise. For
example, `end_job()` must be called before staging on the finalization path
(`"end_job() must be called before stage_gluescript()"`), and a re-staged
transcript missing `end_job()` with `require_complete=True` raises
`"Re-staged gluescript is missing end_job() — job was not completed"`.

The staged rpascript is available on the driver as `driver.rpascript` until
the next stage. `run_job()` (see 3.5 above) runs the most recently staged
rpascript when called with `job=None`.

### 3.6.1 `stage_gluescript_delta()` — Incremental Staging

```python
def stage_gluescript_delta(self, flushed_count: int, delta_lines: list[str], require_complete: bool = True) -> str
```

Incremental sibling of `stage_gluescript()` used by the `RpcRdDriver`
client: the driver transcript already holds the first `flushed_count` lines
(replayed by earlier deltas or a full stage), so only the appended suffix is
replayed through the command registry — O(Δ) per flush instead of O(L·N)
over the whole job. No reset is performed; the assembled rpascript (shared
`_assemble_rpascript` path) is identical to a full re-stage.

- `flushed_count` — must equal `len(driver.gluescript)`. When it does not
  (contiguity broken), the method raises `GlueScriptDeltaMismatchError`
  ("Server gluescript has {n} lines but flushed_count is {m} — transcript
  out of sync; full re-stage required"); the client falls back to a full
  `stage_gluescript()` re-stage.
- `delta_lines` — the appended transcript lines to replay.
- `require_complete` — raises when the replayed suffix never called
  `end_job()` (same message as the full re-stage path).

**Returns:** `str` — the SHA-256 signature (hex) of the staged gluescript
transcript. Failures raise `RuntimeError` instead of returning.

**RpcRdDriver — full-surface RPC mirror.** `RpcRdDriver`
(`rpalib/rpyc_client.py`) mirrors the full `RdDriver` surface described in
this document — lifecycle and execution passthroughs (`start`, `stop`,
`run`, `run_job`, `cancel_script`, `set_protect`, head/tail setters and
getters), the listener registration surface (`register_status_listener`,
`register_error_listener`, `register_reply_listener` and their
`unregister_*` counterparts), the format utilities (`format_reply_value`,
`format_reply`, `format_reply_list`, `decode_status_value`), and the
staging passthroughs (`stage_gluescript`, `stage_gluescript_delta`) — so
an app adapter needs no separate direct-vs-RPC path. It can be constructed
with no arguments, self-connecting over RPyC:

```python
RpcRdDriver(svc=None, *, host="127.0.0.1", port=18812,
            token=None, config=None, timeout=5)
```

- With no `svc`, the driver opens and owns its own connection via the
  module-level `connect_rpc(host=..., port=..., token=..., config=...,
  timeout=...)` helper; `close()` closes it. A caller-provided `svc`
  root (`connect_stream(...).root`, as in `tests/rpyc_poc/test_auth.py`)
  remains the first positional argument, exactly as before — the caller
  keeps ownership.
- Token handshake: `token=None` sends NOTHING (pairs with the default
  token-less server, `start_rpyc_server(token=None)`); a non-empty `str`
  sends a 1-byte length prefix plus the UTF-8 bytes (tokens longer than
  255 bytes raise `ValueError`); `token=""` sends the empty-token prefix
  `b"\x00"` accepted on localhost by authenticator-enabled servers.
- Timeouts: the socket timeout bounds the TCP connect and the
  handshake send; every RPC afterwards is bounded by rpyc's
  `sync_request_timeout`, merged connection-wide as `timeout` — rpyc's
  poll path never consults a socket `settimeout`.
- `close()` is idempotent and closes only connections the driver opened
  itself; a caller-provided `svc` root is left to its owner. Afterwards
  any member call raises `RuntimeError("driver closed")` — deliberately
  `RuntimeError` and not `AttributeError`, which `_flush()` treats as "old
  server without the delta method" — and `is_connected` reads False. The
  synchronous HANDLE_CLOSE sent by `close()` is bounded by
  `sync_request_timeout`, so budget the same `timeout` for close-handshake
  latency.
- Getters diverge from the direct driver: `gluescript`, `rpascript`, and
  `job_complete` are snapshot properties fetched over RPC — in-place
  mutation of the returned list is NOT reflected server-side, unlike the
  direct driver's live lists.

See the self-connect example in the integration guide §8.9
(`docs/guides/integration-guide.md`).

### 3.7 Head/Tail Script Accessors

```python
def set_head_script(self, script: list[str]) -> None
def set_tail_script(self, script: list[str]) -> None
def get_head_script(self) -> list[str]
def get_tail_script(self) -> list[str]
```

The head and tail scripts wrap every job executed via `run_job()`: the final
composed script is `head + job + tail`. Setters store a copy under the driver
lock and getters return a copy — both are thread-safe.

The default head script is:

```
REF_POINT_ABSOLUTE
SET_ABSOLUTE
REF_POINT_SET
ENABLE_BLOCK_CUTTING State:OFF
```

The default tail script is empty.

---

## 4. Listener Registration

All three methods are thread-safe and additive (no remove API).

| Method | Callback Signature | When Called |
|--------|-------------------|-------------|
| `register_status_listener` | `Callable[[RdStatusEvent \| StatusDict], None]` | Session events (CONNECTED, DISCONNECTED) and machine status changes (position, status bits) |
| `register_error_listener` | `Callable[[str], None]` | Script encoding/parsing/execution errors; VmRSS warnings |
| `register_reply_listener` | `Callable[[list[str]], None]` | Formatted reply strings for non-handled GET_SETTING commands |

### 4.1 Copy-on-Iterate Safety

All listener lists are copied under `RLock` before iteration. Each callback
is wrapped in `try/except Exception` — one faulty callback cannot block
other listeners from receiving events.

Listeners fire from **background threads** (runner thread or handshake
thread). UI applications must use thread-safe dispatch mechanisms
(e.g., `call_from_thread()` in Textual, `invokeLater()` in Qt).

---

## 5. Properties

| Property | Type | Description |
|----------|------|-------------|
| `is_connected` | `bool` | `True` if session exists AND controller is responding to pings. |
| `machine_status` | `dict[int, Any]` | Read-only snapshot of decoded memory values, keyed by memory address. Values are the raw integers (dimensions are not converted to mm here — see §5.1). Contains position coordinates, status bits, card ID, bed dimensions. |

### 5.1 machine_status Contents

| Address | Mnemonic | Value Type | Description |
|---------|----------|------------|-------------|
| `0x0400` | `MEM_MACHINE_STATUS` | `int` | Bitmask: bit 0=Moving, bit 1=Part end, bit 2=Job running |
| `0x0421` | `MEM_CURRENT_POSITION_X` | `int` | Current X (raw) |
| `0x0431` | `MEM_CURRENT_POSITION_Y` | `int` | Current Y (raw) |
| `0x0441` | `MEM_CURRENT_POSITION_Z` | `int` | Current Z (raw) |
| `0x0451` | `MEM_CURRENT_POSITION_U` | `int` | Current U (raw) |
| `0x057E` | `MEM_CARD_ID` | `int` | Card identifier |
| `0x0026` | `MEM_BED_SIZE_X` | `int` | Bed width (raw) |
| `0x0036` | `MEM_BED_SIZE_Y` | `int` | Bed height (raw) |

---

## 6. Static Format Utilities

These are pure formatting functions that can be called without a driver instance.

### `format_reply_value(address, raw_reply) -> tuple[str | None, str]`

Decode a reply bytearray using the MT table.

- `address`: The memory address extracted from the reply header (int).
- `raw_reply`: Full reply bytearray (min 9 bytes).
- Returns: `(mnemonic_string_or_None, formatted_value_string)`.

If the address is not in the MT table, `mnemonic` is `None` and a raw
fallback decode is used.

### `format_reply(reply) -> str`

Format a GET_SETTING reply as a human-readable string.

- Input: Raw reply bytearray.
- Output: `"MEM_CARD_ID: 12345"` or `"0x057E: 12345"` (unknown address).

### `format_reply_list(replies) -> list[str]`

Map `format_reply` over a list of reply bytearrays.

---

## 7. Event Types

### `RdStatusEvent` (Enum)

Session-level events fired to status listeners:

| Event | Meaning |
|-------|---------|
| `CONNECTED` | Controller responding to pings |
| `DISCONNECTED` | Ping/query timeout or transport drop |
| `RECONNECTED` | Connection auto-restored after failure |
| `TERMINATED` | Session explicitly shut down by `stop()` |
| `BLOCKED` | Status monitoring blocked for command flow |
| `UNBLOCKED` | Status monitoring resumed |
| `SCRIPT_ERROR` | Script encoding/parsing/execution error |
| `PING_SENT` | Ping command transmitted |
| `PING_REPLIED` | Ping acknowledgment received |
| `QUERY_SENT` | Status query commands transmitted |
| `QUERY_RECEIVED` | Status query replies received |
<!-- table not formatted: invalid structure -->

### `StatusDict` (TypedDict)

A dictionary of changed machine status values. Only keys whose values have
changed since the last update are present. Non-bool values are
`(raw_value, formatted_string)` tuples.

`MACHINE_STATUS` carries the raw status bitfield (`tuple[int, str]`); its
decoded bool flags `MACHINE_STATUS_MOVING`, `MACHINE_STATUS_LAYER_END`, and
`MACHINE_STATUS_JOB_RUNNING` appear as separate keys when their bits change.
Dimension values (`POSITION_*`, `BED_SIZE_*`) are `tuple[float, str]` with
the float in millimeters.

Formatted strings keep their Ruida prefixes (e.g. `"X=12.345mm"`,
`"CardID:..."`, `"MStat:..."`) — they are display-only and unchanged.

```python
class StatusDict(TypedDict, total=False):
    """Status update dict sent from RdDriver to status listeners.

    All fields are optional — only keys that changed are present.
    Non-bool values are (raw_value, formatted_string) tuples.

    MACHINE_STATUS carries the raw status bitfield (tuple[int, str]);
    its decoded bool flags MACHINE_STATUS_MOVING / _LAYER_END /
    _JOB_RUNNING appear as separate keys when their bits change.
    Dimension values (POSITION_*, BED_SIZE_*) are tuple[float, str]
    with the float in mm.
    """

    POSITION_X: tuple[float, str]
    POSITION_Y: tuple[float, str]
    POSITION_Z: tuple[float, str]
    POSITION_U: tuple[float, str]
    CARD_ID: tuple[int, str]
    BED_SIZE_X: tuple[float, str]
    BED_SIZE_Y: tuple[float, str]
    MACHINE_STATUS: tuple[int, str]
    MACHINE_STATUS_MOVING: bool
    MACHINE_STATUS_LAYER_END: bool
    MACHINE_STATUS_JOB_RUNNING: bool
```

---

## 8. Threading Model

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

### Key threading rules:

1. **`start()` and `stop()` are blocking** — `stop()` joins the runner thread with 2s timeout and disconnects synchronously.
2. **`run()` is non-blocking** — appends to a `queue.Queue`; the runner thread processes asynchronously.
3. **Listener callbacks fire from background threads** — applications must use thread-safe dispatch for UI updates.
4. **All listener forwarding uses copy-on-iterate** under `RLock` — safe to register listeners from any thread.
5. **Each listener callback is individually guarded** — one bad callback cannot crash the notification thread.

---

## 9. Error Handling Reference

| Condition | Behavior |
|-----------|----------|
| `start()` with empty/unreachable host | Returns `False`; status monitor retries in background |
| `start()` with different params than prior call | Calls `stop()` first, then creates fresh session |
| `run()` before `start()` | Raises `RuntimeError("Script runner not started. Call start() first.")` |
| `run([])` (empty script) | Silent no-op |
| `run_job()` with `job=None` and nothing staged | Raises `RuntimeError("No gluescript staged. Call stage_gluescript() first.")` |
| `stage_gluescript()` before `end_job()` | Raises `RuntimeError("end_job() must be called before stage_gluescript()")` |
| Re-staging a transcript missing `end_job()` (`require_complete=True`) | Raises `RuntimeError("Re-staged gluescript is missing end_job() — job was not completed")` |
| Script encoding error | Fires `SCRIPT_ERROR` + error listener; continues to next script |
| Transport disconnect mid-script | Re-queues full script; fires `DISCONNECTED` |
| `cancel_script()` during execution | Clears queue; current script iteration won't requeue |
| `END_JOB` mismatch + `auto_checksum=False` | Raises `ValueError` with expected/actual values |
| `END_JOB` mismatch + `auto_checksum=True` | Auto-recalculates checksum; logs warning; continues |
| Duplicate `END_JOB` | Raises `ValueError("Duplicate END_JOB")` |
| Listener callback raises exception | Caught by `except Exception: pass`; other listeners unaffected |

---

## 10. Integration Examples

### Minimal Integration

```python
from ruidadriver.ruida_driver import RdDriver

driver = RdDriver()
driver.register_status_listener(lambda e: print(f"[STATUS] {e}"))
driver.register_error_listener(lambda m: print(f"[ERROR] {m}"))

if not driver.start(udp_host="192.168.1.100"):
    print("Connection will retry in background...")

driver.run(["GET_SETTING MEM_CARD_ID"])
driver.run(["GET_SETTING MEM_MACHINE_STATUS"])
driver.stop()
```

### Full Integration with Event Handling

```python
import time
from ruidadriver.ruida_driver import RdDriver, RdStatusEvent, StatusDict

class MyApp:
    def __init__(self):
        self.driver = RdDriver()
        self.driver.register_status_listener(self._on_status)
        self.driver.register_error_listener(self._on_error)

    def _on_status(self, event: RdStatusEvent | StatusDict) -> None:
        if isinstance(event, RdStatusEvent):
            print(f"Session: {event.value}")
            if event == RdStatusEvent.CONNECTED:
                self.driver.run([
                    "MOVE_FAR_XY X=100mm Y=200mm",
                    "LASER_ON Power=80%",
                ])
        else:
            # StatusDict — machine status changed
            for key, (raw, formatted) in event.items():
                if not isinstance(raw, bool):  # skip bool bit keys
                    print(f"  {key}: {formatted}")

    def _on_error(self, message: str) -> None:
        print(f"Error: {message}")

    def run(self, host: str) -> None:
        if self.driver.start(udp_host=host):
            time.sleep(3)
            self.driver.stop()

MyApp().run("192.168.1.100")
```

### TUI Integration (Textual)

In a Textual application, use `call_from_thread()` to bridge background
thread callbacks to the asyncio event loop:

```python
def on_status_event(self, event: RdStatusEvent | StatusDict) -> None:
    self.call_from_thread(self._handle_status, event)

def _handle_status(self, event):
    # Runs on the asyncio thread — safe to update widgets
    self.status_log.write(f"[STATUS] {event}")
```

---

## 11. Configuration Notes

- **Transport:** UDP (Ethernet) is default. USB (serial via pyserial) is
  optional — pass `usb_device=` instead of or in addition to `udp_host=`.
- **Ping interval:** 5000ms default. Queries every 1000ms.
- **Timeouts:** Per-command timeout 250ms, gross timeout 15s for long
  operations (home sequences, etc.).
- **Connection retry:** Every 1000ms when not connected.
