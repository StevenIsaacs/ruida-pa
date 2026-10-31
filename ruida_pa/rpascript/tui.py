"""
CLI entry point for rpa-script.

Generates Ruida protocol binary output from .rds script files.
Output can be piped directly to rpa for decoding.
"""

import argparse
import logging
import sys

from ruida_pa.rpa import __version__
from ruida_pa.rpascript.interpreter import ScriptInterpreter, ScriptParser


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for rpa-script."""
    parser = argparse.ArgumentParser(
        prog="rpa-script",
        description="Generate Ruida protocol output from .rds scripts or launch interactive TUI.",
        epilog="Examples: rpa-script myscript.rds | rpa -   |   rpa-script --tui",
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
        "--rpc",
        help="Auto-start the RPC server when the TUI launches. "
        "Requires --tui.",
        action="store_true",
    )
    parser.add_argument(
        "--rpc-host",
        help="RPC server bind address (default: localhost). Non-loopback "
             "addresses require --rpc-token and --rpc-cert/--rpc-key (TLS).",
        default="localhost",
    )
    parser.add_argument(
        "--rpc-port",
        help="RPC server bind port (default: 18812)",
        type=int,
        default=18812,
    )
    parser.add_argument(
        "--rpc-token",
        help="RPC authentication token. Optional on loopback binds (enforced "
             "for every client when set); required for non-loopback binds.",
        default=None,
    )
    parser.add_argument(
        "--rpc-cert",
        help="TLS certificate path for the RPC server (enables TLS with "
             "--rpc-key; required for non-loopback binds)",
        default=None,
    )
    parser.add_argument(
        "--rpc-key",
        help="TLS private key path for the RPC server",
        default=None,
    )
    parser.add_argument(
        "--rpc-ca",
        help="CA certificate; RPC clients must then present a certificate "
             "signed by it (mutual TLS)",
        default=None,
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

    # --rpc and its tuning params are only meaningful with the TUI:
    # reject them at the parse boundary instead of silently ignoring them.
    if args.rpc and not args.tui:
        parser.error("--rpc requires --tui")
    rpc_tuning_given = (
        args.rpc_host != "localhost"
        or args.rpc_port != 18812
        or args.rpc_token
        or args.rpc_cert
        or args.rpc_key
        or args.rpc_ca
    )
    if rpc_tuning_given and not args.rpc:
        parser.error(
            "--rpc-host/--rpc-port/--rpc-token/--rpc-cert/--rpc-key/--rpc-ca "
            "require --rpc"
        )

    # TUI mode: launch interactive terminal interface
    if args.tui:
        from ruida_pa.rpascript.tui_adapter import run_tui

        if args.script:
            print(
                f'Note: script argument "{args.script}" ignored in TUI mode. '
                "Use Ctrl+L to load scripts within the TUI."
            )
        exit_code = run_tui(
            rpc=args.rpc,
            rpc_host=args.rpc_host,
            rpc_port=args.rpc_port,
            rpc_token=args.rpc_token,
            rpc_cert=args.rpc_cert,
            rpc_key=args.rpc_key,
            rpc_ca=args.rpc_ca,
        )
        if exit_code is not None:
            sys.exit(exit_code)
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
