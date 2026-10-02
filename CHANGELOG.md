# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `TcpTransport` for controllers that take the Ruida command stream over TCP port 50200 instead of UDP, such as the RDC8445S; select it with `RdDriver.start(protocol="tcp")` (also accepted by the RPC client/service and the TUI adapter). TCP packets carry no checksum prefix, and the byte stream is re-framed into the ACK and reply units the handshake expects
- `RdStatusEvent.TRANSPORT_TCP` and `RdTransport.is_tcp`
- `RdTransport.close_stream()`; after failed pings the status monitor closes a TCP connection and reconnects, so a client the controller dropped without closing the socket recovers
- `session start ... proto=udp|tcp` in the TUI and `.rds` scripts selects the network protocol (default `udp`)
- RDC8445S card ID (`0x90109010`)
## [0.22.0] - 2026-10-02

### Added

- Version bump to 0.22.0.

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
