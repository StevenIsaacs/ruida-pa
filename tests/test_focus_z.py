"""Tests for the Z auto-focus command.

On an RDC8445S, D8 2E runs the same probe routine as the controller
panel's Focus key.
"""

import protocols.ruida.ruida_protocol as rdap
from rpascript.interpreter import ScriptParser
from ruidadriver.rd_gluescript import GlueScript


def test_focus_z_generates_focus_command():
    assert GlueScript().focus_z() == ["FOCUS_Z"]


def test_focus_z_is_a_live_only_home_command():
    assert "focus_z" in GlueScript.HOME_COMMANDS


def test_focus_z_mnemonic_maps_to_d8_2e():
    assert ScriptParser().mnemonic_map["FOCUS_Z"][:2] == (0xD8, 0x2E)


def test_focus_config_memory_address():
    assert rdap.MT[0x03][0x0F][0] == "MEM_FOCUS_CONFIG"
