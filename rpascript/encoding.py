"""
Standalone command encoding functions for rpascript (Ruida Discovery Script).

Extracted from ScriptInterpreter to provide a pure, reusable encoding path
for both ScriptInterpreter (tshark output) and Background Script Runner
(queue-based wire transmission).

All functions are pure — same inputs always produce same outputs, no side effects.
"""

from typing import Any

import protocols.ruida.ruida_protocol as rdap
from rpalib.ruida_transcoder import RdEncoder

# Decoder function name (DDEC from param spec) → RdEncoder method name.
_ENCODER_MAP: dict[str, str | None] = {
    "int7": "encode_int7",
    "uint7": "encode_uint7",
    "int14": "encode_int14",
    "uint14": "encode_uint14",
    "int35": "encode_int35",
    "uint35": "encode_uint35",
    "color": "encode_color",
    "coord": "encode_coord",
    "cstring": "encode_cstring",
    "string8": "encode_string8",
    "power": "encode_power",
    "frequency": "encode_frequency",
    "speed": "encode_speed",
    "time": "encode_time",
    "bool": "encode_bool",
    "on_off": "encode_bool",
    "rel": "encode_uint7",
    "axis": "encode_uint7",
    "mt": "encode_mt",
    "index": "encode_index",
    "checksum": "encode_uint35",
    "card_id": "encode_uint35",
    "tbd": None,
}

# Fallback: ruida type name (DTYP from param spec) → encoder method.
_RDTYPE_ENCODER_MAP: dict[str, str | None] = {
    "int_7": "encode_int7",
    "uint_7": "encode_uint7",
    "int_14": "encode_int14",
    "uint_14": "encode_uint14",
    "int_35": "encode_int35",
    "uint_35": "encode_uint35",
    "cstring": "encode_cstring",
    "string8": "encode_string8",
    "on_off": "encode_bool",
    "bool_7": "encode_bool",
    "mt": "encode_mt",
    "index": "encode_index",
}


# ------------------------------------------------------------------
# Checksum helper functions
# ------------------------------------------------------------------


def _get_prefix_byte(cmd: dict, mnemonic_map: dict) -> int | None:
    """Extract the command prefix byte from the mnemonic map, or None if unknown."""
    info = mnemonic_map.get(cmd["mnemonic"])
    if info is None:
        return None
    return info[0]


def should_include_in_checksum(cmd: dict, mnemonic_map: dict) -> bool:
    """Return True if this command's encoded bytes should be included in file_checksum.

    Excludes commands that the parser skips (CHK_DISABLES: 0xA7 KEYPRESS, 0xDA SETTING)
    and the END_JOB command itself (0xE5→0x05).
    """
    info = mnemonic_map.get(cmd["mnemonic"])
    if info is None:
        return False
    prefix = info[0]

    # Commands with prefix in CHK_DISABLES don't participate in checksum
    if prefix in rdap.CHK_DISABLES:
        return False

    # END_JOB command (0xE5 → 0x05) excluded — its bytes are not part
    # of the checksum value it carries
    if prefix == rdap.FILE_COMMAND and len(info) >= 2 and info[1] == 0x05:
        return False

    return True


def is_eof_command(cmd: dict, mnemonic_map: dict) -> bool:
    """Return True if this command is the EOF (end-of-file) marker.

    Documentation clarity only — EOF is implicitly handled via should_include_in_checksum
    since 0xD7 is not in CHK_DISABLES and is not END_JOB, so its sum(raw) = 0xD7
    is naturally included in the accumulation.
    """
    prefix = _get_prefix_byte(cmd, mnemonic_map)
    return prefix == rdap.EOF


def is_end_job(cmd: dict, mnemonic_map: dict) -> bool:
    """Return True if this command is END_JOB (0xE5 → 0x05)."""
    info = mnemonic_map.get(cmd["mnemonic"])
    if info is None:
        return False
    prefix = info[0]
    if prefix != rdap.FILE_COMMAND or len(info) < 2:
        return False
    return info[1] == 0x05


def _get_cmd_entry(info: tuple) -> tuple | None:
    """Extract the command entry (param specs) from a mnemonic_map info tuple.

    Info tuples are either 3-tuples (prefix, opcode_or_None, cmd_entry_or_None)
    or 4-tuples (prefix, middle_opcode, inner_opcode, cmd_entry_or_None).
    """
    if len(info) == 4:
        return info[3] if len(info) > 3 else None
    return info[2] if len(info) > 2 else None


