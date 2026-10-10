# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.23.0] - 2026-10-10

### Added

- `GlueScript.scan_rows()` — a run-chunked raster-fill for the MeerK40t
  integration plan: a raster job is emitted as roughly one rpascript line per
  power-run instead of ~3 lines per pixel. Consecutive equal-power pixels
  collapse into a single `CUT_*` (or `MOVE_*` for a zero-power run),
  `IMD_POWER_1`/`IMD_POWER_3` are change-gated through a `_current_imd_power`
  shared with `power()`, bidirectional scans advance with a cross-axis-only
  move, `horizontal=False` scans along Y, and the layer must be
  IMAGE/DEPTHMAP (`ValueError` otherwise).
- Canonical multi-line gluescript transcript serialization — a new
  `_format_gluescript_call`/`_render_literal`/`_record` serializer lets any
  gluescript command span physical lines for readability (a `scan_rows(...)`
  call is recorded as one logical command); `RpcRdDriver.sync()` joins
  continuation lines so wrapped commands re-stage correctly.
- `RdDriver.start()`'s network-host argument renamed `udp_host` → `network_host`
  (accurate for both UDP and TCP). `network_host` is primary; `udp_host`
  remains a **deprecated alias** emitting a `DeprecationWarning` (supplying
  both raises `ValueError`). Applied consistently across `RpcRdDriver.start`,
  `TuiAdapter.start`, `RpycTuiService.exposed_start`, and `RdTransport.open`.
- The `.rds`/TUI `session start` command now uses `host=` as primary, with
  `udp=` normalized as a deprecated alias; the internal
  `_start_udp_host`/`_last_udp_host`/`_udp_host` attributes were renamed to
  `_network_host`.
- The integration guide and API reference document the `protocol` argument of
  `start()` (UDP/TCP selection for port-50200 controllers such as the
  RDC8445S); the stale `start()` and `configure()` signatures in the docs were
  corrected.
- Per-axis resolution of the memory-table (MT) axis blocks: the X/Y/Z/U
  machine-setting mnemonics in the `0x002_`–`0x005_` blocks were renamed from
  numbered `MEM_AXIS_*_1..4` to axis-suffixed canonical names (`MEM_PRECISION_X..U`,
  `MEM_MAX_VELOCITY_X..U`, `MEM_START_VELOCITY_X..U`, `MEM_MAX_ACC_X..U`,
  `MEM_BTN_START_VEL_X..U`, `MEM_BTN_ACC_X..U`, `MEM_ESTP_ACC_X..U`,
  `MEM_HOME_OFFSET_X..U`, and `MEM_TRAVEL_Z/U` for the Z/U travel values), each
  typed from `TBDU35` to a dim/speed/accel spec (`Vel:…mm/S`, `Acc:…mm/S2`,
  `Prec:…mm`, `HomeOffset:…mm`) and `# Verified RDC8445S`. `MEM_BED_SIZE_X/Y`
  and `MEM_AXIS_CONTROL_PARA_*`/`MEM_AXIS_BACKLASH_*` are unchanged. The old
  names remain as deprecated aliases resolved in `ScriptParser`'s MT-mnemonic
  lookup, so existing `.rds` scripts and `GET_SETTING`/`SET_SETTING` keep
  working. The VSCode verified-mnemonic syntax list (66), the rsascript-guide,
  and the per-axis decode formatting tests were updated.
- Marked 29 more MT/CT mnemonics as `# Verified RDC6442S` (confirmed by probing
  an RDC6442S): `MEM_LASER_PWM_FREQUENCY_1`, `MEM_LASER_MIN_POWER_1`,
  `MEM_LASER_MAX_POWER_1`, `MEM_BED_SIZE_X/Y`, `MEM_MACHINE_STATUS`,
  `MEM_CARD_ID`, `MOVE_FAR_X/Y/Z`, `MOVE_NEAR_XY/X/Y`, `CUT_NEAR_XY/X/Y`,
  `IMD_POWER_1/3`, `STOP_JOB`, `PAUSE_JOB`, `RESUME_JOB`, `HOME_XY/Z`,
  `JOG_X/Y/Z/XY`, and `GET_SETTING`/`SET_SETTING`; entries already verified
  under another source were left unchanged. The VSCode verified-mnemonic syntax
  list grew to 95 so the newly verified mnemonics render green.
