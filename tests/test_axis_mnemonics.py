"""Tests for the per-axis X/Y/Z/U mnemonic renames and their deprecated
_1.._4 aliases in the MT memory table."""

import ruida_pa.protocols.ruida.ruida_protocol as rdap
from ruida_pa.rpalib.ruida_transcoder import RdEncoder
from ruida_pa.rpascript.interpreter import ScriptParser
from ruida_pa.ruidadriver.ruida_driver import RdDriver

AXIS_SUFFIXES = ("X", "Y", "Z", "U")
ROWS = (
    "PRECISION",
    "MAX_VELOCITY",
    "START_VELOCITY",
    "MAX_ACC",
    "TRAVEL",
    "BTN_START_VEL",
    "BTN_ACC",
    "ESTP_ACC",
    "HOME_OFFSET",
)


def _canonical(row, axis):
    """The canonical mnemonic for a sweep row and axis.

    MEM_BED_SIZE_X/Y are retained as the canonical X/Y travel names.
    """
    if row == "TRAVEL" and axis in ("X", "Y"):
        return f"MEM_BED_SIZE_{axis}"
    return f"MEM_{row}_{axis}"


def test_axis_mnemonics_use_typed_specs():
    # Every renamed canonical entry must be typed, not TBDU35.
    for row in ROWS:
        for axis in AXIS_SUFFIXES:
            mnemonic = _canonical(row, axis)
            found = [
                entry
                for block in rdap.MT.values()
                for entry in block.values()
                if isinstance(entry, tuple)
                and len(entry) >= 1
                and entry[0] == mnemonic
            ]
            assert found, f"{mnemonic} missing from MT"
            assert found[0][1] is not rdap.TBDU35, f"{mnemonic} still TBDU35"


def test_bed_size_names_are_canonical():
    assert rdap.MT[0x00][0x26][0] == "MEM_BED_SIZE_X"
    assert rdap.MT[0x00][0x36][0] == "MEM_BED_SIZE_Y"
    assert "MEM_BED_SIZE_X" not in rdap.MT_MNEMONIC_ALIASES
    assert "MEM_BED_SIZE_Y" not in rdap.MT_MNEMONIC_ALIASES


def test_deprecated_axis_aliases_resolve_to_canonical_address():
    mt_map = ScriptParser()._mt_map
    for alias, canonical in rdap.MT_MNEMONIC_ALIASES.items():
        assert alias in mt_map, f"missing alias {alias}"
        assert mt_map[alias] == mt_map[canonical], (
            f"{alias} -> {mt_map[alias]} != {canonical} -> {mt_map[canonical]}"
        )


def test_canonical_addresses_match_per_axis_blocks():
    # X=0x002_, Y=0x003_, Z=0x004_, U=0x005_.
    base_lsb = {"X": 0x20, "Y": 0x30, "Z": 0x40, "U": 0x50}
    offsets = (0x01, 0x03, 0x04, 0x05)  # PRECISION, MAX_VELOCITY, START_VELOCITY, MAX_ACC
    for i, row in enumerate(("PRECISION", "MAX_VELOCITY", "START_VELOCITY", "MAX_ACC")):
        for axis in AXIS_SUFFIXES:
            lsb = base_lsb[axis] + offsets[i]
            assert ScriptParser()._mt_map[f"MEM_{row}_{axis}"] == (0x00, lsb)


def test_format_reply_uses_typed_labels():
    # MEM_MAX_VELOCITY_X (0x0023): raw 500000 -> 500 mm/s.
    reply = bytearray(
        [0xDA, 0x01, 0x00, 0x23, *RdEncoder().encode_uint35(500000)]
    )
    assert RdDriver.format_reply(reply) == (
        "MEM_MAX_VELOCITY_X: Vel:500.000mm/S"
    )
    # MEM_BED_SIZE_X (0x0026): raw 1300000 -> 1300.0 mm, X= absolute form.
    travel = bytearray(
        [0xDA, 0x01, 0x00, 0x26, *RdEncoder().encode_uint35(1300000)]
    )
    assert RdDriver.format_reply(travel) == (
        "MEM_BED_SIZE_X: X=1300.000mm"
    )


def test_features_script_uses_bed_size_names():
    assert "GET_SETTING MEM_BED_SIZE_X" in RdDriver._FEATURES_SCRIPT
    assert "GET_SETTING MEM_BED_SIZE_Y" in RdDriver._FEATURES_SCRIPT
    assert not any("MEM_TRAVEL" in line for line in RdDriver._FEATURES_SCRIPT)


def test_unconfirmed_blocks_stay_numbered_tbd():
    # CONTROL_PARA and BACKLASH were not in the confirmation sweep.
    mt = ScriptParser()._mt_map
    assert mt["MEM_AXIS_CONTROL_PARA_1"] == (0x00, 0x20)
    assert rdap.MT[0x00][0x2B][0] == "MEM_AXIS_BACKLASH_1"
    assert rdap.MT[0x00][0x2B][1] is rdap.TBDU35


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
