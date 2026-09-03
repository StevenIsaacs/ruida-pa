# Ruida Driver — Test Plan (L4–L7)

> **Status:** Draft  
> **Document type:** Test plan  
> **See also:** [`RuidaDriver-functional.md`](./RuidaDriver-functional.md) for behavioral specs, [`RuidaDriver-design.md`](./RuidaDriver-design.md) for architectural intent

---

## 1. Overview & Setup

### 1.1 Purpose

Verify the complete L4–L7 stack of the Ruida Driver implementation, covering:

- **L4 — Transport Layer**: UDP/USB packing/unpacking, handshake state machine, swizzle/checksum
- **L5 — Session Layer**: Connection lifecycle, status monitor state machine, auto-reconnect
- **L6 — Driver Layer**: Script parsing/encoding, command execution, file checksum logic, machine status tracking
- **L7 — Application Layer**: TUI interactive testing, AppAdapter interface compliance

### 1.2 Environment Setup

| Component | Detail |
|-----------|--------|
| **Python virtual environment** | `source .venv/bin/activate` (required for `rpa-script --tui`) |
| **Primary test tool** | `rpa-script --tui` (interactive TUI for session management, command entry, status/reply display) |
| **Secondary test tool** | `rpa` (protocol analyzer for capture/decode of UDP traffic) |
| **Debugger** | VSCode (`launch.json` available at `.vscode/launch.json`) |
| **Controller** | Ruida RDC6442S (or similar) on known IP, e.g. `192.168.1.100`, reachable via Ethernet |
| **USB (optional)** | Ruida controller connected via USB serial, device e.g. `/dev/ttyUSB0` |
| **Network capture (optional)** | `tshark` via the `./capture <ip> <file>` Bash script |

### 1.3 Test Environment Assumptions

- A Ruida controller is available on the local network at a known IP address (e.g. `192.168.1.100`).
- The controller is powered on and idle (not executing a job).
- For USB tests, the controller is connected via USB and the device file is known (e.g. `/dev/ttyUSB0`).
- No other application is communicating with the controller during testing.
- The Python virtual environment (`.venv`) is active for all `rpa-script` commands.

---

## 2. L4 — Transport Layer Testing

### 2.1 Automated (Offline) — UDP Packing/Unpacking Verification [ ]

These tests require no controller. They verify the core wire-format logic using known byte sequences.

#### Test 2.1.1: `_package()` — Swizzle Output
- [x] Test 2.1.1

| Field | Value |
|-------|-------|
| **Objective** | Verify that `_package()` produces correct swizzled output for a given raw command bytearray. |
| **Prerequisites** | `RdTransport` instance configured for UDP transport (no socket needed — call `_package()` directly on test data). |
| **Steps** | 1. Prepare a known raw command bytearray (e.g., a `CORE NOP` command).<br>2. Call `_package(data)`.<br>3. Compare the output with the expected swizzled byte sequence. |
| **Expected result** | The output matches the expected swizzled sequence. |

#### Test 2.1.2: `_package()` — Checksum Computation (UDP)
- [x] Test 2.1.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that the 2-byte big-endian checksum prepended to the swizzled data is correct: `sum(swizzled_data) & 0xFFFF`. |
| **Prerequisites** | As above. |
| **Steps** | 1. Call `_package(data)` on a known bytearray.<br>2. Extract the first 2 bytes and interpret as `struct.unpack(">H", ...)`.<br>3. Compute the expected checksum from the swizzled payload. |
| **Expected result** | The extracted checksum matches the expected value. |

#### Test 2.1.3: `_package()` — No Checksum for USB
- [x] Test 2.1.3

| Field | Value |
|-------|-------|
| **Objective** | Verify that `_package()` does NOT prepend a checksum when the active transport is USB. |
| **Prerequisites** | `RdTransport` configured with USB transport. |
| **Steps** | 1. Call `_package(data)`.<br>2. Verify the output is the swizzled bytes only (no leading 2 bytes). |
| **Expected result** | Output length equals `len(swizzled(data))`. |

#### Test 2.1.4: `_unpack_replies()` — 9-Byte Splitting and 0xDA Validation
- [x] Test 2.1.4

| Field | Value |
|-------|-------|
| **Objective** | Verify that `_unpack_replies()` correctly unswizzles, splits into 9-byte chunks, and validates the 0xDA header. |
| **Prerequisites** | Known swizzled reply data containing one or more valid 9-byte GET_SETTING replies. |
| **Steps** | 1. Prepare swizzled bytes representing two valid 9-byte replies (18 bytes total).<br>2. Call `_unpack_replies(data)`.<br>3. Verify that a list of two `bytearray` objects is returned, each 9 bytes long.<br>4. Verify the first byte of each chunk is `0xDA`. |
| **Expected result** | Two valid 9-byte replies returned. First byte of each is `0xDA`. |

#### Test 2.1.5: `_unpack_replies()` — Invalid Header Truncation
- [x] Test 2.1.5

