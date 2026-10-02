"""Tests for selecting the network protocol with `session start proto=`."""

import asyncio
import io
from unittest.mock import Mock

import pytest

import ruidadriver.ruida_driver as ruida_driver
from rpascript.interpreter import ScriptInterpreter, ScriptParser
from rpascript.tui_adapter import TuiAdapter
from ruidadriver.rd_transport import parse_network_protocol


@pytest.mark.parametrize(
    "value, expected",
    [("tcp", "tcp"), ("TCP", "tcp"), (" udp ", "udp"), (None, None), ("", None)],
)
def test_parse_network_protocol(value, expected):
    assert parse_network_protocol(value) == expected


def test_parse_network_protocol_rejects_unknown():
    with pytest.raises(ValueError):
        parse_network_protocol("sctp")


def test_session_start_parses_proto():
    cmd = ScriptParser().parse_lines(["session start udp=192.168.1.58 proto=tcp"])[0]
    assert cmd["type"] == "SESSION_START"
    assert cmd["params"] == {"udp": "192.168.1.58", "proto": "tcp"}


def _run_session(monkeypatch, line):
    driver = Mock()
    driver.start.return_value = True
    monkeypatch.setattr(ruida_driver, "RdDriver", lambda: driver)
    out = io.StringIO()
    commands = ScriptParser().parse_lines([line, "session end"])
    ScriptInterpreter(out).interpret(commands)
    return driver, out.getvalue()


def test_script_session_passes_tcp_protocol(monkeypatch):
    driver, _ = _run_session(monkeypatch, "session start udp=192.168.1.58 proto=tcp")
    assert driver.start.call_args.kwargs["protocol"] == "tcp"


def test_script_session_defaults_protocol(monkeypatch):
    driver, _ = _run_session(monkeypatch, "session start udp=192.168.1.10")
    assert driver.start.call_args.kwargs["protocol"] is None


def test_script_session_rejects_unknown_protocol(monkeypatch):
    driver, output = _run_session(monkeypatch, "session start udp=192.168.1.10 proto=sctp")
    driver.start.assert_not_called()
    assert "Unsupported network protocol" in output


def _tui_with_driver():
    adapter = TuiAdapter.__new__(TuiAdapter)
    adapter._last_udp_host = ""
    adapter._last_usb_device = ""
    adapter._last_magic = 0x88
    adapter._last_protocol = "udp"
    adapter._log_error = Mock()
    adapter._log_info = Mock()
    driver = Mock()
    driver._session = object()
    adapter._ruida_driver = driver
    return adapter, driver


def test_tui_session_start_passes_tcp_protocol():
    adapter, driver = _tui_with_driver()

    asyncio.run(adapter._start_session(udp="192.168.1.58", proto="tcp"))

    assert driver.start.call_args.kwargs["protocol"] == "tcp"
    assert adapter._last_protocol == "tcp"


def test_tui_session_start_rejects_unknown_protocol():
    adapter, driver = _tui_with_driver()

    asyncio.run(adapter._start_session(udp="192.168.1.58", proto="sctp"))

    driver.start.assert_not_called()
    adapter._log_error.assert_called_once()
