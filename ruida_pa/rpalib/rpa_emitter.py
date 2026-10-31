"""Message emitter for the Ruida Protocol Analyzer"""

import sys
from pathlib import Path


class RpaEmitter:
    """
    A class to write messages to the output file and console.

    Some message actions will trigger exceptions and a shutdown.

    """

    def __init__(self, args):
        self.args = args
        self._out_fp = None
        self.pkt_n = 0
        self.cmd_n = 0
        self.dir = "---"
        self._msg_n = 0

    @property
    def out_stem(self):
        return Path(self.args.output_file).with_suffix("")

    def open(self):
        if self.args.output_file:
            self._out_fp = open(self.args.output_file, "w")

    def close(self):
        if self._out_fp is not None:
            self._out_fp.close()
            self._out_fp = None

    def set_pkt_n(self, pkt_n: int):
        self.pkt_n = pkt_n

    def set_cmd_n(self, cmd_n: int):
        self.cmd_n = cmd_n
        self._msg_n = 1

    def set_direction(self, dir: str):
        self.dir = dir

    def pause(self, message="Press Enter"):
        return input("\n" + message + ": ")

    def console(self, message: str):
        """Emit a message to the console (stdout) only."""
        sys.stdout.write(message)
        sys.stdout.flush()

    def write(self, message: str):
        """A write method to emulate an output file for the analyzer.

        The analyzer calls this method while decoding the input file. This
        then writes to the console or the output file."""
        _msg = f"\n{self.pkt_n:04d}:{self.cmd_n:06d}:{self._msg_n:03d}:{message}"
        self._msg_n += 1
        if self._out_fp is not None:
            self._out_fp.write(_msg)
        if not self.args.quiet:
            sys.stdout.write(_msg)
            sys.stdout.flush()

    def reader(self, message: str):
        """Emit packet information messages from a reader."""
        self.write(f"PRT:RDR:{self.dir}:" + message)

    def parser(self, message: str):
        """Emit a message related to parsing the incoming data."""
        self.write(f"PRT:PRS:{self.dir}:" + message)

    def error(self, message: str):
        """Emit error messages related to the incoming stream."""
        self.write(f"PRT:ERR:{self.dir}:" + message)
        if self.args.stop_on_error:
            self.pause()

    def shutdown(self, message: str):
        """This is used to shutdown the analyzer when an uncorrectable error
        is detected (cannot sync)."""
        self.write(f"PRT:FTL:{self.dir}:" + message)
        raise SyntaxError("Stopping...")

    def verbose(self, message: str):
        """Emit verbose messages."""
        if self.args.verbose:
            self.write("vrb:" + message)

    def raw(self, message: str):
        """Emit raw unprocessed data messages or packets."""
        if self.args.raw:
            self.write(f"PRT:raw:{self.dir}:\n" + message)

    # Internal messages.
    def protocol(self, message: str):
        """A problem with the protocol definitions or state machines."""
        _msg = "INT:FTL:" + message
        if self.args.stop_on_error:
            raise SyntaxError(_msg)
        else:
            self.write(_msg)

    def info(self, message: str):
        """Emit message related to the protocol analyzer and parser."""
        self.write("INT:---:" + message)

    def warn(self, message: str):
        """A warning about a correctable error."""
        self.write("INT:WRN:" + message)

    def critical(self, message: str):
        """A critical error has occurred -- will continue to run."""
        self.write("INT:CRT:" + message)

    def fatal(self, message: str):
        """A fatal error has occurred and shutting down."""
        raise RuntimeError("INT:FTL:" + message)
