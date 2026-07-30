import argparse
import os
import sys
import time
from pathlib import Path

import protocols.ruida.ruida_analyzer as rpa
from rpalib.rpa_emitter import RpaEmitter
from protocols.ruida.ruida_parser import RdParser
from rpalib.rd_binary_reader import RdBinaryStream
from rpascript.interpreter import ScriptParser
from rpascript.encoding import encode_command
from rpalib.ruida_transcoder import RdEncoder
from rpalib.rpa_swizzler import RpaSwizzler

# Graceful Bokeh import: BokehApp is None if Bokeh not installed.
try:
    from rpalib.bokeh_app import BokehApp
except ImportError:
    BokehApp = None

# Additional Bokeh imports for --save-plot
try:
    from bokeh.embed import file_html
    from bokeh.models import ColumnDataSource
    from bokeh.resources import CDN
    from rpalib.bokeh_view import BokehView
except ImportError:
    file_html = None
    ColumnDataSource = None
    CDN = None
    BokehView = None

# --- Version detection ---
# Prefer importlib.metadata (works when package is pip-installed or built with PyInstaller).
# Falls back to a dev version when running rpa.py directly from source.
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        __version__ = _pkg_version("ruida-pa")
    except PackageNotFoundError:
        __version__ = "0.13.0"
except ImportError:
    # Python < 3.8 fallback
    __version__ = "0.13.0"


def parse_arguments():
    """Parse command line arguments for Ruida protocol analyzer"""
    parser = argparse.ArgumentParser(
        description="""
Ruida Protocol Analyzer - Parse and decode Ruida protocol packets.

The tshark log file must be in a specific format. Use this command to capture:

tshark -Y "(ip.addr == <ruida_ip> && udp.payload)" -T fields \
       -e frame.time -e udp.port -e udp.length -e data.data > capture.log

The decoded data is emitted to the console (stdout) which can be redirected to
a file.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s capture.log                      # Analyze tshark log file
  %(prog)s capture.rd                       # Analyze RDWorks binary file
  %(prog)s -o output.txt capture.log        # Save decoded output to file
  %(prog)s --verbose --raw capture.log      # Detailed output with raw data
  %(prog)s --magic 0x88 capture.rd          # Use specific magic number
  %(prog)s --generate-rd capture.log        # Decode and generate .rd binary
  %(prog)s capture.rds                      # Convert .rds script to .rd binary
        """,
    )

    # Input source
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Tshark log file (.log/.txt) or RDWorks binary file (.rd) to analyze.",
    )

    # Input file encodng.
    parser.add_argument(
        "--input-encoding",
        metavar="<input_encoding>",
        default="utf-8",
        help="Input text encoding. Windows files can be encoded as utf-16.",
    )

    # Protocol
    parser.add_argument(
        "--protocol",
        metavar="<protocol>",
        default="ruida",
        help="Specify the protocol to use for decoding the raw data. "
        "Currently only the ruida protocol is available.",
    )

    # Magic number
    parser.add_argument(
        "--magic",
        metavar="<magic_number>",
        default="0x88",
        help="Specify the swizzle magic number rather than attempt "
        "to discover it in the capture. "
        "Available only with the ruida protocol.",
    )

    # Output file
    parser.add_argument(
        "--out",
        "-o",
        dest="output_file",
        metavar="<file>",
        help="Write the decoded data to <file> in addition to the console.",
    )

    # Quiet mode
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Do not output to stdout. Output file (if specified with -o) is still written.",
    )

    # Verbose output
    parser.add_argument(
        "--verbose", action="store_true", help="Generate verbose output."
    )

    # Raw dump output
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Output the raw dump lines with the decoded output.",
    )

    # Raw dump output
    parser.add_argument(
        "--unswizzled",
        action="store_true",
        help="Output the unswizzled and unprocessed data.",
    )

    # Stop on error
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop decode when an error is detected -- do not attempt to resync.",
    )

    # Plot moves.
    parser.add_argument(
        "--plot-moves",
        action="store_true",
        help="Plot all moves and cuts in an interactive Bokeh visualization.",
    )

    # Bokeh server port
    parser.add_argument(
        "--bokeh-port",
        type=int,
        default=5006,
        metavar="<port>",
        help="Port for the Bokeh visualization server (default: 5006).",
    )

    # Save plot to HTML file
    parser.add_argument(
        "--save-plot",
        action="store_true",
        help="Save an interactive Bokeh HTML plot and exit (no server).",
    )

    # Generate .rds script file
    parser.add_argument(
        "--generate-script",
        action="store_true",
        help="Generate a .rds script file alongside the standard output for round-trip verification.",
    )

    # Generate .rd binary file
    parser.add_argument(
        "--generate-rd",
        action="store_true",
        help="Generate a .rd binary file from the decoded data.",
    )

    # Version
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    args = parser.parse_args()

    # Validation
    if not args.input_file:
        parser.error("Input file required")

    if args.magic is not None and args.protocol != "ruida":
        parser.error("--magic can only be used with the ruida protocol.")

    # Parse magic number if provided
    if args.magic:
        try:
            # Handle hex format (0x prefix) or decimal
            if args.magic.lower().startswith("0x"):
                args.magic = int(args.magic, 16) & 0xFF
            else:
                raise ValueError
        except ValueError:
            parser.error(f"Invalid magic number format: {args.magic}")

    return args


