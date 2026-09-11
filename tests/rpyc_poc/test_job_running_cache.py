"""Test RpcRdDriver job-running cache updated from status events.

The cache is exercised with a mock service root — no real controller and
no real RPyC server. ``_on_status_event_for_cache`` updates the cache
from dict status events and resets it on DISCONNECTED/TERMINATED;
``_job_running`` lazily registers the internal listener and returns the
cached value.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from rpalib.rpyc_client import RpcRdDriver
from ruidadriver.rd_gluescript import JobRunningError


def _make_mock_svc():
    """Create a mock service root that accepts status listeners."""
    svc = MagicMock()
    svc.pause.return_value = ["PAUSE_JOB"]
    return svc


def test_status_event_sets_flag_and_guards_commands():
    """A True job-running status event guards commands but not job control."""
    svc = _make_mock_svc()
    driver = RpcRdDriver(svc)
    driver._on_status_event_for_cache({"MACHINE_STATUS_JOB_RUNNING": True})
    assert driver._job_running_cache is True
    assert driver._job_running() is True
    try:
        driver.declare_job("test")
    except JobRunningError:
        pass
    else:
        raise AssertionError(
            "declare_job should raise JobRunningError while a job runs"
        )
    # Job-control commands remain allowed while a job runs.
    assert driver.pause() == ["PAUSE_JOB"]
    print("PASS: True status event sets the cache and guards commands")


def test_false_event_clears_flag():
    """A False job-running status event clears the cache and unguards."""
    svc = _make_mock_svc()
    driver = RpcRdDriver(svc)
    driver._on_status_event_for_cache({"MACHINE_STATUS_JOB_RUNNING": True})
    assert driver._job_running_cache is True
    driver._on_status_event_for_cache({"MACHINE_STATUS_JOB_RUNNING": False})
    assert driver._job_running_cache is False
    assert driver._job_running() is False
    driver.declare_job("test")  # must not raise
    print("PASS: False status event clears the cache")


def test_disconnected_resets_cache_and_registration():
    """A DISCONNECTED event resets the cache and forces lazy re-registration."""
    svc = _make_mock_svc()
    driver = RpcRdDriver(svc)
    driver._on_status_event_for_cache({"MACHINE_STATUS_JOB_RUNNING": True})
    assert driver._job_running_cache is True
    driver._on_status_event_for_cache("DISCONNECTED")
    assert driver._job_running_cache is False
    assert driver._job_running_cache_registered is False
    # The next _job_running() call re-registers the listener lazily.
    assert driver._job_running() is False
    assert svc.register_status_listener.call_count == 1
    print("PASS: DISCONNECTED resets the cache and registration flag")


def test_lazy_registration_swallows_runtime_error():
    """A not-ready server driver leaves the cache unregistered and returns False."""
    svc = _make_mock_svc()
    svc.register_status_listener.side_effect = RuntimeError(
        "server driver not ready"
    )
    driver = RpcRdDriver(svc)
    assert driver._job_running() is False
    assert driver._job_running_cache_registered is False
    print("PASS: lazy registration swallows RuntimeError when the server is not ready")


def test_lazy_registration_registers_once():
    """A ready server driver registers the listener exactly once."""
    svc = _make_mock_svc()
    driver = RpcRdDriver(svc)
    assert driver._job_running() is False
    assert driver._job_running() is False
    assert svc.register_status_listener.call_count == 1
    assert driver._job_running_cache_registered is True
    print("PASS: lazy registration registers the listener exactly once")


if __name__ == "__main__":
    print("=== Job-running cache tests ===\n")
    test_status_event_sets_flag_and_guards_commands()
    test_false_event_clears_flag()
    test_disconnected_resets_cache_and_registration()
    test_lazy_registration_swallows_runtime_error()
    test_lazy_registration_registers_once()
    print("\n=== All job-running cache tests complete ===")