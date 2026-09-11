"""Test RpcRdDriver job-control overrides forward to the server."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from rpalib.rpyc_client import RpcRdDriver


def _make_mock_svc():
    """Create a mock service root with job-control methods."""
    svc = MagicMock()
    svc.pause.return_value = ["PAUSE_JOB"]
    svc.resume.return_value = ["RESUME_JOB"]
    svc.stop_job.return_value = ["STOP_JOB"]
    svc.reset.return_value = ["STOP_JOB", "HOME_XY"]
    return svc


def test_construct_with_shadowed_job_control():
    """RpcRdDriver constructs successfully with job-control overrides."""
    svc = _make_mock_svc()
    driver = RpcRdDriver(svc)
    assert driver._svc is svc
    print("PASS: RpcRdDriver constructs with shadowed job-control methods")


def test_job_control_forwards_to_server():
    """pause/resume/stop_job/reset forward and return the server result."""
    svc = _make_mock_svc()
    driver = RpcRdDriver(svc)
    assert driver.pause() == ["PAUSE_JOB"]
    svc.pause.assert_called_once_with()
    assert driver.resume() == ["RESUME_JOB"]
    svc.resume.assert_called_once_with()
    assert driver.stop_job() == ["STOP_JOB"]
    svc.stop_job.assert_called_once_with()
    assert driver.reset() == ["STOP_JOB", "HOME_XY"]
    svc.reset.assert_called_once_with()
    print("PASS: pause/resume/stop_job/reset forward and return the result")


def test_closed_driver_raises():
    """After close(), job-control calls raise RuntimeError('driver closed')."""
    svc = _make_mock_svc()
    driver = RpcRdDriver(svc)
    driver.close()
    for method in (driver.pause, driver.resume, driver.stop_job, driver.reset):
        try:
            method()
        except RuntimeError as exc:
            assert str(exc) == "driver closed"
        else:
            raise AssertionError("expected RuntimeError('driver closed')")
    print("PASS: closed driver raises RuntimeError('driver closed')")


if __name__ == "__main__":
    print("=== Job-control override tests ===\n")
    test_construct_with_shadowed_job_control()
    test_job_control_forwards_to_server()
    test_closed_driver_raises()
    print("\n=== All job-control override tests complete ===")
