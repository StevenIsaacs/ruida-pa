"""Tests for RdDriver.read_settings().

Replies are injected through RdDriver._on_reply, the same path the
transport uses. Values mirror an RDC8445S (bed 1300 x 900 mm,
500 mm/s max velocity).
"""

import threading

import pytest

from ruidadriver.ruida_driver import RdDriver


def reply(address: int, value: int) -> bytearray:
    data = bytes((value >> shift) & 0x7F for shift in (28, 21, 14, 7, 0))
    return bytearray([0xDA, 0x01, address >> 8, address & 0xFF]) + data


def answering_driver(answers: dict[int, int]) -> tuple[RdDriver, list]:
    """An RdDriver whose run() answers GET_SETTINGs from *answers*."""
    driver = RdDriver()
    sent: list[list[str]] = []

    def fake_run(script, auto_checksum=False):
        sent.append(script)
        replies = []
        for line in script:
            address = RdDriver.setting_address(line.split()[1])
            if address in answers:
                replies.append(reply(address, answers[address]))
        threading.Timer(0.01, driver._on_reply, args=(replies,)).start()

    driver.run = fake_run
    return driver, sent


def test_setting_address_resolves_mnemonics():
    assert RdDriver.setting_address("MEM_BED_SIZE_X") == 0x0026
    assert RdDriver.setting_address("MEM_CARD_ID") == 0x057E


def test_setting_address_rejects_unknown_mnemonic():
    with pytest.raises(KeyError):
        RdDriver.setting_address("MEM_DOES_NOT_EXIST")


def test_read_settings_returns_raw_values():
    driver, sent = answering_driver({0x0026: 1300000, 0x0036: 900000})

    values = driver.read_settings(["MEM_BED_SIZE_X", "MEM_BED_SIZE_Y"])

    assert values == {"MEM_BED_SIZE_X": 1300000, "MEM_BED_SIZE_Y": 900000}
    assert sent == [["GET_SETTING MEM_BED_SIZE_X", "GET_SETTING MEM_BED_SIZE_Y"]]
    assert driver._pending_reads == []


def test_read_settings_omits_settings_that_time_out():
    driver, _ = answering_driver({0x0023: 500000})

    values = driver.read_settings(
        ["MEM_AXIS_MAX_VELOCITY_1", "MEM_AXIS_MAX_VELOCITY_2"], timeout=0.2
    )

    assert values == {"MEM_AXIS_MAX_VELOCITY_1": 500000}
    assert driver._pending_reads == []


def test_status_addresses_still_resolve_reads():
    """CARD_ID is also a status address; a read must still capture it."""
    driver, _ = answering_driver({0x057E: 0x90109010})

    values = driver.read_settings(["MEM_CARD_ID"])

    assert values == {"MEM_CARD_ID": 0x90109010}