- Version bump to 0.23.0.

## [0.22.0] - 2026-10-02

### Added

- Text selection in the TUI log pane using the mouse (drag) or keyboard (Tab to focus, arrow keys to move a caret, Shift+arrows to extend); Enter copies the selection (with a confirmation toast, preferring a native clipboard tool over OSC 52) and Ctrl+C quits. Selection is confined to the log pane and copied text has no trailing spaces.
- `TcpTransport` for controllers that take the Ruida command stream over TCP port 50200 instead of UDP, such as the RDC8445S; select it with `RdDriver.start(protocol="tcp")` (also accepted by the RPC client/service and the TUI adapter). TCP packets carry no checksum prefix, and the byte stream is re-framed into the ACK and reply units the handshake expects
- `RdStatusEvent.TRANSPORT_TCP` and `RdTransport.is_tcp`
- `RdTransport.close_stream()`; after failed pings the status monitor closes a TCP connection and reconnects, so a client the controller dropped without closing the socket recovers
- `session start ... proto=udp|tcp` in the TUI and `.rds` scripts selects the network protocol (default `udp`)
- RDC8445S card ID (`0x90109010`)
- `GlueScript.focus_z()` sends `FOCUS_Z` (`D8 2E`), the controller's Z auto-focus (same routine as the panel Focus key): the table is raised until the probe triggers, lowered to `MEM_FOCUS_DEPTH`, and Z is set to that distance. Available through `RdDriver`, the RPC client/service and the TUI (`focus_z`)
- `MEM_MACHINE_FEATURES` (`0x030F`, renamed from `MEM_FOCUS_CONFIG`); a bit/field table (`MFT`/`MFT_FIELDS`) decodes the controller feature flags — bit `0x0001` focus enabled (so hosts can choose between `HOME_Z` and `FOCUS_Z`), bit `0x0008` Z return to docking, and field `0x0600` air-assist mode
- Text selection and clipboard copy in the TUI monitor pane (`#reply-log`, the memory/GC tables rendered by `/monitor`): mouse drag, keyboard caret (Tab to focus, arrows, Shift+arrows, Home/End), and Enter to copy. Selection is confined to the text currently displayed — off-screen rows and markup tags are never copied. The selection/caret machinery was extracted from `SelectableRichLog` into a shared `TextSelectionMixin` used by both the log and monitor panes.
- `./capture <ip> <file> --tcp` (bash) and `./capture.ps1 -Protocol tcp` (PowerShell) capture the Ruida TCP stream on port 50200 with tshark (`tcp.srcport`/`tcp.dstport`/`tcp.len`/`tcp.payload` fields) in addition to the default UDP capture
- `RuidaProtocolAnalyzer` autodetects TCP captures from the field count (UDP log lines have four tab-separated fields, TCP lines have five), so `rpa.py` decodes `./capture --tcp` logs with no extra parameter; TCP carries no checksum prefix and uses port 50200 for controller replies
- `RdDriver._FEATURES_SCRIPT` (renamed from `_BED_SIZE_SCRIPT`) now also issues `GET_SETTING MEM_MACHINE_FEATURES` on each `MEM_CARD_ID` reply, so the controller feature flags are fetched on connect and exposed to status listeners as the `MACHINE_FEATURES` key in `StatusDict` (`(raw_int, "MFeat:…")`)
- `RdDriver._FEATURES_SCRIPT` also issues `GET_SETTING MEM_MAINBOARD_VERSION`, so the mainboard firmware version (e.g. `RDLC-V8.01.70`) is fetched on connect and exposed to status listeners as the `MAINBOARD_VERSION` key in `StatusDict`; `0x057F` is now a handled status address and the TUI stores it
- `TransportEvent.MALFORMED_REPLY`; `RdTransport._unpack_replies` now requires a reply to be at least 9 bytes (`[0xDA, 0x01, msb, lsb, d0..d4]`; a `CSTRING` value may make it longer), fires `MALFORMED_REPLY` for shorter chunks, skips them, and returns only well-formed replies.
- `RdStatusEvent.TRANSPORT_MALFORMED_REPLY`, `TRANSPORT_REPLY_ERROR` and `TRANSPORT_UNEXPECTED_REPLY`; `RdStatus` re-surfaces the corresponding `TransportEvent`s as status events, and the TUI always writes them to the status log regardless of the `/status` and `/status connection` toggles.
- `/scan_mem` (and any run script containing `GET_SETTING`) now displays replies for driver-handled status addresses (positions, machine status, `CARD_ID`, bed size, features, mainboard version): the TUI arms its explicit-reply display for those addresses at run time, so values the driver otherwise consumes for status tracking are no longer silently dropped; the command-pane explicit-reply helpers now also cover script runs.
- The running ruida-pa version is exposed over RPC: `AppAdapter.get_version()` returns the library version, `TuiAdapter.get_version()` implements it, `RpycTuiService.exposed_get_version()` serves it, and `RpcRdDriver.get_version()`/`version_mismatch` forward and compare it, so an application adapter can detect a client/server version mismatch
- The README now documents the controller driver and application integration: the introduction and Features list cover `RdDriver` (UDP/TCP/USB connection, GlueScript job authoring, status/reply/error listeners) alongside the analyzer, and a new "Driver & Application Integration" section explains the application-specific-adapter role, direct vs. RPyC (`RpcRdDriver`) integration, shows an end-to-end driver example, and links the Integration Guide and RdDriver interface reference
- PyPI packaging now rewrites the README's repo-relative links to absolute GitHub `main` URLs so they resolve on PyPI: `scripts/pypi_readme.py` backs up `README.md`, rewrites file links to `blob/main` and images to `raw.githubusercontent.com`, and restores the working copy afterwards (fenced code and existing URLs are left untouched). It is invoked by `publish.sh`/`publish.ps1`, `release.sh`/`release.ps1`, and the `build-wheel` CI job.
- Version bump to 0.22.0.