| Field | Value |
|-------|-------|
| **Objective** | Verify that data with an invalid first byte (not `0xDA`) causes truncation and fires `REPLY_ERROR`. |
| **Prerequisites** | Known swizzled data where the first byte of the first chunk is **not** `0xDA`. |
| **Steps** | 1. Prepare such data.<br>2. Call `_unpack_replies(data)`.<br>3. Verify `REPLY_ERROR` event was fired to status listeners.<br>4. Verify the returned list is empty or contains only valid chunks before the error. |
| **Expected result** | Error event fired. No valid replies returned (or only those before the corrupt chunk). |

#### Test 2.1.6: `_unpack_replies()` — Unexpected Second Byte
- [x] Test 2.1.6

| Field | Value |
|-------|-------|
| **Objective** | Verify that a chunk with valid `0xDA` header but non-`0x01` second byte fires `UNEXPECTED_REPLY` and is truncated. |
| **Prerequisites** | Swizzled data where a chunk starts with `0xDA` but byte 2 is not `0x01`. |
| **Steps** | 1. Prepare such data.<br>2. Call `_unpack_replies(data)`.<br>3. Verify `UNEXPECTED_REPLY` event was fired.<br>4. Verify the chunk is excluded from the returned list. |
| **Expected result** | `UNEXPECTED_REPLY` fired. Invalid chunk excluded. |

#### Test 2.1.7: `_has_get_setting()` — GET_SETTING Detection
- [x] Test 2.1.7

| Field | Value |
|-------|-------|
| **Objective** | Verify that `_has_get_setting()` correctly detects the two-byte marker `0xDA 0x01` in raw (unswizzled) packet bytes. |
| **Prerequisites** | Raw command bytearrays with and without `GET_SETTING` commands. |
| **Steps** | 1. Prepare a packet containing a `GET_SETTING` command → call `_has_get_setting()` → expect `True`.<br>2. Prepare a packet with no `GET_SETTING` command → call `_has_get_setting()` → expect `False`. |
| **Expected result** | Detection matches expectations. |

---

### 2.2 Manual (With Controller) — Handshake Thread State Machine [ ]

#### Test 2.2.1: Session Start → Handshake State Transitions
- [x] Test 2.2.1

| Field | Value |
|-------|-------|
| **Objective** | Verify the handshake thread transitions through IDLE → SEND → ACK_PENDING → REPLY_PENDING → IDLE when a ping is sent over UDP. |
| **Prerequisites** | Controller on network at `192.168.1.100`. `.venv` active. |
| **Steps** | 1. Launch TUI: `rpa-script --tui`.<br>2. Type `session start udp=192.168.1.100` and press Enter.<br>3. Observe the TUI status log for `PING_SENT` and `PING_REPLIED` events.<br>4. Optionally: Set a breakpoint in `rd_transport.py` handshake thread to step through IDLE → SEND → ACK_PENDING → REPLY_PENDING transitions. |
| **Expected result** | Status log shows `PING_SENT` followed by `PING_REPLIED` within ~1s. No timeout errors. |

#### Test 2.2.2: Ping Failure → DISCONNECTED → RECONNECTED
- [x] Test 2.2.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that disconnecting the Ethernet cable causes the handshake thread to detect failure and fire `DISCONNECTED`, and reconnecting restores the session with `RECONNECTED`. |
| **Prerequisites** | Active session as in Test 2.2.1. |
| **Steps** | 1. While TUI is running with an active session, unplug the Ethernet cable from the controller or the host.<br>2. Observe the status log.<br>3. After a few seconds, reconnect the cable.<br>4. Observe the status log. |
| **Expected result** | Within ~5–15s of disconnection, `DISCONNECTED` appears in the log. Within ~5s of reconnection, `RECONNECTED` appears, followed by `PING_SENT` / `PING_REPLIED`. |

#### Test 2.2.3: Transport Open → Capture Swizzled Packets
- [x] Test 2.2.3

| Field | Value |
|-------|-------|
| **Objective** | Verify that `rpa` capture shows swizzled packets with correct checksums when a session is active. |
| **Prerequisites** | Controller on network. `rpa` installed (part of project). |
| **Steps** | 1. Start a UDP session: `rpa-script --tui`, then `session start udp=192.168.1.100`.<br>2. Simultaneously run `./capture 192.168.1.100 session-test.log` in another terminal.<br>3. After capturing a few pings, stop the capture.<br>4. Run `rpa session-test.log` and examine the decoded output. |
| **Expected result** | Decoded packets show the expected command bytes. The swizzle/checksum can be verified by cross-referencing the raw hex dump in `session-test-vrb.tshark`. |

---

## 3. L5 — Session Layer Testing

### 3.1 Automated (Offline) — RdSession Lifecycle [ ]

#### Test 3.1.1: `RdSession.connect()` with Mock Transport (Not Connected)
- [x] Test 3.1.1

| Field | Value |
|-------|-------|
| **Objective** | Verify that `connect()` returns `False` when the transport cannot be opened. |
| **Prerequisites** | A mock or unconfigured `RdSession` (no controller available). |
| **Steps** | 1. Create an `RdSession` instance.<br>2. Do not configure or start any transport.<br>3. Call `session.connect(timeout=1000)`. |
| **Expected result** | Returns `False`. |