def encode_command(
    cmd: dict,
    mnemonic_map: dict[str, tuple],
    mt_map: dict[str, tuple[int, int]],
    encoder: RdEncoder,
) -> bytearray:
    """Encode a parsed command dict to raw bytes (unswizzled, no checksum).

    Args:
        cmd: Parsed command dict from ScriptParser.parse_lines()
        mnemonic_map: ScriptParser.mnemonic_map (CT command lookups)
        mt_map: ScriptParser.mt_map (memory-address mnemonic lookups)
        encoder: RdEncoder instance for parameter encoding

    Returns:
        bytearray of encoded command bytes
    """
    mnemonic = cmd["mnemonic"]
    info = mnemonic_map.get(mnemonic)
    if info is None:
        raise ValueError(f'{cmd["line_num"]}: Unknown mnemonic "{mnemonic}"')

    prefix_byte = info[0]
    raw = bytearray([prefix_byte])

    if len(info) == 4:
        # Nested option command: (prefix, middle_opcode, inner_opcode, cmd_entry_or_None)
        raw.append(info[1] & 0x7F)
        raw.append(info[2] & 0x7F)
    elif info[1] is not None:
        raw.append(info[1] & 0x7F)

    cmd_entry = _get_cmd_entry(info)
    if cmd_entry is not None and len(cmd_entry) > 1 and cmd["params"]:
        param_specs = cmd_entry[1:]  # Skip command name at index 0
        param_values = cmd["params"]
        raw.extend(
            encode_params(param_specs, param_values, cmd, mnemonic_map, mt_map, encoder)
        )

    return raw


def encode_params(
    param_specs: tuple,
    param_values: list[str],
    cmd: dict,
    mnemonic_map: dict[str, tuple],
    mt_map: dict[str, tuple[int, int]],
    encoder: RdEncoder,
) -> bytearray:
    """Encode script parameter values into binary using CT param specs.

    Each param spec is a tuple (format_str, decoder_fn, rd_type).
    The decoder function name determines which encoder to use.
    """
    result = bytearray()

    for i, (spec, value_token) in enumerate(zip(param_specs, param_values)):
        if not isinstance(spec, tuple) or len(spec) < 2:
            continue

        decoder_fn = spec[1]  # DDEC — e.g. 'coord', 'mt', 'int35'
        rd_type = spec[2] if len(spec) >= 3 else None

        encoded = encode_single_param(
            decoder_fn, rd_type, value_token, cmd, mnemonic_map, mt_map, encoder
        )
        result.extend(encoded)

    return result


def encode_single_param(
    decoder_fn: str,
    rd_type: str | None,
    value_token: str,
    cmd: dict,
    mnemonic_map: dict[str, tuple],
    mt_map: dict[str, tuple[int, int]],
    encoder: RdEncoder,
) -> bytearray:
    """Encode a single parameter value to binary."""
    # --- MT / Index special handling ---
    if decoder_fn in ("mt", "index"):
        return encode_mt_param(value_token, mt_map)

    # --- Named-param split: 'X=200.000mm' → '200.000mm' ---
    if "=" in value_token:
        _, value_token = value_token.split("=", 1)

    # --- Parse the value token to a Python value ---
    parsed = parse_value(value_token, decoder_fn, rd_type)

    # --- Determine the encoder method ---
    method_name = _ENCODER_MAP.get(decoder_fn)
    if method_name is None and rd_type is not None:
        method_name = _RDTYPE_ENCODER_MAP.get(rd_type)

    if method_name is None:
        return bytearray()

    # Special handling for coord which needs rd_type to select byte count
    if decoder_fn == "coord":
        coord_nbytes = {"int_14": 2, "uint_14": 2, "int_35": 5, "uint_35": 5}.get(
            rd_type, 5
        )
        return encoder.encode_coord(parsed, coord_nbytes)

    # Resolve method on RdEncoder instance
    encoder_method = getattr(encoder, method_name, None)
    if encoder_method is None:
        return bytearray()

    return encoder_method(parsed)


def encode_mt_param(
    value_token: str,
    mt_map: dict[str, tuple[int, int]],
) -> bytearray:
    """Encode a memory/index parameter.

    Looks up the token in the MT mnemonic table and encodes
    its MSB/LSB address pair.
    """
    if value_token in mt_map:
        msb, lsb = mt_map[value_token]
    else:
        # Try numeric address (e.g. "1568" for Addr:0620)
        try:
            addr = int(value_token, 0)
            msb = (addr >> 8) & 0xFF
            lsb = addr & 0xFF
        except ValueError:
            raise ValueError(f"Unknown memory address mnemonic: {value_token}")

    return bytearray([msb & 0x7F, lsb & 0x7F])


