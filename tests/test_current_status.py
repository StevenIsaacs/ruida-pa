"""Tests for ``RdDriver.current_status`` — the full most-recent-replies StatusDict."""

from unittest.mock import Mock

from ruida_pa.rpalib.ruida_transcoder import RdEncoder
from ruida_pa.ruidadriver.ruida_driver import RdDriver


def _reply(msb: int, lsb: int, value: int) -> bytearray:
    """Build a minimal 0xDA status reply carrying ``value`` for a status address."""
    return bytearray([0xDA, 0x01, msb, lsb]) + RdEncoder().encode_uint35(value)


# Handled status fields: (msb, lsb, status key)
_POS = [
    (0x04, 0x21, "POSITION_X"),
    (0x04, 0x31, "POSITION_Y"),
    (0x04, 0x41, "POSITION_Z"),
    (0x04, 0x51, "POSITION_U"),
]
_BED = [(0x00, 0x26, "BED_SIZE_X"), (0x00, 0x36, "BED_SIZE_Y")]
_OTHER = [
    (0x05, 0x7E, "CARD_ID"),
    (0x05, 0x7F, "MAINBOARD_VERSION"),
    (0x03, 0x0F, "MACHINE_FEATURES"),
    (0x04, 0x00, "MACHINE_STATUS"),
]
_BIT_KEYS = {
    "MACHINE_STATUS_MOVING",
    "MACHINE_STATUS_PAUSED",
    "MACHINE_STATUS_JOB_RUNNING",
}


def test_current_status_empty_before_replies():
    driver = RdDriver()
    assert driver.current_status == {}


def test_current_status_tracks_first_reply():
    driver = RdDriver()
    reply = _reply(0x04, 0x21, 1000)
    driver._on_reply([reply])
    st = driver.current_status
    assert "POSITION_X" in st
    value, formatted = st["POSITION_X"]
    assert value == RdDriver.decode_status_value(0x0421, reply)
    assert formatted == RdDriver._format_status_value(0x0421, reply)


def test_current_status_keeps_most_recent_replies():
    driver = RdDriver()
    driver._on_reply([_reply(0x04, 0x21, 100)])
    driver._on_reply([_reply(0x04, 0x31, 200)])
    driver._on_reply([_reply(0x04, 0x21, 900)])
    st = driver.current_status
    assert st["POSITION_X"][0] == RdDriver.decode_status_value(
        0x0421, _reply(0x04, 0x21, 900)
    )
    assert st["POSITION_Y"][0] == RdDriver.decode_status_value(
        0x0431, _reply(0x04, 0x31, 200)
    )


def test_current_status_refreshes_unchanged_value():
    driver = RdDriver()
    driver._on_reply([_reply(0x04, 0x21, 100)])
    before = driver.current_status["POSITION_X"]
    driver._on_reply([_reply(0x04, 0x21, 100)])
    assert driver.current_status["POSITION_X"] == before


def test_current_status_includes_machine_status_bits():
    driver = RdDriver()
    value = 0x01000000 | 0x2  # moving + paused
    driver._on_reply([_reply(0x04, 0x00, value)])
    st = driver.current_status
    assert st["MACHINE_STATUS"][0] == value
    assert st["MACHINE_STATUS_MOVING"] is True
    assert st["MACHINE_STATUS_PAUSED"] is True
    assert st["MACHINE_STATUS_JOB_RUNNING"] is False


def test_current_status_machine_bits_refresh_on_every_status_reply():
    driver = RdDriver()
    driver._on_reply([_reply(0x04, 0x00, 0x1)])  # job running
    assert driver.current_status["MACHINE_STATUS_JOB_RUNNING"] is True
    driver._on_reply([_reply(0x04, 0x00, 0x0)])  # cleared
    assert driver.current_status["MACHINE_STATUS_JOB_RUNNING"] is False
    assert driver.current_status["MACHINE_STATUS_MOVING"] is False


def test_current_status_holds_all_tracked_fields():
    driver = RdDriver()
    driver.run = Mock()  # CARD_ID replies queue the FEATURES_SCRIPT via run()
    for msb, lsb, _ in (*_POS, *_BED, *_OTHER):
        driver._on_reply([_reply(msb, lsb, 100)])
    st = driver.current_status
    expected_keys = {key for _, _, key in (*_POS, *_BED, *_OTHER)}
    assert expected_keys <= set(st)
    assert _BIT_KEYS <= set(st)
    for key in expected_keys:
        assert isinstance(st[key], tuple) and len(st[key]) == 2


def test_current_status_snapshot_is_copy():
    driver = RdDriver()
    driver._on_reply([_reply(0x04, 0x21, 100)])
    original = driver.current_status["POSITION_X"]
    snapshot = driver.current_status
    snapshot["POSITION_X"] = ("x", "x")
    snapshot["BOGUS"] = 1
    assert driver.current_status["POSITION_X"] == original
    assert "BOGUS" not in driver.current_status


# ------------------------------------------------------------------ #
#  TRANSPORT — live-derived transport in current_status
# ------------------------------------------------------------------ #


class _FakeTransport:
    def __init__(self, is_usb: bool = False, is_tcp: bool = False) -> None:
        self.is_usb = is_usb
        self.is_tcp = is_tcp


class _FakeSession:
    def __init__(
        self,
        is_usb: bool = False,
        is_tcp: bool = False,
        is_connected: bool = True,
    ) -> None:
        self.transport = _FakeTransport(is_usb=is_usb, is_tcp=is_tcp)
        self.is_usb = is_usb
        self.is_connected = is_connected


def test_current_status_transport_absent_without_session():
    driver = RdDriver()
    assert "TRANSPORT" not in driver.current_status


def test_current_status_transport_absent_when_not_connected():
    driver = RdDriver()
    driver._session = _FakeSession(is_tcp=True, is_connected=False)
    assert "TRANSPORT" not in driver.current_status


def test_current_status_transport_udp():
    driver = RdDriver()
    driver._session = _FakeSession()
    assert driver.current_status["TRANSPORT"] == ("TRANSPORT_UDP", "UDP")


def test_current_status_transport_tcp():
    driver = RdDriver()
    driver._session = _FakeSession(is_tcp=True)
    assert driver.current_status["TRANSPORT"] == ("TRANSPORT_TCP", "TCP")


def test_current_status_transport_usb_precedes_tcp():
    driver = RdDriver()
    driver._session = _FakeSession(is_usb=True, is_tcp=True)
    assert driver.current_status["TRANSPORT"] == ("TRANSPORT_USB", "USB")


def test_current_status_transport_coexists_with_replies():
    driver = RdDriver()
    driver._session = _FakeSession(is_tcp=True)
    driver._on_reply([_reply(0x04, 0x21, 100)])
    st = driver.current_status
    assert st["TRANSPORT"] == ("TRANSPORT_TCP", "TCP")
    assert "POSITION_X" in st


def test_current_status_transport_is_not_an_event_value():
    driver = RdDriver()
    driver._session = _FakeSession(is_tcp=True)
    driver._on_reply([_reply(0x04, 0x21, 100)])
    assert "TRANSPORT" not in driver._current_status


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