#### Test 3.1.2: `RdSession.disconnect()` Idempotency
- [x] Test 3.1.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that calling `disconnect()` multiple times does not raise exceptions or cause hangs. |
| **Prerequisites** | `RdSession` instance (connected state not required). |
| **Steps** | 1. Call `session.disconnect()`.<br>2. Call `session.disconnect()` again.<br>3. Call `session.disconnect()` a third time. |
| **Expected result** | All calls return without error. No exceptions raised. |

#### Test 3.1.3: `RdSession.is_connected` Property Logic
- [x] Test 3.1.3

| Field | Value |
|-------|-------|
| **Objective** | Verify `is_connected` returns `False` after construction, `True` after successful connect, `False` after disconnect. |
| **Prerequisites** | Controller available OR mock transport. |
| **Steps** | 1. Create `RdSession` → verify `is_connected` is `False`.<br>2. Call `session.connect()` → verify `is_connected` is `True` (or `False` if mock).<br>3. Call `session.disconnect()` → verify `is_connected` is `False`. |
| **Expected result** | Property tracks connection state correctly. |

---

### 3.2 Manual (With Controller) — Status Monitor State Machine [ ]

#### Test 3.2.1: Status Monitor CONNECTED → PING Cycle
- [x] Test 3.2.1

| Field | Value |
|-------|-------|
| **Objective** | Verify the 8-state status monitor transitions through CONNECTING → WAIT_TO_PING → SEND_PING → PING_REPLY → WAIT_TO_POLL, with `CONNECTED`, `PING_SENT`, `PING_REPLIED` events appearing in the TUI. |
| **Prerequisites** | Controller at `192.168.1.100`. `.venv` active. |
| **Steps** | 1. `rpa-script --tui`.<br>2. `session start udp=192.168.1.100`.<br>3. Observe the TUI status log. |
| **Expected result** | Within seconds: `CONNECTED`, then periodic `PING_SENT` / `PING_REPLIED` pairs cycling every ~5s. No `DISCONNECTED` or timeout events. |

#### Test 3.2.2: Query Cycle — QUERY_SENT / QUERY_RECEIVED
- [x] Test 3.2.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that after connection, the status monitor sends query commands and processes their replies. |
| **Prerequisites** | Active session as in Test 3.2.1. |
| **Steps** | 1. With an active session, observe the TUI status log for approximately 10–15 seconds.<br>2. Look for `QUERY_SENT` and `QUERY_RECEIVED` events. |
| **Expected result** | `QUERY_SENT` followed by `QUERY_RECEIVED` appears periodically (every ~1s by default). Within each cycle, `PING_SENT`/`PING_REPLIED` and `QUERY_SENT`/`QUERY_RECEIVED` alternate. |

#### Test 3.2.3: Cable Disconnect → DISCONNECTED → RECONNECTED
- [x] Test 3.2.3

| Field | Value |
|-------|-------|
| **Objective** | Verify the status monitor's `DISCONNECTED` and `RECONNECTED` events on transport drop and restore. |
| **Prerequisites** | Active session. |
| **Steps** | 1. While TUI is running with active session, unplug Ethernet cable.<br>2. Observe status log — `DISCONNECTED` should appear within ~5–15s.<br>3. Reconnect cable.<br>4. Observe status log — `RECONNECTED` should appear within ~5s. |
| **Expected result** | `DISCONNECTED` emitted on transport drop; `RECONNECTED` emitted once session is re-established. |

#### Test 3.2.4: Block / Unblock Mechanism
- [x] Test 3.2.4

| Field | Value |
|-------|-------|
| **Objective** | Verify that calling `block()` prevents status queries from interleaving with command flow, and `unblock()` resumes them. |
| **Prerequisites** | Active session. Access to Python REPL or a test script with access to `RdStatus`. |
| **Steps** | 1. In an active session, call `status.block()`.<br>2. Send a command (e.g., `GET_SETTING MEM_CARD_ID`).<br>3. Observe that no `QUERY_SENT` events appear during the block.<br>4. Call `status.unblock()`.<br>5. Observe that `QUERY_SENT` events resume. |
| **Expected result** | No status queries during block; queries resume after unblock. |

#### Test 3.2.5: Status Panel — Address:Value Display
- [x] Test 3.2.5

| Field | Value |
|-------|-------|
| **Objective** | Verify that the TUI side panel shows decoded address:value pairs for `MEM_MACHINE_STATUS` (0x0400), `MEM_CARD_ID` (0x057E), bed size, and position. |
| **Prerequisites** | Active session. |
| **Steps** | 1. After session start, wait for at least one query cycle.<br>2. Examine the TUI reply panel (right side). |
| **Expected result** | Reply panel shows entries like `0x0400: <status_value>`, `0x057E: <card_id>`, `0x...X/Y/Z/U: <position_value>`. Values are decoded integers (not raw bytes). |

---

## 4. L6 — Driver Layer Testing

### 4.1 Automated (Offline) — Script Parsing and Encoding [ ]

#### Test 4.1.1: `ScriptParser.parse_lines()` — Known Good Input
- [x] Test 4.1.1

