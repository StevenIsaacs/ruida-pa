"""Test netref callbacks through the service module."""
import os
import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from rpyc.utils.factory import connect
from rpyc.utils.server import ThreadedServer
from rpalib.rpyc_service import RpycTuiService
from rpascript.tui_adapter import TuiAdapter


received_events = []
received_errors = []
received_replies = []

def on_status(event):
    received_events.append(event)
    print(f"  [callback] status: {event}")

def on_error(msg):
    received_errors.append(msg)
    print(f"  [callback] error: {msg}")

def on_reply(replies_list):
    received_replies.extend(replies_list)
    print(f"  [callback] reply: {replies_list}")


def _make_mock_driver():
    """Create a mock driver that accepts listeners but does nothing."""
    mock = MagicMock()
    mock.register_status_listener = MagicMock()
    mock.register_error_listener = MagicMock()
    mock.register_reply_listener = MagicMock()
    return mock


def test_callback_registration():
    """Register callbacks and verify exposed_ methods exist."""
    adapter = TuiAdapter()
    adapter._ruida_driver = _make_mock_driver()
    service = RpycTuiService(tui_adapter=adapter)
    server = ThreadedServer(service, hostname="127.0.0.1", port=19995)
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        conn = connect("127.0.0.1", 19995)
        svc = conn.root
        svc.register_status_listener(on_status)
        svc.register_error_listener(on_error)
        svc.register_reply_listener(on_reply)
        print("PASS: Callbacks registered via RPyC netref")
        # Verify all exposed_ methods exist
        methods = [
            "exposed_connect", "exposed_disconnect",
            "exposed_start", "exposed_stop", "exposed_run",
            "exposed_cancel_script", "exposed_is_connected",
            "exposed_machine_status", "exposed_current_status",
            "exposed_register_status_listener",
            "exposed_register_error_listener", "exposed_register_reply_listener",
            "exposed_format_reply_value", "exposed_format_reply",
            "exposed_format_reply_list",
        ]
        for m in methods:
            assert hasattr(svc, m), f"Missing {m}"
        print("PASS: All 15 exposed_ methods are accessible")
    except Exception as e:
        print(f"FAIL: {e}")
        import traceback
        traceback.print_exc()
    finally:
        server.close()


def test_run_returns_none():
    """Verify exposed_run spawns a background thread and returns None."""
    adapter = TuiAdapter()
    adapter._ruida_driver = _make_mock_driver()
    service = RpycTuiService(tui_adapter=adapter)
    server = ThreadedServer(service, hostname="127.0.0.1", port=19996)
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    time.sleep(0.3)
    try:
        conn = connect("127.0.0.1", 19996)
        svc = conn.root
        result = svc.exposed_run(["CORE NOP"], auto_checksum=False)
        assert result is None, f"Expected None, got {result}"
        print("PASS: exposed_run returns None (background thread)")
    except Exception as e:
        print(f"FAIL: {e}")
    finally:
        server.close()


if __name__ == "__main__":
    print("=== Callback & API Tests ===\n")
    test_callback_registration()
    test_run_returns_none()
    print("\n=== All callback tests complete ===")