def open_input(args):
    """Open the input tshark log file for reading."""
    return open(args.input_file, "r", encoding=args.input_encoding)


def _write_rd(commands, mnemonic_map, mt_map, rd_path, magic):
    """Encode parsed commands to binary, swizzle, and write .rd file.

    Returns a summary string on success, or None on error.
    """
    enc = RdEncoder()
    raw = bytearray()
    for cmd in commands:
        cmd_type = cmd.get("type")
        if cmd_type in ("new_packet", "SESSION_START", "SESSION_END", "DELAY", "WAIT"):
            continue
        mnemonic = cmd.get("mnemonic")
        if not mnemonic:
            continue
        if mnemonic.startswith("GET_"):
            continue
        try:
            cmd_bytes = encode_command(cmd, mnemonic_map, mt_map, enc)
        except (ValueError, TypeError) as e:
            print(
                f"Encoding failed for command '{mnemonic}' "
                f"(params={cmd.get('params', [])!r}): {e}",
                file=sys.stderr,
            )
            return None
        raw.extend(cmd_bytes)

    if not raw:
        print("No encodable commands.", file=sys.stderr)
        return None

    swizzler = RpaSwizzler(magic=magic)
    swizzled = swizzler.swizzle(raw)

    try:
        with open(rd_path, "wb") as f:
            f.write(b"RDWORKV" + b"\x00" * 3)
            f.write(swizzled)
    except OSError as e:
        print(f"Error writing {rd_path}: {e}", file=sys.stderr)
        return None

    return f"Wrote {len(raw)} bytes ({len(swizzled)} swizzled) to {rd_path}"


