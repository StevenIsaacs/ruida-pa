"""
CLI entry point for rpa-script.

Generates Ruida protocol binary output from .rds script files.
Output can be piped directly to rpa.py for decoding.
"""

import argparse
import logging
import sys

from rpa import __version__
from rpascript.interpreter import ScriptInterpreter, ScriptParser


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for rpa-script."""
    parser = argparse.ArgumentParser(
        prog="rpa-script",
        description="Generate Ruida protocol output from .rds scripts or launch interactive TUI.",
        epilog="Examples: rpa-script myscript.rds | python rpa.py -   |   rpa-script --tui",
    )
    parser.add_argument(
        "script",
        nargs="?",
        default=None,
        help=".rds script file to process (optional with --tui)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output file (default: stdout)",
        default=None,
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        help="Parse only, show parsed commands without generating output",
        action="store_true",
    )
    parser.add_argument(
        "-t",
        "--tui",
        help="Launch interactive TUI (Textual-based terminal interface)",
        action="store_true",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main() -> None:
    """Main entry point for rpa-script CLI."""
    parser = build_parser()
    args = parser.parse_args()

    # TUI mode: launch interactive terminal interface
    if args.tui:
        from rpascript.tui_adapter import run_tui

        if args.script:
            print(
                f'Note: script argument "{args.script}" ignored in TUI mode. '
                "Use Ctrl+L to load scripts within the TUI."
            )
        run_tui()
        return

    # Script argument is required when not in TUI mode
    if args.script is None:
        parser.print_help()
        sys.exit(1)

    # Parse script
    script_parser = ScriptParser(warning_callback=lambda msg, syn: logging.warning(f"{msg}  |  Syntax: {syn}"))
    commands = script_parser.parse_file(args.script)

    # Dry run: show parsed commands
    if args.dry_run:
        for cmd in commands:
            if cmd["type"] == "new_packet":
                print("  new_packet")
                continue
            params_str = " ".join(cmd["params"])
            expected_str = f"= {cmd['expected']}" if cmd["expected"] else ""
            print(
                f"  {cmd['type']:8s} {cmd['mnemonic']:20s} "
                f"{params_str:30s} {expected_str}"
            )
        return

    # Generate tshark output
    out_stream = open(args.output, "w") if args.output else sys.stdout
    try:
        interpreter = ScriptInterpreter(out_stream)
        interpreter.interpret(commands)
    finally:
        if args.output:
            out_stream.close()


if __name__ == "__main__":
    main()
