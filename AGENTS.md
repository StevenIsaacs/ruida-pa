# AGENTS.md — ruida-pa

## Project at a glance

A tool that decodes binary UDP packets captured from Ruida CNC/laser controllers. Entry point: `rpa.py`.

## Commands

```
python rpa.py capture.log              # Decode a tshark log file
./capture <ip> <file>                  # Capture tshark log (bash)
./capture.ps1 -if Ethernet -ip <IP> -out <file>  # Capture (PowerShell)
./decode <file>                        # Produces <file>.tshark + <file>-vrb.tshark
./link <type> <case> <app>             # Symlink a test case into discovery/
```

- `./decode` **requires a venv to be active** (checks `$VIRTUAL_ENV`). Create one from `requirements.txt` if needed.
- `requirements.txt` has `bokeh` (plotting). Everything else is stdlib.

## Architecture

| Path | Role |
|------|------|
| `rpa.py` | CLI entry — arg parsing, opens input, runs analyzer |
| `rpalib/` | Output emission, line parsing, plotting UI (Bokeh) |
| `protocols/ruida/` | Protocol state machine, parser, command tables, checksum logic |
| `ruidadriver/rd_gluescript.py` | High-level job scripting (GlueScript mixin for RdDriver) |
| `discovery/` | **Git submodule** — test cases (`tc/`), problems (`prb/`), captured logs |

Only the Ruida UDP protocol is currently implemented. Adding a new protocol means creating a parallel `protocols/<name>/` directory with its own analyzer.

## Workflow

1. **Capture** traffic with `./capture` → produces `.log`
2. **Decode** with `./decode` → produces `.tshark` (summary) and `-vrb.tshark` (verbose)
3. **Investigate** unknown commands/parameters marked `TBD` in output

The `./link` script creates symlinks (`discovery/selected.log`, `selected.tshark`, `selected-vrb.tshark`) pointing to a specific test case. Apps are identified as `mk` (MeerK40t), `lb` (LightBurn), `rdw` (RDWorks).

## Key conventions

- Protocol command tables live in `protocols/ruida/ruida_parser.py` (CT dict: byte → command name + param specs).
- Parameter decoder tuples: `(format_string, decoder_fn, raw_type)` — e.g. `('X={}mm', dim, 'int_35')`.
- Checksum is a running sum of bytes in engrave/cut commands; excludes memory and jog commands. Known ~220-byte discrepancy with LightBurn captures.
- `discovery/` is a **separate git repo** (submodule). Commit test case changes there, not in the parent.
- `/gluescript` TUI command for high-level job scripting (see [docs/guides/gluescript-guide.md](docs/guides/gluescript-guide.md)).
  Subcommands: `new`, `show`, `declare_job`, `end_job`, `declare_layer`, `layer`, `stage`, `run`, `save`, `load`, `list`, `list_rpa`.
  `save`/`load` persist the current gluescript to `.cglu` files (the on-disk GlueScript format; `.gs` is deliberately unused). Jog commands (`jog_*`, including the `jog_set_*` config setters) are live-only actions — they act on the live session (movement jogs execute immediately; `jog_set_*` configure jog speed/distance) and are never persisted to, or replayed from, `.cglu` files.

## No test/lint/CI infrastructure

There are no unit tests, no formatter, no linter, no type checker, and no CI pipeline. Verify changes manually by running `rpa.py` against existing capture logs in `discovery/`.

## Dev tips

- README states VSCode or its forks like VSCodium and Antigravity are the recommended IDEs for stepping through code alongside plots.
- PEP8 compliance expected.
- `.vscode/launch.json` exists for debugging.
- `--plot-moves` opens a Bokeh server application in browser showing interactive head moves with power/speed popups, context menus, and filtering.
- Ignored dirs: `discovery/`, `testing/`, `tmp/`, `build/`, `dist/`, `__pycache__`, `.png` files.
- Temporary files are to be placed in `tmp/`.
- Test output files are to be placed in `tmp/`.
- Test output file names have the form `<base>-<run>.<ext>` where:
	- `<base>` is the base name of the input file.
	- `<run>` is a sequential two digit run number. New runs with the same input file will increment this number.
	- `<ext>` is the extension corresponding to the output file type where:
		- `.log` is a `tshark` capture file.
		- `.txt` is a decode text file.
		- `.rds` is a Ruida Script file.
		- `.cglu` is a GlueScript file.
		- `.tshark` is a generated `tshark` log file. NOTE: When doing round trip testing packet sequence and content should be identical to the input file. The timestamps can vary.
	  For example, if the input file is `discovery/selected.log` then first test run using this input file will generate the following files:
		- `tmp/selected-01.txt` for the decode file.
		- `tmp/selected-01.rds` for the generated Ruida Script file.
		- When performing round trip testing using `ruidascript`, additional files will be generated:
			- `tmp/selected-01.tshark` for the `tshark` log using `tmp/selected-01.rds` as the input to `ruidascript`.
			- `tmp/selected-01-rt.txt` for the decode file generated using `tmp/selected-01.tshark` as the input file.
		A second run will have the number `02` instead of `01`.

- `commit.txt`: Pre-composed commit message, placed in project root. Only cleared on user request after commit is confirmed.