### Fixed

- TUI `/monitor off` no longer crashes with `'Timer' object has no attribute 'cancel'`: Textual `Timer` handles returned by `set_interval()` are now stopped with `.stop()` instead of `.cancel()` (also fixed for `/clear`, the GlueScript file-watch stop, and the app-exit teardown); the asyncio `Task.cancel()` site is unaffected.
- TUI command-pane `GET_SETTING` replies now appear in the log pane for status addresses (`MEM_MACHINE_STATUS`, positions, `MEM_CARD_ID`, bed size) whose replies the driver otherwise consumes for status tracking; the TUI registers a raw transport reply listener and displays each queried address once.
- `MEM_FOCUS_DEPTH` (`0x020E`) now decodes as a Z-axis dimension (`ZFARDIM`, mm) instead of the opaque `TBDU35`, so it displays as `MEM_FOCUS_DEPTH: Z=12.345mm`.
- The protocol analyzer no longer aborts with `TypeError: unsupported format string passed to NoneType.__format__` when a host packet containing many `GET_SETTING` commands is answered with replies split across several reply packets: the parser now stays in reply mode across reply-packet boundaries, hands off cleanly to command parsing on the next host packet, and defers the command-context reset to the host command byte (also prevents continued replies from being mis-attributed to a phantom command number).
- `MEM_MAINBOARD_VERSION` (`0x057F`) now decodes as a C-string (e.g. `RDLC-V8.01.70`) instead of the opaque `TBDU35`; the C-string decoder now accumulates bytes until the NUL terminator instead of returning after one byte.
- The live driver/TUI now decodes variable-length reply values (such as `MEM_MAINBOARD_VERSION`) instead of showing a raw integer: `RdTransport` frames replies by high-bit boundary (rather than fixed 9-byte chunks), `RdDriver.format_reply_value`/`decode_status_value` decode from the full value bytes, and `RdDecoder` tolerates a missing output object instead of falling back to the wrong numeric type.
- Rapidly switching between the UDP and USB transports no longer raises an unhandled `IndexError` from an incomplete/malformed `GET_SETTING` reply; `RdTransport._unpack_replies` detects replies shorter than the 9-byte minimum, fires `MALFORMED_REPLY`, and returns only well-formed replies.
- `GET_SETTING` replies that span reply packets and do not start on a packet boundary (especially over USB, where `read()` returns arbitrary byte counts) are now reassembled: `RdTransport._handshake_loop` accumulates all pending-reply packets into one buffer (reading until the `_inter_packet_timeout` gap) before unpacking, instead of unpacking each read independently.