| Field | Value |
|-------|-------|
| **Objective** | Verify that `ScriptParser.parse_lines()` correctly parses known good `.rds` script lines into command dicts. |
| **Prerequisites** | A known good `.rds` script (e.g., containing `CORE NOP`, `MOVE MOVE_FAR_XY X=100.000mm Y=200.000mm`). |
| **Steps** | 1. Create a list of `.rds` lines.<br>2. Call `ScriptParser.parse_lines(lines)`.<br>3. Examine the returned command dicts. |
| **Expected result** | Each line is correctly parsed into a dict with keys: `type`, `mnemonic`, `params`, etc. Values match input. |

#### Test 4.1.2: `encode_command()` — Expected Byte Sequences
- [x] Test 4.1.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that `encode_command()` produces the correct bytearray for known commands. |
| **Prerequisites** | A parsed command dict from Test 4.1.1. |
| **Steps** | 1. For a command like `CORE NOP`, parse and then encode.<br>2. Compare the resulting bytearray against the expected byte sequence from the protocol definition. |
| **Expected result** | Encoded bytearray matches the expected sequence (e.g., `NOP` → known opcode bytes). |

#### Test 4.1.3: File Checksum — Helper Functions
- [x] Test 4.1.3

| Field | Value |
|-------|-------|
| **Objective** | Verify `should_include_in_checksum()`, `is_set_file_sum()`, and `is_eof_command()` return correct values. |
| **Prerequisites** | Known command dicts. |
| **Steps** | 1. Test each function with commands that should and should not match.<br>2. Verify `should_include_in_checksum()` returns `False` for `END_JOB`, `KEYPRESS`, and `SETTING` commands.<br>3. Verify `is_set_file_sum()` returns `True` for `END_JOB` commands only.<br>4. Verify `is_eof_command()` returns `True` for end-of-file commands. |
| **Expected result** | All helper functions return correct boolean values. |

#### Test 4.1.4: `reconstruct_script_line()` — Round-Trip
- [x] Test 4.1.4

| Field | Value |
|-------|-------|
| **Objective** | Verify that a script line survives a parse → reconstruct round-trip without data loss. |
| **Prerequisites** | A parsed command dict. |
| **Steps** | 1. Parse a script line.<br>2. Call `reconstruct_script_line(cmd)`.<br>3. Compare the reconstructed text with the original. |
| **Expected result** | The reconstructed line matches the original (modulo whitespace normalization). |

#### Test 4.1.5: Duplicate `END_JOB` Raises `ValueError`
- [x] Test 4.1.5

| Field | Value |
|-------|-------|
| **Objective** | Verify that a script containing two `END_JOB` commands raises `ValueError`. |
| **Prerequisites** | A script list with two `END_JOB` lines. |
| **Steps** | 1. Create script lines containing two `END_JOB` commands.<br>2. Run through the encoding pipeline.<br>3. Verify `ValueError` is raised. |
| **Expected result** | `ValueError("Duplicate END_JOB — at most one per file")`. |

#### Test 4.1.6: Checksum Mismatch Raises `ValueError`
- [x] Test 4.1.6

| Field | Value |
|-------|-------|
| **Objective** | Verify that a `END_JOB` with a value that does not match the accumulated checksum raises `ValueError`. |
| **Prerequisites** | A script with commands whose accumulated checksum is known. |
| **Steps** | 1. Create a script with a `END_JOB = <wrong_value>`.<br>2. Run through the encoding pipeline.<br>3. Verify `ValueError` is raised with both expected and actual values. |
| **Expected result** | `ValueError` raised with mismatch detail. |

---

### 4.2 Manual (With Controller) — Command Execution [ ]

#### Test 4.2.1: `GET_SETTING MEM_CARD_ID` — Reply in TUI
- [x] Test 4.2.1

| Field | Value |
|-------|-------|
| **Objective** | Verify that a manual `GET_SETTING MEM_CARD_ID` command produces a reply displayed in the TUI side panel. |
| **Prerequisites** | Active session (see Test 3.2.1). |
| **Steps** | 1. With active TUI session, type `GET_SETTING MEM_CARD_ID` in the command input and press Enter.<br>2. Observe the reply panel. |
| **Expected result** | Reply panel shows an entry for address `0x057E` with a decoded integer value (the card ID). |

#### Test 4.2.2: `GET_SETTING MEM_MACHINE_STATUS` — Status Bits
- [x] Test 4.2.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that `MEM_MACHINE_STATUS` reply is decoded into status events (`MOVING`, `PAUSED`, `JOB_RUNNING`). |
| **Prerequisites** | Active session. |
| **Steps** | 1. With active TUI session, type `GET_SETTING MEM_MACHINE_STATUS` and press Enter.<br>2. Observe the status log and side panel. |
| **Expected result** | Status log shows events for `MACHINE_STATUS_MOVING`, `MACHINE_STATUS_PAUSED`, and/or `MACHINE_STATUS_JOB_RUNNING` reflecting the current machine state. Reply panel shows `0x0400: <decoded_value>`. |

#### Test 4.2.3: Simple Move (Jog) Command
- [x] Test 4.2.3

