"""
Script parser for .rds (Ruida Discovery Script) files.

Parses human-readable script files into structured command tuples,
supporting comment stripping and command/expected-reply directives.
"""

from protocols.ruida.ruida_protocol import ACK, CT, MT
from rpalib.rpa_swizzler import RpaSwizzler
from rpalib.ruida_transcoder import RdEncoder
from rpascript.encoding import (
    _ENCODER_MAP,
    _RDTYPE_ENCODER_MAP,
    encode_command,
    encode_mt_param,
    encode_params,
    encode_single_param,
    is_end_job,
    parse_value,
    should_include_in_checksum,
)

# Type group names recognized in .rds script files.
# These are purely informational labels in the script format and map to
# the underlying hex-prefix command categories in CT.
TYPE_NAMES = frozenset(
    {
        "CORE",
        "MOVE",
        "LASER",
        "CONFIG",
        "QUERY",
        "ENGRAVE",
        "CUT",
        "FILE",
        "SYSTEM",
    }
)


def reconstruct_script_line(cmd: dict) -> str:
    """Convert a parsed command dict back to an rpascript text line.

    Reconstructs the original rpascript line from a parsed command dict,
    handling session meta-commands, NEW_PACKET directives, and regular commands
    with their parameters and expected values.

    Args:
        cmd: Parsed command dict from ScriptParser.

    Returns:
        rpascript-formatted command line string suitable for driver.run().
    """
    cmd_type = cmd.get("type")

    # Session commands
    if cmd_type == "SESSION_START":
        params = cmd.get("params", {})
        tokens = ["session", "start"]
        for k, v in params.items():
            tokens.append(f"{k}={v}" if v is not None else f"{k}=none")
        return " ".join(tokens)

    if cmd_type == "SESSION_END":
        return "session end"

    if cmd_type == "SERVER_START":
        params = cmd.get("params", {})
        tokens = ["server", "start"]
        for k, v in params.items():
            tokens.append(f"{k}={v}" if v is not None else f"{k}=none")
        return " ".join(tokens)

    if cmd_type == "SERVER_STOP":
        return "server stop"

    if cmd_type == "DELAY":
        params = cmd.get("params", [])
        return f"delay {params[0]}" if params else "delay 0s"

    if cmd_type == "WAIT":
        parts = ["wait"]
        params = cmd.get("params", [])
        if params:
            parts.append(params[0])
        to_val = cmd.get("to")
        if to_val is not None:
            parts.append(f"to={to_val}")
        return " ".join(parts)

    # NEW_PACKET directive
    if cmd_type == "NEW_PACKET":
        return "NEW_PACKET"

    # Regular command: [TYPE] MNEMONIC param1 param2 [= expected]
    tokens = []
    if cmd_type:
        tokens.append(cmd_type)
    mnemonic = cmd.get("mnemonic")
    if mnemonic and mnemonic != cmd_type:
        tokens.append(mnemonic)
    tokens.extend(cmd.get("params", []))
    expected = cmd.get("expected")
    if expected is not None:
        tokens.append("=")
        tokens.append(str(expected))
    return " ".join(tokens)


def _strip_inline_comment(line: str) -> str:
    """Remove inline # comments, respecting quoted strings and \\# escapes."""
    in_quote = False
    quote_char = None
    i = 0
    while i < len(line):
        ch = line[i]
        # Track escaped hash: \# — skip it as a literal hash
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "#" and not in_quote:
            i += 2  # skip both \ and #
            continue
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
        elif ch == quote_char and in_quote:
            in_quote = False
            quote_char = None
        elif ch == "#" and not in_quote:
            return line[:i].rstrip()
        i += 1
    return line


def _find_block_comment(text: str) -> tuple[int, int]:
    """Find the start and end of a triple-quote block comment."""
    start = text.find('"""')
    if start == -1:
        return -1, -1
    end = text.find('"""', start + 3)
    if end == -1:
        raise ValueError('Unterminated """ block comment')
    return start, end + 3


def _remove_block_comments(text: str) -> str:
    """Remove all triple-quote block comments from text."""
    result = text
    while True:
        start, end = _find_block_comment(result)
        if start == -1:
            break
        result = result[:start] + result[end:]
    return result