## [0.21.2] - 2026-09-30

### Added

- `publish.sh` and `publish.ps1` append a commit-derived dev version (e.g. `0.21.2.dev245122120`) to the package when uploading to TestPyPI (`--test`/`-Test`), giving each test upload a unique, uploadable version

### Fixed

- `UdpTransport.read()` no longer uses `socket.MSG_DONTWAIT` (POSIX-only); the socket is already non-blocking via `setblocking(False)`, so a plain `recv()` raises `BlockingIOError` when no data is pending — fixing an `AttributeError: module 'socket' has no attribute 'MSG_DONTWAIT'` on Windows
- TestPyPI uploads no longer use a PEP 440 local version (`0.21.2+<sha>`), which TestPyPI rejects with `HTTP 400 Bad Request`; the short commit ID is now encoded as a numeric dev segment

## [0.21.1] - 2026-09-27

### Added

- `set_power_scaling_enabled()` now records a storable gluescript transcript line, so a persisted `.cglu` replays it and the effective-min power scaling flag is restored before the next `power_range()` flush; it is exempt from the job-running guard so it can be toggled while a job runs

### Fixed

- Launching a second TUI instance while another holds the RPC port now reports the bind error on an error screen and exits gracefully (auto-start path) instead of failing silently in a background thread; manual `server start` logs the error and leaves the TUI running

## [0.21.0] - 2026-09-25

### Added

- `/gs` shorthand alias for the `/gluescript` TUI command, with full parity in command recognition, autocomplete, `/help`, and the file browser
- TUI `/run` command accepts an optional `.rds` file argument (`/run [<file>]`), loading and running the whole script in one step
- GlueScript loader watches the loaded `.cglu` file and auto-reloads on external edits (2-second poll)
- GlueScript `frequency()` and `declare_layer(frequency=...)` now emit a real `LAYER_FREQUENCY` rpascript action
- GlueScript authored `.cglu` files support multi-line parameter spans and keyword arguments
- GlueScript `power_range()` scales the effective min power by cut speed, with configurable `power_floor`, `max_cut_speed`, and `power_scaling_enabled`, plus a new `/power_scale` TUI command
- TUI file selector enters directories on Enter/Tab, and the command input no longer auto-selects on focus

### Fixed

- TUI `/help` colorization broken for some commands (unescaped `[` markup in `power_scale`/`listeners`; greedy `ReprHighlighter` tag regex swallowing `<...>` placeholders)
- TUI help implementation made consistent: every command documented in `_cmd_descriptions` and `/help` driven by a `_HELP_CATEGORIES` structure
- Windows USB transport now retries reconnect after cable disconnect/reconnect (`UsbTransport.open()` hardened against `comports()` exceptions; `/dev/` prefix applied on POSIX only; manual replug acceptance check pending)
- Windows UDP transport no longer dies silently on `WSAECONNRESET` (read/write paths and monitor thread guarded)
- `/frame` command uses the job's detected reference point (MACHINE/CURRENT/SET_POINT/ABSOLUTE)
- Malformed ABSOLUTE reference declarations warn instead of silently degrading
- `/autosave` fires for gluescript runs from any source (TUI stage/run/load paths, not just RPC)
- `LAYER_FREQUENCY` Laser parameter corrected to 0-based indexing (`Laser:0`)
- `LAYER_FREQUENCY` rpascript mnemonic marked verified
- GlueScript `frequency()` defers `LAYER_FREQUENCY` to `power_range()`, which now emits `SELECT_LAYER` before power changes
- GlueScript `power_range()`/`cut_speed()` defer emission to the next `cut_*` action so scaling uses the actual cut speed

### Removed

- `wait`/`delay` flow-control TUI commands and GlueScript directives (runner-side only, never sent to the controller)
