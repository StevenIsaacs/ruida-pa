# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