| Field | Value |
|-------|-------|
| **Objective** | Verify that executing a move command causes the machine to move as expected. |
| **Prerequisites** | Active session. **WARNING:** Ensure safe operation — verify no mechanical obstructions, set safe speed, have emergency stop accessible. |
| **Steps** | 1. With active TUI session, type `MOVE MOVE_FAR_XY X=10.000mm Y=10.000mm` and press Enter.<br>2. Observe the controller's physical response.<br>3. Verify the TUI log shows no errors. |
| **Expected result** | The machine moves to (10, 10) or as close as mechanically possible. No timeout or error events in the log. |

#### Test 4.2.4: Queue Multiple Commands
- [x] Test 4.2.4

| Field | Value |
|-------|-------|
| **Objective** | Verify that multiple queued commands execute in order. |
| **Prerequisites** | Active session. Safe mechanical environment. |
| **Steps** | 1. With active TUI session, type `MOVE MOVE_FAR_XY X=100.000mm Y=100.000mm` and press Enter.<br>2. Immediately type `MOVE MOVE_FAR_XY X=200.000mm Y=200.000mm` and press Enter.<br>3. Observe the machine moves to (100, 100) first, then to (200, 200). |
| **Expected result** | Commands execute sequentially in the order they were entered. |

#### Test 4.2.5: [Optional] Load and Execute `.rds` File via Slash Commands
- [ ] Test 4.2.5

| Field | Value |
|-------|-------|
| **Objective** | Verify that a pre-recorded `.rds` file can be loaded via `/load` and executed via `/exec`. |
| **Prerequisites** | Active session. A pre-recorded `.rds` file available (e.g., at `tmp/test-script.rds`). |
| **Steps** | 1. With active TUI, type `/load tmp/test-script.rds` and press Enter.<br>2. Observe the log — should show `Loaded N lines from tmp/test-script.rds`.<br>3. Type `/exec` and press Enter.<br>4. Observe the TUI log and machine behavior. |
| **Expected result** | The script loads without error. Execution produces expected commands in the log. Machine behaves as expected. |

---

## 5. L7 — Application Layer Testing

### 5.1 Automated (Offline) — AppAdapter Interface [ ]

#### Test 5.1.1: AppAdapter ABC Cannot Be Instantiated
- [ ] Test 5.1.1

| Field | Value |
|-------|-------|
| **Objective** | Verify that `AppAdapter` cannot be instantiated directly (abstract class). |
| **Prerequisites** | Import `AppAdapter` from `rpalib.app_adapter`. |
| **Steps** | 1. Attempt `AppAdapter()`. |
| **Expected result** | `TypeError` raised: "Can't instantiate abstract class AppAdapter with abstract methods...". |

#### Test 5.1.2: TuiAdapter Has All AppAdapter Interface Methods
- [ ] Test 5.1.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that `TuiAdapter` implements all required AppAdapter methods. |
| **Prerequisites** | Import `TuiAdapter` from `rpascript.tui_adapter`. |
| **Steps** | 1. Call `dir(TuiAdapter)` and check for method names: `create_driver_and_session`, `on_status_event`, `on_reply_data`, `on_error`, `run_script`, `start`, `stop`. |
| **Expected result** | All seven methods are present. |

#### Test 5.1.3: `run_tui()` Is Callable
- [ ] Test 5.1.3

| Field | Value |
|-------|-------|
| **Objective** | Verify that `run_tui()` is a callable function (does not test the actual TUI launch). |
| **Prerequisites** | Import `run_tui` from `rpascript.tui_adapter`. |
| **Steps** | 1. Verify `callable(run_tui)` returns `True`. |
| **Expected result** | `run_tui` is callable. |

---

### 5.2 Manual — TUI Interactive Testing [ ]

#### Test 5.2.1: Launch TUI
- [ ] Test 5.2.1

| Field | Value |
|-------|-------|
| **Objective** | Verify the TUI launches without errors. |
| **Prerequisites** | `.venv` active. |
| **Steps** | 1. Run: `python -m rpascript --tui` or `./.venv/bin/rpa-script --tui`.<br>2. Observe the terminal. |
| **Expected result** | TUI appears with Header, log area, command input, side panel, and Footer. No traceback or crash. |

#### Test 5.2.2: Verify TUI Layout
- [ ] Test 5.2.2

| Field | Value |
|-------|-------|
| **Objective** | Verify the TUI layout matches the spec: Header, log area (left), side panel (right), command input (bottom), Footer with key bindings. |
| **Prerequisites** | TUI running. |
| **Steps** | 1. Visually inspect the TUI layout.<br>2. Confirm the side panel has three sections: status events (top), reply data (middle), counters (bottom).<br>3. Confirm the Footer shows `Ctrl+C Quit` (the sole remaining key binding). |
| **Expected result** | Layout matches the specification in Section 6.2.1 of the functional spec. |

#### Test 5.2.3: Session Start via TUI
- [ ] Test 5.2.3

| Field | Value |
|-------|-------|
| **Objective** | Verify `session start udp=192.168.1.100` in the TUI produces a valid connection. |
| **Prerequisites** | TUI running. Controller at `192.168.1.100`. |
| **Steps** | 1. In the command input, type `session start udp=192.168.1.100` and press Enter.<br>2. Observe the log area — should show `[STATUS] CONNECTED`.<br>3. Observe the side panel — status section should show events. |
| **Expected result** | Log shows `[STATUS] CONNECTED`. Side panel populates with status events. No error messages. |