def main():
    """Main function with command line argument processing"""
    args = parse_arguments()

    # Detect input type by extension
    _, ext = os.path.splitext(args.input_file)
    ext = ext.lower()
    is_rd = (ext == ".rd")
    is_rds = (ext == ".rds")

    # Set up output handling
    output = RpaEmitter(args)
    output.open()

    # .rds input: no decode needed, parse script and generate .rd directly
    if is_rds:
        rd_path = str(Path(args.input_file).with_suffix(".rd"))
        sparser = ScriptParser()
        commands = sparser.parse_file(args.input_file)
        result = _write_rd(commands, sparser.mnemonic_map, sparser.mt_map, rd_path, args.magic)
        if result:
            print(result, file=sys.stderr)
        else:
            print("Failed to generate .rd file.", file=sys.stderr)
        output.close()
        sys.stdout.flush()
        return

    if is_rd:
        stream = RdBinaryStream(args.input_file, magic=args.magic)
        parser = RdParser(output, args.input_file)
        analyzer = None
    else:
        input_stream = open_input(args)
        analyzer = rpa.RuidaProtocolAnalyzer(args, input_stream, output)
        parser = analyzer.parser

    # Derive output base for script file generation
    base = args.output_file or args.input_file or "capture"

    # Set up script generator
    if args.generate_rd:
        args.generate_script = True  # Auto-enable --generate-script
    script_gen = None
    if getattr(args, "generate_script", False):
        from rpascript.generator import ScriptGenerator

        script_path = str(Path(base).with_suffix(".rds"))
        script_gen = ScriptGenerator(script_path, source_file=args.input_file)
        parser.on_command = script_gen.write_command
        if analyzer is not None:
            analyzer.on_new_packet = script_gen.on_new_packet
        output.info(f"Generating script: {script_path}")

    # Derive .rd output path for --generate-rd
    rd_path = None
    if args.generate_rd:
        if is_rd:
            rd_path = str(Path(str(base) + "-reencoded").with_suffix(".rd"))
        else:
            rd_path = str(Path(base).with_suffix(".rd"))

    bokeh_app = None

    if args.plot_moves or args.save_plot:
        parser.plot.plot.enable()

    if args.plot_moves and BokehApp is None:
        output.warning("Bokeh is not installed. Install with: pip install bokeh")

    try:
        if is_rd:
            # Feed bytes from binary stream directly to the parser state machine
            while True:
                b = stream.next_byte()
                if b is None:
                    break
                parser.step(
                    b,
                    is_reply=False,
                    take=stream.take,
                    remaining=stream.remaining,
                )
        else:
            analyzer.decode()  # Does not return until decode is complete.

        output.info("Decode complete.\n")
        output.close()

        if script_gen is not None:
            script_gen.close()

        if args.generate_rd and rd_path:
            # Read generated .rds back, encode to .rd binary
            sparser = ScriptParser()
            commands = sparser.parse_file(script_path)
            result = _write_rd(commands, sparser.mnemonic_map, sparser.mt_map, rd_path, args.magic)
            if result:
                print(result, file=sys.stderr)
            else:
                print(f"Failed to generate {rd_path}.", file=sys.stderr)

        if args.save_plot and file_html is not None:
            # Save interactive HTML plot without starting a Bokeh server.
            try:
                _plot = parser.plot.plot

                _plot_cds = ColumnDataSource(
                    data=_plot.to_column_data()
                )
                # Use input file stem for clean naming
                _in = Path(args.input_file)
                _stem = _in.stem
                _view = BokehView(
                    args,
                    source=_plot_cds,
                    title="All Vectors",
                    color_lut=_plot.color_lut,
                    out_stem=_stem,
                )
                _view.update_histograms()

                # Output alongside input: <stem>-plot.html
                _plot_path = _in.with_name(f"{_stem}-plot.html")

                _html = file_html(_view.layout, CDN, title=_view.title)
                _plot_path.write_text(_html, encoding="utf-8")
                print(
                    f"Interactive plot saved to {_plot_path}",
                    file=sys.stderr,
                )
            except Exception as e:
                print(
                    f"Failed to save plot: {e}",
                    file=sys.stderr,
                )

        if args.plot_moves and BokehApp is not None:
            # File mode: start Bokeh server after output file is written.
            try:
                bokeh_app = BokehApp(args, parser.plot.plot)
                if bokeh_app.start(port=args.bokeh_port):
                    print(
                        "Now plotting moves. Press Ctrl+C in the terminal to exit.",
                        file=sys.stderr,
                    )
                    # Block until user presses Ctrl+C.
                    try:
                        while bokeh_app._running:
                            time.sleep(0.1)
                    except KeyboardInterrupt:
                        pass
                else:
                    print("Failed to start Bokeh server.", file=sys.stderr)
                    bokeh_app = None
            except Exception as e:
                print(f"Failed to start Bokeh server: {e}.", file=sys.stderr)
                bokeh_app = None

    except LookupError as e:
        output.critical(f"{e}")
        if is_rd:
            output.critical("Verify the .rd file is not corrupted or try --magic 0xNN.")
        else:
            output.critical("Verify incoming data is a tshark dump of a Ruida UDP session.")
    except SyntaxError as e:
        output.critical(f"{e}")
    except RuntimeError as e:
        output.critical(f"Shutting down: {e}")
    except KeyboardInterrupt:
        output.info("Exiting at user request.\n")
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception as e:
        if isinstance(e, str):
            output.error(f"Unhandled error: {e}")
        else:
            output.error(f"Unhandled exception {e.__str__()}")
        exit(1)
    finally:
        if bokeh_app is not None:
            bokeh_app.shutdown()
        output.close()
        sys.stdout.flush()


if __name__ == "__main__":
    main()