class ScriptParser:
    """Parser for .rds (Ruida Discovery Script) files.

    Reads human-readable script files and produces structured command dicts
    ready for interpretation / binary encoding.

    Uses the CT command table from protocols.ruida.ruida_protocol to resolve
    command mnemonics and parameter specifications.
    """

    def __init__(self, warning_callback=None) -> None:
        self._mnemonic_map: dict[str, tuple] = self._build_mnemonic_map()
        self._mt_map: dict[str, tuple[int, int]] = self._build_mt_map()
        self._warning_callback = warning_callback or ScriptParser._default_warning

    @staticmethod
    def _default_warning(message: str, syntax: str) -> None:
        import logging
        logging.warning(f"Line: {message}  |  Syntax: {syntax}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_file(self, path: str) -> list[dict]:
        """Open and parse an .rds script file.

        Returns a list of command dicts.
        """
        with open(path, "r") as f:
            content = f.read()
        lines = content.splitlines()
        return self.parse_lines(lines)

    def parse_lines(self, lines: list[str]) -> list[dict]:
        """Parse a list of script lines (useful for testing).

        Strips block comments, then processes each non-empty line.
        """
        text = "\n".join(lines)
        text = _remove_block_comments(text)
        stripped_lines = text.splitlines()

        commands: list[dict] = []
        for line_num, raw_line in enumerate(stripped_lines, start=1):
            trimmed = raw_line.strip()
            if not trimmed:
                continue
            cmd = self._parse_line(trimmed, line_num)
            if cmd is not None:
                commands.append(cmd)
        return commands

    # ------------------------------------------------------------------
    # Line parsing
    # ------------------------------------------------------------------

    def _parse_line(self, line: str, line_num: int) -> dict | None:
        """Parse a single non-empty script line into a command dict.

        Returns None if the line is empty after comment stripping.
        """
        # Strip inline comments
        line = _strip_inline_comment(line)
        line = line.strip()
        if not line:
            return None

        tokens = line.split()
        if not tokens:
            return None

        raw = line
        idx = 0

        # --- Session meta-commands (live controller testing) ---
        if tokens[0] == "session":
            if len(tokens) < 2:
                self._warning_callback(
                    f'{line_num}: "session" requires an action: start or end',
                    'session start [key=value ...] or session end'
                )
                return None
            action = tokens[1].lower()
            if action == "start":
                kwargs = {}
                for token in tokens[2:]:
                    key, _, val = token.partition("=")
                    kwargs[key.lower()] = None if val.lower() == "none" else val
                return {
                    "type": "SESSION_START",
                    "mnemonic": "SESSION_START",
                    "params": kwargs,
                    "expected": None,
                    "line_num": line_num,
                    "raw": raw,
                }
            elif action == "end":
                return {
                    "type": "SESSION_END",
                    "mnemonic": "SESSION_END",
                    "params": [],
                    "expected": None,
                    "line_num": line_num,
                    "raw": raw,
                }
            else:
                self._warning_callback(
                    f'{line_num}: Unknown session action "{action}". Use "start" or "end".',
                    'session start [key=value ...] or session end'
                )
                return None

        # --- Server meta-commands (RPC server control) ---
        if tokens[0] == "server":
            if len(tokens) < 2:
                self._warning_callback(
                    f'{line_num}: "server" requires an action: start or stop',
                    'server start [host=...] [port=...] [token=...] or server stop'
                )
                return None
            action = tokens[1].lower()
            if action == "start":
                kwargs = {}
                for token in tokens[2:]:
                    key, _, val = token.partition("=")
                    kwargs[key.lower()] = None if val.lower() == "none" else val
                # Parse port as int if provided
                if "port" in kwargs:
                    try:
                        kwargs["port"] = int(kwargs["port"])
                    except ValueError:
                        self._warning_callback(
                            f'{line_num}: Invalid port value "{kwargs["port"]}"',
                            'server start port=<port_number>'
                        )
                        return None
                return {
                    "type": "SERVER_START",
                    "mnemonic": "SERVER_START",
                    "params": kwargs,
                    "expected": None,
                    "line_num": line_num,
                    "raw": raw,
                }
            elif action == "stop":
                return {
                    "type": "SERVER_STOP",
                    "mnemonic": "SERVER_STOP",
                    "params": [],
                    "expected": None,
                    "line_num": line_num,
                    "raw": raw,
                }
            else:
                self._warning_callback(
                    f'{line_num}: Unknown server action "{action}". Use "start" or "stop".',
                    'server start [host=...] [port=...] [token=...] or server stop'
                )
                return None

        # --- NEW_PACKET directive (per-packet boundary marker) ---
        if tokens[0] == "NEW_PACKET":
            return {
                "type": "NEW_PACKET",
                "mnemonic": "NEW_PACKET",
                "params": [],
                "expected": None,
                "line_num": line_num,
                "raw": raw,
            }

        # --- Flow-control: delay ---
        if tokens[0] == "delay":
            if len(tokens) < 2:
                self._warning_callback(
                    f'{line_num}: "delay" requires a time argument (e.g. 5s, 500ms)',
                    'DELAY <time> (e.g. 5s, 500ms)'
                )
                return None
            return {
                "type": "DELAY",
                "mnemonic": "DELAY",
                "params": tokens[1:],
                "expected": None,
                "line_num": line_num,
                "raw": raw,
            }

        # --- Flow-control: wait ---
        if tokens[0] == "wait":
            if len(tokens) < 2:
                self._warning_callback(
                    f'{line_num}: "wait" requires a status argument (e.g. MACHINE_STATUS_JOB_RUNNING)',
                    'WAIT <STATUS> [to=<seconds>]'
                )
                return None
            status = tokens[1]
            kwargs = {}
            for token in tokens[2:]:
                key, _, val = token.partition("=")
                kwargs[key.lower()] = None if val.lower() == "none" else val
            # Validate recognized keyword parameters
            valid_wait_keys = frozenset({"to"})
            unknown = kwargs.keys() - valid_wait_keys
            if unknown:
                self._warning_callback(
                    f"{line_num}: Unknown wait parameter(s): {', '.join(sorted(unknown))}. Valid: to=<timeout>",
                    'WAIT <STATUS> [to=<seconds>]'
                )
                return None
            return {
                "type": "WAIT",
                "mnemonic": "WAIT",
                "params": [status],
                "to": kwargs.get("to"),
                "expected": None,
                "line_num": line_num,
                "raw": raw,
            }

        # --- Type / mnemonic detection ---
        type_name = None
        mnemonic = None
        first = tokens[0]

        # Try: first token is a TYPE group name
        resolved_type = self._resolve_type(first)
        if resolved_type is not None:
            type_name = resolved_type
            idx = 1
            # Skip optional 'CMD' decorative keyword after type
            # e.g. "CORE CMD NOP" — 'CMD' is not the mnemonic
            if len(tokens) > idx and tokens[idx] == "CMD":
                idx += 1
            if idx >= len(tokens):
                self._warning_callback(
                    f'{line_num}: Type "{type_name}" specified but no mnemonic follows.',
                    'TYPE MNEMONIC [param ...]'
                )
                return None
            mnemonic = tokens[idx]
            idx += 1
        else:
            # No type prefix: first token IS the mnemonic
            mnemonic = first
            idx = 1

        # Validate mnemonic exists in the command table
        if mnemonic not in self._mnemonic_map:
            self._warning_callback(
                f'{line_num}: Unknown command mnemonic "{mnemonic}".',
                'See command table for valid mnemonics'
            )
            return None

        # --- Parameters and expected reply ---
        params: list[str] = []
        expected: str | None = None

        remaining = tokens[idx:]
        eq_idx = None
        for i, tok in enumerate(remaining):
            if tok == "=":
                eq_idx = i
                break

        if eq_idx is not None:
            params = remaining[:eq_idx]
            expected_tokens = remaining[eq_idx + 1 :]
            expected = " ".join(expected_tokens) if expected_tokens else ""
        else:
            params = remaining

        return {
            "type": type_name or "",
            "mnemonic": mnemonic,
            "params": params,
            "expected": expected,
            "line_num": line_num,
            "raw": raw,
        }

    # ------------------------------------------------------------------
    # Type resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_type(token: str) -> str | None:
        """Check if a token is a recognized type group name."""
        if token in TYPE_NAMES:
            return token
        # Try stripping _CMD suffix (e.g., CORE_CMD → CORE)
        if token.endswith("_CMD") and token[:-4] in TYPE_NAMES:
            return token[:-4]
        return None

    # ------------------------------------------------------------------
    # Table indexing
    # ------------------------------------------------------------------

    @staticmethod
    def _build_mnemonic_map() -> dict[str, tuple]:
        """Build {mnemonic: (prefix_byte, opcode_or_None, cmd_entry_or_None)}.

        Iterates over the full CT table structure, handling:
        - Direct command tuples  (0x88: ('MOVE_ABS_XY', ...))
        - Sub-command dicts      (0x80: {0x00: ('MOVE_ABS_X', ...)})
        - Nested sub-command     (0xCA: {0x01: {0x00: 'LAYER_END', ...}})
        - Plain string commands  (0xCE: 'ENQ')
        - External refs          (0xA7: KT) — skipped
        """
        _map: dict[str, tuple] = {}

        for prefix_byte, entry in CT.items():
            # --- Direct command tuple ---
            # e.g. 0x88: ('MOVE_ABS_XY', XABSCOORD, YABSCOORD)
            if isinstance(entry, tuple):
                if len(entry) >= 1 and isinstance(entry[0], str):
                    _map[entry[0]] = (prefix_byte, None, entry)
                continue

            # --- Plain string command ---
            # e.g. 0xCE: 'ENQ'
            if isinstance(entry, str):
                name = entry.strip()
                # Skip non-command strings like '\n ---- EOF ----'
                if name and not name.startswith("\n") and len(name) < 40:
                    _map[name] = (prefix_byte, None, None)
                continue

            # --- Dictionary: sub-commands or external reference ---
            if isinstance(entry, dict):
                # Detect external refs (KT, IDXT, RT) by checking for
                # non-standard structures (list values, etc.)
                has_list_val = any(isinstance(v, list) for v in entry.values())
                if has_list_val:
                    continue  # Skip external refs like KT

                for opcode, sub_entry in entry.items():
                    if isinstance(sub_entry, str):
                        _map[sub_entry] = (prefix_byte, opcode, None)
                    elif isinstance(sub_entry, tuple):
                        if len(sub_entry) >= 1 and isinstance(sub_entry[0], str):
                            _map[sub_entry[0]] = (prefix_byte, opcode, sub_entry)
                    elif isinstance(sub_entry, dict):
                        # Nested sub-commands (e.g., 0xCA: {0x01: {0x00: ...}})
                        for opcode2, nested in sub_entry.items():
                            if isinstance(nested, str):
                                _map[nested] = (prefix_byte, opcode, opcode2, None)
                            elif isinstance(nested, tuple):
                                if len(nested) >= 1 and isinstance(nested[0], str):
                                    _map[nested[0]] = (
                                        prefix_byte,
                                        opcode,
                                        opcode2,
                                        nested,
                                    )

        return _map

    @staticmethod
    def _build_mt_map() -> dict[str, tuple[int, int]]:
        """Build {mnemonic: (msb, lsb)} mapping from MT table.

        Enables resolution of memory-address mnemonics (e.g. MEM_IO_ENABLE)
        to their MSB/LSB byte pair for encoding.
        """
        _map: dict[str, tuple[int, int]] = {}
        for msb, entries in MT.items():
            for lsb, entry in entries.items():
                if isinstance(entry, tuple) and len(entry) >= 1:
                    name = entry[0]
                    if isinstance(name, str):
                        _map[name] = (msb, lsb)
        return _map

    # ------------------------------------------------------------------
    # Utility (exposed for testing / debugging)
    # ------------------------------------------------------------------

    @property
    def mnemonic_map(self) -> dict[str, tuple]:
        """The resolved mnemonic → encoding-info map."""
        return self._mnemonic_map

    @property
    def mt_map(self) -> dict[str, tuple[int, int]]:
        """The resolved MT-mnemonic → (msb, lsb) map."""
        return self._mt_map


# ═══════════════════════════════════════════════════════════════════════
# ScriptInterpreter
# ═══════════════════════════════════════════════════════════════════════


class ScriptInterpreter:
    """Interprets parsed script commands and generates tshark-compatible output.

    Walks a list of parsed command dicts (from ScriptParser), looks up
    each command in the CT command table, encodes parameters to binary,
    and writes tshark-format lines to the output stream.
    """

    # Default UDP endpoint (controller port range)
    DEFAULT_SRC_PORT = 40200
    DEFAULT_DST_PORT = 50200

    def __init__(self, output_stream, warning_callback=None):
        self._out = output_stream
        self._warning_callback = warning_callback or ScriptParser._default_warning
        self._parser = ScriptParser(warning_callback=warning_callback)
        self._enc = RdEncoder()
        self._swizzler = RpaSwizzler(magic=0x88)
        self._timestamp = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def interpret(self, commands: list[dict]) -> None:
        """Process a list of parsed commands, writing tshark output.

        Commands between NEW_PACKET markers are combined into single UDP packets.
        Synthetic ACK replies are emitted immediately after each command packet.
        Commands with an ``expected`` value get a synthetic reply packet emitted
        after the batch packet (and ACK) that carried them.

        File checksum: accumulates sum(raw) for eligible commands, then either
        verifies END_JOB or fills a placeholder at EOF.

        If any command has type SESSION_START or SESSION_END, switches to session
        mode: creates RdSession/RdDriver, connects, and routes commands through
        the live session instead of generating tshark output.
        """
        # Check for session commands
        session_active = any(
            cmd.get("type") in ("SESSION_START", "SESSION_END") for cmd in commands
        )

        if session_active:
            self._interpret_session(commands)
            return

        self._timestamp = 0.0
        current_batch = bytearray()
        pending_commands: list[dict] = []
        file_checksum = 0
        end_job_value = None
        end_job_offset = None
        end_job_batch = bytearray()  # reference to batch containing placeholder
        for cmd in commands:
            if cmd.get("type") == "NEW_PACKET":
                # Flush current batch as a single combined packet,
                # emit ACK immediately after the command packet,
                # then emit synthetic replies for any commands with expected values.
                if current_batch:
                    self._emit_packet(current_batch)
                    # Emit ACK immediately after the command packet
                    self._out.write(self._encode_ack(self._timestamp) + "\n")
                    self._timestamp += 0.0003
                    # Then emit expected replies
                    for pending in pending_commands:
                        reply_line = self._encode_reply(pending)
                        if reply_line:
                            self._out.write(reply_line + "\n")
                    current_batch = bytearray()
                    pending_commands = []
                continue

            # Accumulate raw command bytes into the current batch
            raw = self._encode_raw(cmd)

            # === File Checksum Logic ===
            skip_append = False
            if is_end_job(cmd, self._parser.mnemonic_map):
                if end_job_value is not None:
                    self._warning_callback(
                        "Duplicate END_JOB — at most one per file",
                        "END_JOB [=<checksum>] (at most one per file)"
                    )
                    skip_append = True
                elif cmd["params"]:
                    end_job_value = parse_value(
                        cmd["params"][0], "checksum", "uint_35"
                    )
                else:
                    raw.extend(b"\x00" * 5)  # placeholder; filled after EOF
                    end_job_offset = len(current_batch) + len(raw) - 5
                    end_job_batch = (
                        current_batch  # track which batch has the placeholder
                    )
                # raw is skipped on duplicate (skip_append=True)
            elif should_include_in_checksum(cmd, self._parser.mnemonic_map):
                file_checksum += sum(raw)
            # EOF (0xD7) is handled implicitly: should_include_in_checksum returns True
            # since 0xD7 is not in CHK_DISABLES and is not END_JOB.
            # sum(raw) naturally includes 0xD7.
            # =========================

            if not skip_append:
                current_batch.extend(raw)

            # Track commands that have an expected reply value
            if cmd.get("expected"):
                pending_commands.append(cmd)

        # === Patch placeholder BEFORE final emit ===
        if end_job_offset is not None and end_job_value is None:
            encoded_sum = self._enc.encode_uint35(file_checksum)
            if end_job_batch is not current_batch:
                # The batch containing the END_JOB placeholder was already flushed
                # by a NEW_PACKET directive. This is a script structure error.
                self._warning_callback(
                    "END_JOB without value must appear in the final batch "
                    "(cannot be before a NEW_PACKET directive)",
                    "END_JOB without value must be in the final packet batch"
                )
            current_batch[end_job_offset : end_job_offset + 5] = encoded_sum

        # Flush the final batch (now with correct checksum bytes)
        if current_batch:
            self._emit_packet(current_batch)
            # Emit ACK for the final packet too
            self._out.write(self._encode_ack(self._timestamp) + "\n")
            self._timestamp += 0.0003
            for pending in pending_commands:
                reply_line = self._encode_reply(pending)
                if reply_line:
                    self._out.write(reply_line + "\n")

        # === Post-loop: verify END_JOB ===
        if end_job_value is not None:
            if file_checksum != end_job_value:
                raise ValueError(
                    f"END_JOB value {end_job_value} does not match "
                    f"accumulated file checksum {file_checksum}"
                )

    # ------------------------------------------------------------------
    # Session execution
    # ------------------------------------------------------------------

    def _interpret_session(self, commands: list[dict]) -> None:
        """Execute parsed commands through a live Ruida session.

        Handles SESSION_START → create/connect/driver, SESSION_END → stop/cleanup,
        and routes intermediate commands through driver.run() via reconstruct_script_line().
        """
        driver = None

        for cmd in commands:
            if cmd["type"] == "SESSION_START":
                from ruidadriver.ruida_driver import RdDriver

                params = cmd.get("params", {})
                driver = RdDriver()
                opened = driver.start(
                    udp_host=params.get("udp", ""),
                    usb_device=params.get("usb", ""),
                )
                if not opened:
                    self._out.write(
                        f"# ERROR: Failed to open transport to Ruida controller "
                        f"(udp={params.get('udp', '')}, usb={params.get('usb', '')})\n"
                    )
                    return

            elif cmd["type"] == "SESSION_END":
                if driver is not None:
                    driver.stop()
                    driver = None

            else:
                if driver is None:
                    raise RuntimeError(
                        "Cannot execute command without active session. "
                        "Use 'session start' first."
                    )
                line = reconstruct_script_line(cmd)
                driver.run([line])

        # Clean up if session wasn't explicitly ended
        if driver is not None:
            driver.stop()

    # ------------------------------------------------------------------
    # Command encoding
    # ------------------------------------------------------------------

    def _encode_raw(self, cmd: dict) -> bytearray:
        """Encode a single command to raw bytes (unswizzled, no checksum).

        Delegates to the standalone encode_command function.
        Returns empty bytearray on encoding failure (error already warned).
        """
        try:
            return encode_command(
                cmd, self._parser.mnemonic_map, self._parser.mt_map, self._enc
            )
        except ValueError as e:
            import logging
            logging.warning(
                f"Line {cmd.get('line_num', '?')}: Encoding error: {e}  |  "
                f"Syntax: See command table for valid mnemonics/params"
            )
            return bytearray()

    def _emit_packet(self, raw: bytearray) -> None:
        """Swizzle, checksum, and emit a single packet as a tshark line.

        Takes combined raw command bytes (unswizzled, no checksum),
        applies the forward swizzle, prepends a 2-byte big-endian checksum,
        and writes a single tshark-format line.
        """
        if not raw:
            return

        # Swizzle all raw bytes
        swizzled = self._swizzler.swizzle(raw)

        # Calculate checksum = sum of swizzled bytes
        chk = sum(swizzled) & 0xFFFF

        # Build final binary: checksum (2 bytes big-endian) + swizzled data
        binary = bytearray(
            [
                (chk >> 8) & 0xFF,
                chk & 0xFF,
            ]
        )
        binary.extend(swizzled)

        # Build tshark line
        ts = f"{self._timestamp:.6f}"
        src_dst = f"{self.DEFAULT_SRC_PORT},{self.DEFAULT_DST_PORT}"
        pkt_len = len(binary) + 8  # UDP header is 8 bytes
        hex_data = binary.hex()

        line = f"{ts}\t{src_dst}\t{pkt_len}\t{hex_data}"
        self._out.write(line + "\n")

        self._timestamp += 0.001

    def _encode_ack(self, ts: float) -> str:
        """Generate a tshark-format ACK reply line (controller→host).

        Produces a synthetic 1-byte ACK packet compatible with rpa.py's
        check_handshake() method. The raw byte is the swizzled form of
        0xCC (ACK) using magic 0x88.
        """
        src_dst = f"{self.DEFAULT_DST_PORT},{self.DEFAULT_SRC_PORT}"
        raw_ack = RpaSwizzler.swizzle_byte(ACK, 0x88)
        return f"{ts:.6f}\t{src_dst}\t9\t{raw_ack:02x}"

    def _encode_reply(self, cmd: dict) -> str:
        """Generate a tshark-format reply packet for a command with expected value.

        Controller→host replies do not carry a checksum.  The data bytes are
        swizzled (same magic 0x88).  Ports are reversed (50200→40200).

        Returns the tshark line string, or an empty string if no reply can be
        generated (unknown MEM mnemonic, unknown type, TBD types, etc.).
        """
        expected = cmd.get("expected", "")
        if not expected or expected in ("?", "*"):
            return ""

        params = cmd.get("params", [])
        if not params:
            return ""

        mem_mnemonic = params[0]
        mt_map = self._parser.mt_map
        if mem_mnemonic not in mt_map:
            return ""

        msb, lsb = mt_map[mem_mnemonic]

        # Look up the MT entry to get the parameter spec tuple
        mt_entry = MT.get(msb, {}).get(lsb)
        if mt_entry is None or len(mt_entry) < 2:
            return ""

        spec = mt_entry[1]  # Type spec, e.g. CARD_ID, XABSCOORD, TBDU35
        if not isinstance(spec, tuple) or len(spec) < 2:
            return ""

        decoder_name = spec[1]
        rd_type = spec[2] if len(spec) >= 3 else None

        # Skip unknown / TBD types
        if decoder_name in ("tbd",):
            return ""

        # Parse the expected value string into a Python value
        parsed = self._parse_value(expected, decoder_name, rd_type)

        # Choose and call the appropriate encoder
        raw: bytearray
        if decoder_name == "card_id":
            raw = self._enc.encode_card_id(parsed)
        elif decoder_name == "dim":
            dim_nbytes = {"int_14": 2, "uint_14": 2, "int_35": 5, "uint_35": 5}.get(
                rd_type, 5
            )
            raw = self._enc.encode_dim(parsed, dim_nbytes)
        else:
            method_name = _ENCODER_MAP.get(decoder_name)
            if method_name is None and rd_type is not None:
                method_name = _RDTYPE_ENCODER_MAP.get(rd_type)
            if method_name is None:
                return ""
            encoder = getattr(self._enc, method_name, None)
            if encoder is None:
                return ""
            raw = encoder(parsed)

        if not raw:
            return ""

        # Build reply framing + data bytes.
        # The parser's state machine expects:
        #   _st_mt_command → _st_mt_sub_command → _st_mt_address_msb
        #   → _st_mt_address_lsb → _st_mt_decode_reply
        #   Reply command byte: 0xDA (SETTING, bit 7 set → _h_is_command)
        #   Reply sub-command: 0x01 (GET_SETTING in RT, not 0x00 in CT)
        #   Address MSB/LSB: from the MT entry being queried
        framing = bytearray([0xDA, 0x01, msb & 0xFF, lsb & 0xFF])
        full_reply = framing + raw

        # Swizzle all reply bytes (replies use the same magic 0x88)
        swizzled = self._swizzler.swizzle(full_reply)

        # Controller→host: no checksum, ports reversed
        ts = f"{self._timestamp:.6f}"
        src_dst = f"{self.DEFAULT_DST_PORT},{self.DEFAULT_SRC_PORT}"
        pkt_len = len(swizzled) + 8
        hex_data = swizzled.hex()

        # Advance timestamp for the reply
        self._timestamp += 0.0002

        return f"{ts}\t{src_dst}\t{pkt_len}\t{hex_data}"

    # ------------------------------------------------------------------
    # Parameter encoding
    # ------------------------------------------------------------------

    def _encode_params(
        self,
        param_specs: tuple,
        param_values: list[str],
        cmd: dict,
    ) -> bytearray:
        """Encode script parameter values into binary using CT param specs.

        Delegates to the standalone encode_params function.
        """
        return encode_params(
            param_specs,
            param_values,
            cmd,
            self._parser.mnemonic_map,
            self._parser.mt_map,
            self._enc,
        )

    def _encode_single_param(
        self,
        decoder_fn: str,
        rd_type: str | None,
        value_token: str,
        cmd: dict,
    ) -> bytearray:
        """Encode a single parameter value to binary.

        Delegates to the standalone encode_single_param function.
        """
        return encode_single_param(
            decoder_fn,
            rd_type,
            value_token,
            cmd,
            self._parser.mnemonic_map,
            self._parser.mt_map,
            self._enc,
        )

    def _encode_mt_param(self, value_token: str, cmd: dict) -> bytearray:
        """Encode a memory/index parameter.

        Delegates to the standalone encode_mt_param function.
        """
        return encode_mt_param(value_token, self._parser.mt_map)

    # ------------------------------------------------------------------
    # Value parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_value(
        token: str,
        decoder_fn: str | None,
        rd_type: str | None,
    ):
        """Parse a script parameter token into a Python value.

        Delegates to the standalone parse_value function.
        """
        return parse_value(token, decoder_fn, rd_type)