#### Test 5.2.4: Command via TUI → Reply Data
- [ ] Test 5.2.4

| Field | Value |
|-------|-------|
| **Objective** | Verify that entering a command produces decoded reply data in the side panel. |
| **Prerequisites** | Active session in TUI. |
| **Steps** | 1. Type `GET_SETTING MEM_CARD_ID` and press Enter.<br>2. Observe the reply section of the side panel. |
| **Expected result** | Reply panel shows an entry with the decoded address and value. Event counters increment. |

#### Test 5.2.5: Session End — Cleanup
- [ ] Test 5.2.5

| Field | Value |
|-------|-------|
| **Objective** | Verify `session end` produces proper cleanup. |
| **Prerequisites** | Active session in TUI. |
| **Steps** | 1. Type `session end` and press Enter.<br>2. Observe the log. |
| **Expected result** | Log shows "Session ended" or similar. No error traceback. Session state is clean. |

#### Test 5.2.6: Ctrl+C — Clean Exit
- [ ] Test 5.2.6

| Field | Value |
|-------|-------|
| **Objective** | Verify that Ctrl+C exits the TUI without exception traceback. |
| **Prerequisites** | TUI running (with or without session). |
| **Steps** | 1. Press Ctrl+C.<br>2. Observe terminal after exit. |
| **Expected result** | TUI closes cleanly. No traceback or exception message printed to terminal. Exit code 0. |

#### Test 5.2.7: Removed Key Bindings Are Inert
- [ ] Test 5.2.7

| Field | Value |
|-------|-------|
| **Objective** | Verify that removed key bindings (F1) have no effect. Ctrl+L and Ctrl+E are also inert — their functionality has been replaced by the `/load` and `/exec` slash commands. |
| **Prerequisites** | TUI running |
| **Steps** | 1. Press F1.<br>2. Press Ctrl+L.<br>3. Press Ctrl+E.<br>4. Observe the log area and verify no action occurs |
| **Expected result** | No help text, file picker, or script execution occurs on any of the three key presses. The TUI remains stable. Textual may log a warning about unbound keys to stderr. The equivalent functionality is now available via the `/load` and `/exec` text-input commands (see §5.4). |

### 5.3 Introspection Testing [ ]

#### Test 5.3.1: Variable View — `!session`
- [ ] Test 5.3.1

| Field | Value |
|-------|-------|
| **Objective** | Verify that `!session` displays the repr of the session object |
| **Prerequisites** | Active session in TUI |
| **Steps** | 1. With active TUI session, type `!session` and press Enter |
| **Expected result** | Log shows `<ruidadriver.rd_session.RdSession object at 0x...>` (or similar repr) |

#### Test 5.3.2: Method Call — `!transport._package test_data`
- [ ] Test 5.3.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that calling a method via introspection works and shows repr of result |
| **Prerequisites** | Active session in TUI |
| **Steps** | 1. With active TUI session, type `!transport._package 0xAA` and press Enter |
| **Expected result** | Log shows the swizzled bytearray repr (e.g., `bytearray(b'\xaa\x88...')`) |

#### Test 5.3.3: Signature Display — Method without args
- [ ] Test 5.3.3

| Field | Value |
|-------|-------|
| **Objective** | Verify that referencing a method without parentheses shows its signature |
| **Prerequisites** | TUI running (session not required) |
| **Steps** | 1. Type `!decoder.decode_address` and press Enter |
| **Expected result** | Log shows `<Signature (data: bytearray) -> int>` |

#### Test 5.3.4: Error Handling — Unknown object
- [ ] Test 5.3.4

| Field | Value |
|-------|-------|
| **Objective** | Verify that referencing an unknown object logs a helpful error |
| **Prerequisites** | TUI running |
| **Steps** | 1. Type `!nonexistent` and press Enter |
| **Expected result** | Log shows error message listing known objects: `session, transport, driver, status, parser, decoder` |

### 5.4 Slash-Command Testing [ ]

#### Test 5.4.1 — `/help` Displays Help
- [ ] Test 5.4.1

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/help` displays help text covering `/` commands, `!` introspection, and Ruida/session commands |
| **Prerequisites** | TUI running |
| **Steps** | 1. Type `/help` and press Enter |
| **Expected result** | Help text is displayed in the log area. The text contains `TUI Commands`, `Introspection`, and `Ruida Commands` section headers. |

#### Test 5.4.2 — `?` Displays Same Help
- [ ] Test 5.4.2

| Field | Value |
|-------|-------|
| **Objective** | Verify that `?` displays the same help text as `/help` |
| **Prerequisites** | TUI running |
| **Steps** | 1. Type `?` and press Enter |
| **Expected result** | Help text is displayed. Content matches the output of `/help` (same sections: `TUI Commands`, `Introspection`, `Ruida Commands`). |

#### Test 5.4.3 — `?` Not Recognized Mid-Line
- [ ] Test 5.4.3

| Field | Value |
|-------|-------|
| **Objective** | Verify that `?` is only treated as help when it is the entire input, not as a trailing character |
| **Prerequisites** | TUI running (session recommended but not required) |
| **Steps** | 1. Type `GET_SETTING?` (or any command with `?` appended) and press Enter |
| **Expected result** | The input is NOT treated as a help request. The log shows `[SCRIPT] GET_SETTING?` — it is forwarded as a raw rpascript command (and will fail on the controller, which is expected). |

#### Test 5.4.4 — `/load` Loads Script File
- [ ] Test 5.4.4

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/load <path>` loads a script file into memory |
| **Prerequisites** | A valid script file exists at `tmp/test-script.rds` |
| **Steps** | 1. Type `/load tmp/test-script.rds` and press Enter<br>2. Observe the log |
| **Expected result** | Log shows `Loaded N lines from tmp/test-script.rds` where `N` is the number of lines read. |