def parse_value(
    token: str,
    decoder_fn: str | None,
    rd_type: str | None,
) -> Any:
    """Parse a script parameter token into a Python value.

    Handles numeric values, typed suffixes (mm, %, KHz, etc.),
    booleans, and plain strings.
    """
    raw = token.strip()

    # --- Unescape \# → # (from rds comment escaping) ---
    raw = raw.replace("\\#", "#")

    # --- Strip format-string label prefix ---
    # Parameters from format strings like 'Speed:{:.3f}mm/S' or 'State: {}'
    # include labels (e.g. 'Speed:', 'Power:', 'Freq:', 'CardID:') that must
    # be removed before parsing.  Skip strings with '=' which are already
    # split by encode_single_param, and skip strings where the part before
    # ':' is not a plain label.
    if ":" in raw and "=" not in raw:
        maybe_label, after = raw.split(":", 1)
        if maybe_label.isalpha():
            raw = after.strip()

    # --- Boolean ---
    if raw.upper() in ("ON", "TRUE", "1"):
        return True
    if raw.upper() in ("OFF", "FALSE", "0"):
        return False

    # --- Typed suffix parsing ---
    if decoder_fn == "coord":
        clean = raw
        if "=" in clean:
            clean = clean.split("=", 1)[1].strip()
        for suffix in ("mm", "MM"):
            if clean.lower().endswith(suffix):
                clean = clean[: -len(suffix)]
                break
        return float(clean)

    if decoder_fn == "power":
        if raw.endswith("%"):
            return float(raw[:-1])
        return float(raw)

    if decoder_fn == "frequency":
        clean = raw
        for suffix in ("KHz", "khz", "KHz", "kHz"):
            if clean.endswith(suffix):
                clean = clean[: -len(suffix)]
                break
        return float(clean)

    if decoder_fn == "speed":
        clean = raw
        for suffix in ("mm/S", "mm/s", "MM/S"):
            if clean.endswith(suffix):
                clean = clean[: -len(suffix)]
                break
        return float(clean)

    if decoder_fn == "time":
        clean = raw
        for suffix in ("mS", "ms", "MS"):
            if clean.endswith(suffix):
                clean = clean[: -len(suffix)]
                break
        return float(clean)

    if decoder_fn == "cstring":
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            return raw[1:-1]
        return raw
    if decoder_fn == "string8":
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            return raw[1:-1]
        return raw

    # --- Numeric ---
    # Handle hex prefixed with # (e.g. Color:#FF00FFFF)
    if raw.startswith("#"):
        try:
            return int(raw[1:], 16)
        except ValueError:
            pass

    # Handle 0x/0X prefixed hex (e.g. 0x0000048216 from Sum:0x{...} format strings)
    if raw.startswith(("0x", "0X")):
        try:
            return int(raw, 0)
        except ValueError:
            pass

    try:
        return int(raw)
    except ValueError:
        pass

    # TBDU-style format values (e.g. "TBDU35:0000001b: 0x1a: 26") contain
    # colon-separated representations of the same value. Extract the last
    # segment (the decimal integer) for round-trip parsing.
    if ":" in raw:
        parts = raw.rsplit(":", 1)
        if len(parts) == 2:
            try:
                return int(parts[1].strip())
            except ValueError:
                pass

    try:
        return float(raw)
    except ValueError:
        pass

    # --- RELT table resolution ---
    if decoder_fn == "rel":
        rev_rel = {v: k for k, v in rdap.RELT.items()}
        if raw in rev_rel:
            return rev_rel[raw]

    # --- Axis label-to-value resolution ---
    if decoder_fn == "axis":
        rev_axis = {v: k for k, v in rdap.AXIS_T.items()}
        if raw in rev_axis:
            return rev_axis[raw]

    # --- MStat label-to-bitmask resolution ---
    # When the decoded reply is a human-readable label (e.g. 'Job Running')
    # instead of a hex number, map it back to its numeric bitmask so the
    # encoder can produce the correct binary value.
    if decoder_fn == "m_stat":
        label_to_bit = {lbl: bit for bit, lbl in rdap.MST}
        parts = [p.strip() for p in raw.split(",")]
        result = 0
        for part in parts:
            if part in label_to_bit:
                result |= label_to_bit[part]
            else:
                # Unknown label — skip; the round-trip will be inexact but
                # won't crash.  The loss is acceptable since m_stat labels
                # are informational and the numeric value is captured in the
                # original capture log, not the script file.
                pass
        # If at least one label was resolved, return the bitmask;
        # otherwise fall through to the string fallback below.
        if result or parts == [""]:
            return result

    # --- Card ID name-to-value resolution ---
    if decoder_fn == "card_id":
        numeric_id = rdap.CARD_IDS_BY_NAME.get(raw)
        if numeric_id is None:
            raise ValueError(f"Unknown card model name: {raw!r}")
        return numeric_id

    # --- Fallback: return as string ---
    return raw


def is_resolvable_address(token: str, mt_map: dict) -> bool:
    """Check if a GET_SETTING address token can be resolved (MT mnemonic or numeric).

    Args:
        token: The address token (e.g. "MEM_CARD_ID", "0x0400", "1024").
        mt_map: The MT mnemonic-to-entry mapping from ScriptParser._mt_map.

    Returns:
        True if the token is a known MT mnemonic or a parseable integer.
    """
    if token in mt_map:
        return True
    try:
        int(token, 0)
        return True
    except ValueError:
        return False