#### Test 5.4.5 — `/load` With No Path
- [ ] Test 5.4.5

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/load` with no argument shows an error message |
| **Prerequisites** | TUI running |
| **Steps** | 1. Type `/load` and press Enter |
| **Expected result** | Log shows `Usage: /load <path>` error message. No file operation is attempted. |

#### Test 5.4.6 — `/load` File Not Found
- [ ] Test 5.4.6

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/load` with a nonexistent path shows an error message |
| **Prerequisites** | TUI running. No file at `nonexistent.rds`. |
| **Steps** | 1. Type `/load nonexistent.rds` and press Enter |
| **Expected result** | Log shows `File not found: nonexistent.rds` (or similar) error message. |

#### Test 5.4.7 — `/exec` With No Script Loaded
- [x] Test 5.4.7 ✅ 2026-06-06

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/exec` with no prior `/load` shows an error message |
| **Prerequisites** | TUI running. No script loaded via `/load`. |
| **Steps** | 1. Type `/exec` and press Enter |
| **Expected result** | Log shows `No script loaded` error message. |

#### Test 5.4.8 — `/exec` With No Active Session
- [ ] Test 5.4.8

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/exec` requires an active session |
| **Prerequisites** | TUI running. Script loaded via `/load tmp/test-script.rds`. Session NOT started (no `session start`). |
| **Steps** | 1. Type `/load tmp/test-script.rds` and press Enter (succeeds).<br>2. Type `/exec` and press Enter. |
| **Expected result** | Log shows `No active session` error message. No commands are sent to the controller. |

#### Test 5.4.9 — `/clear` Clears Logs
- [x] Test 5.4.9 ✅ 2026-06-06

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/clear` clears all log panels and the loaded script |
| **Prerequisites** | TUI running. Some log content exists (e.g., after `/help` or a command). Script loaded via `/load`. |
| **Steps** | 1. Generate some log content (e.g., type `/help`).<br>2. Type `/clear` and press Enter. |
| **Expected result** | Log area is empty. Any previously loaded script (`_loaded_script`) is cleared. Status and reply panels are not affected (they persist). |

#### Test 5.4.10 — `/quit` Exits TUI
- [x] Test 5.4.10 ✅ 2026-06-06

| Field | Value |
|-------|-------|
| **Objective** | Verify that `/quit` exits the TUI cleanly |
| **Prerequisites** | TUI running (with or without session). |
| **Steps** | 1. Type `/quit` and press Enter.<br>2. Observe terminal after exit. |
| **Expected result** | TUI closes cleanly. No traceback or exception message printed to terminal. Exit code 0. Returns to shell without error. |

#### Test 5.4.11 — Unknown Slash Command
- [x] Test 5.4.11 ✅ 2026-06-06

| Field | Value |
|-------|-------|
| **Objective** | Verify that an unknown `/` command shows an error message |
| **Prerequisites** | TUI running |
| **Steps** | 1. Type `/foobar` and press Enter |
| **Expected result** | Log shows `Unknown TUI command: /foobar` (or similar) error message. The TUI remains stable and responsive. |

#### Test 5.4.12 — Case Insensitivity of Slash Commands
- [x] Test 5.4.12 ✅ 2026-06-06

| Field | Value |
|-------|-------|
| **Objective** | Verify that slash commands are case-insensitive (`/HELP`, `/Help`, `/help` all work) |
| **Prerequisites** | TUI running |
| **Steps** | 1. Type `/HELP` and press Enter — verify help text appears.<br>2. Type `/Help` and press Enter — verify help text appears.<br>3. Type `/help` and press Enter — verify help text appears. |
| **Expected result** | All three variants produce the same help text with the same content. No error messages for any variant. |

---

## 6. Integration: Live Capture & Decode Verification

### Test 6.1: TUI + GET_SETTING + Simultaneous Capture [ ]

| Field | Value |
|-------|-------|
| **Objective** | Verify that commands sent from the TUI appear correctly in a simultaneous network capture. |
| **Prerequisites** | Controller on network. `rpa-script --tui` ready. `./capture` script available. |
| **Steps** | 1. Start a packet capture: `./capture 192.168.1.100 integration-test.log` (in another terminal).<br>2. In TUI, start a session: `session start udp=192.168.1.100`.<br>3. Send a command: `GET_SETTING MEM_CARD_ID`.<br>4. After reply appears, stop the capture.<br>5. Run `rpa integration-test.log` to decode. |
| **Expected result** | The decoded capture shows the GET_SETTING command followed by a reply. The command bytes match expectations. |

### Test 6.2: Capture Decode Verification [ ]

| Field | Value |
|-------|-------|
| **Objective** | Verify that `rpa.py decode` correctly interprets the captured traffic. |
| **Prerequisites** | Capture file from Test 6.1. |
| **Steps** | 1. Run `rpa integration-test.log`.<br>2. Examine the decoded output for the GET_SETTING command and its reply. |
| **Expected result** | Decoded output shows the command mnemonic (`GET_SETTING`), memory address (`MEM_CARD_ID`), and the reply value. |

### Test 6.3: Round-Trip — Script → Encode → Transmit → Capture → Decode [ ]

| Field | Value |
|-------|-------|
| **Objective** | End-to-end verification: an `.rds` script is encoded, sent to the controller, captured on the wire, and decoded back to the original script form. |
| **Prerequisites** | Controller on network. `rpa-script` CLI available. |
| **Steps** | 1. Create a simple `.rds` script file (e.g., `CORE NOP`).<br>2. Run `rpa-script script.rds -o tmp/encoded-output` to produce tshark-format output.<br>3. Pipe the tshark output through `rpa.py` to decode: `rpa tmp/encoded-output`.<br>4. Alternatively, send the script live and capture with `./capture`, then decode with `rpa.py`. |
| **Expected result** | Decoded output matches the original `.rds` commands (round-trip fidelity). |

---

## 7. Problem Reporting Procedure

When a test fails, follow this procedure to document the issue:

### 7.1 Capture TUI Log Output

- Take a screenshot or copy the TUI log text.
- If the TUI is still running, scroll back to capture all relevant log entries.
- Note the exact sequence of commands that led to the failure.

### 7.2 Record Session Details

| Field | Value |
|-------|-------|
| **`rpa-script --tui` command** | Full command line used to launch |
| **Session commands** | Exact text of each command entered |
| **Controller IP** | IP address of the Ruida controller |
| **Transport type** | UDP or USB |
| **Timestamp** | Date and time of test |

### 7.3 Note Unexpected TUI Behavior

Specific symptoms to document:

- Widgets not updating (log frozen, side panel stale, counters not incrementing).
- TUI hang / unresponsive input.
- Exception tracebacks (copy the full traceback text).
- Unexpected event sequences (e.g., `DISCONNECTED` without a cable event).

### 7.4 Protocol-Level Issues

If the issue appears to be at the protocol/wire level:

1. Capture UDP traffic with `tshark` using the `./capture` script:
   ```
   ./capture 192.168.1.100 problem-capture.log
   ```
2. Run the capture through `rpa.py`:
   ```
   rpa problem-capture.log
   ```
3. Compare the decoded output with expected command byte sequences.
4. Inspect the verbose output (`problem-capture-vrb.tshark`) for raw hex dumps.

### 7.5 Code-Level Issues

If the issue is in Python code:

1. Open the project in VSCode.
2. Set breakpoints in the relevant layer files:
   - **L4**: `ruidadriver/rd_transport.py`, `ruidadriver/transport/`
   - **L5**: `ruidadriver/rd_status.py`, `ruidadriver/rd_session.py`
   - **L6**: `ruidadriver/ruida_driver.py`, `rpascript/interpreter.py`, `rpascript/encoding.py`
   - **L7**: `rpascript/tui_adapter.py`, `rpalib/app_adapter.py`
3. Reproduce the failure under the debugger.
4. Capture stack traces and variable state.

### 7.5a Using Introspection for Diagnostics

The `!` introspection commands provide a powerful diagnostic tool for understanding the internal state of the driver stack during a test session:

- **Check connection state**: `!session.is_connected` — returns `True`/`False`
- **Inspect driver status**: `!driver.machine_status` — shows decoded address:value map
- **Examine transport state**: `!transport.is_open` — returns `True`/`False`
- **Test encoding directly**: `!transport._package 0xDA0100` — manually invoke wire-format packaging
- **Inspect any object**: Use `!<path>` to walk the object graph starting from: `session`, `transport`, `driver`, `status`, `parser`, `decoder`
- **Self-inspect**: Use `!self.<attribute>` to inspect TuiAdapter TUI internals (e.g., `!self._event_count`)

Introspection output appears in the main log area with `[INFO]` prefix. Errors (unknown objects, attribute errors, type errors) are logged as `[ERROR]` without crashing the TUI.

### 7.6 Report Template

When reporting a failure, include:

```
## Test Failure Report

### Test Reference
[Section.TestNumber] — [Test Name]

### Environment
- Controller IP: 192.168.1.100
- Transport: UDP/USB
- rpa-script version: [version]
- Python version: [version]
- OS: [OS name and version]

### Steps to Reproduce
1. ...
2. ...

### Actual Result
[What happened]

### Expected Result
[What should have happened]

### Artifacts
- TUI log: [attached]
- Packet capture: [attached]
- Stack trace: [attached]

### Suspected Layer
[L4 / L5 / L6 / L7 / Integration]
```

---

*End of test plan.*
