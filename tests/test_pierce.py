"""Tests for the GlueScript delay-family methods: ``pierce()``,
``add_delay()``, ``laser_on_delay()`` and ``laser_off_delay()``."""

import pytest

from ruida_pa.rpalib.rpyc_client import RpcRdDriver
from ruida_pa.rpalib.rpyc_service import RpycTuiService
from ruida_pa.rpascript.tui_adapter import TuiAdapter
from ruida_pa.ruidadriver.rd_gluescript import GlueScript, JobRunningError

DELAY_METHODS = ("pierce", "add_delay", "laser_on_delay", "laser_off_delay")


def _actions(g):
    return [line for line in g.rpascript if not line.startswith("#")]


def _layer_actions(g):
    """rpascript lines in the layer block (after SELECT_LAYER); no comments."""
    in_layer = False
    out = []
    for line in g.rpascript:
        if line.startswith("SELECT_LAYER"):
            in_layer = True
            continue
        if in_layer and not line.startswith("#"):
            out.append(line)
    return out


def _vector_job(*calls):
    g = GlueScript()
    g.declare_job("Pierce Test")
    g.declare_layer("Cut", "#000000", mode="VECTOR", speed=100.0)
    for name, args in calls:
        getattr(g, name)(*args)
    g.end_job()
    g.stage_gluescript()
    return g


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", DELAY_METHODS)
@pytest.mark.parametrize("value", [-1.0, float("inf"), float("-inf"),
                                   float("nan")])
def test_delay_methods_reject_bad_duration(method, value):
    g = GlueScript()
    with pytest.raises(ValueError, match="duration must be >= 0"):
        getattr(g, method)(value)


@pytest.mark.parametrize("method", DELAY_METHODS)
def test_delay_methods_accept_zero(method):
    g = GlueScript()
    g.declare_job("Zero")
    g.declare_layer("Cut", "#000000", mode="VECTOR", speed=100.0)
    getattr(g, method)(0.0)
    g.end_job()
    g.stage_gluescript()
    tail = [a for a in _actions(g) if "PIERCE" in a or "DELAY" in a]
    assert tail  # all four emit a real action for 0.000mS


# --------------------------------------------------------------------------- #
# Emission semantics
# --------------------------------------------------------------------------- #


def test_pierce_emission_in_call_order():
    """A FAR position move, pierce(), then a NEAR move-on mirrors the RDWorks
    dot-mode capture pattern."""
    g = _vector_job(
        ("move_xy_to", (365.0, 225.0)),
        ("pierce", (100.0,)),
        ("move_x_to", (360.0,)),
    )
    actions = _actions(g)
    assert "MOVE_FAR_XY X=365.000mm Y=225.000mm" in actions
    assert "CUT_PIERCE 100.000mS" in actions
    assert "MOVE_NEAR_X nearX=-5.000mm" in actions
    assert actions.index("CUT_PIERCE 100.000mS") > actions.index(
        "MOVE_FAR_XY X=365.000mm Y=225.000mm"
    )
    assert actions.index("MOVE_NEAR_X nearX=-5.000mm") > actions.index(
        "CUT_PIERCE 100.000mS"
    )


def test_delay_family_emission():
    g = _vector_job(
        ("pierce", (100.0,)),
        ("add_delay", (5.0,)),
        ("laser_on_delay", (2.0,)),
        ("laser_off_delay", (3.0,)),
    )
    actions = _actions(g)
    expected = [
        "CUT_PIERCE 100.000mS",
        "ADD_CUT_DELAY 5.000mS",
        "LASER_ON_DELAY 2.000mS",
        "LASER_OFF_DELAY 3.000mS",
    ]
    indices = [actions.index(exp) for exp in expected]
    assert indices == sorted(indices)  # call order preserved
    assert all(line in actions for line in expected)


def test_pierce_does_not_move_the_head():
    g = _vector_job(
        ("move_xy_to", (10.0, 10.0)),
        ("pierce", (100.0,)),
        ("move_x_to", (5.0,)),
    )
    # The move after pierce() is relative to the pre-pierce position.
    actions = _actions(g)
    assert "MOVE_NEAR_X nearX=-5.000mm" in actions


def test_delay_family_are_plain_actions_not_flushed_settings():
    """Pending cut_speed settings are NOT flushed by a delay action — they
    stay pending until the next cut_* action."""
    g = GlueScript()
    g.declare_job("Flush")
    g.declare_layer("Cut", "#000000", mode="VECTOR", speed=100.0)
    g.cut_speed(120.0)
    g.pierce(100.0)
    g.end_job()
    g.stage_gluescript()
    actions = _layer_actions(g)
    # No CUT_SPEED line in the layer block: the pending speed has no
    # following cut action, so it drops (the delay did NOT flush it).
    assert not any(line.startswith("CUT_SPEED_LASER_1") for line in actions)
    assert "CUT_PIERCE 100.000mS" in actions


def test_delay_family_interleaves_with_scan_rows():
    g = GlueScript()
    g.declare_job("Interleave")
    g.declare_layer("R", "#000000", mode="IMAGE", overscan="X")
    g.scan_rows([[50.0, 50.0], [0.0, 0.0]], (0, 0), (0.1, 0.1), True, True)
    g.pierce(100.0)
    g.scan_rows([[0.0, 0.0]], (0, 0), (0.1, 0.1), True, True)
    g.end_job()
    g.stage_gluescript()
    actions = _actions(g)
    assert "CUT_PIERCE 100.000mS" in actions
    assert actions.index("CUT_PIERCE 100.000mS") > actions.index(
        "IMD_POWER_1 Power:0.0%"
    )
    assert actions.index("CUT_PIERCE 100.000mS") < actions.index(
        "MOVE_NEAR_XY nearX=0.000mm nearY=-0.100mm"
    )


# --------------------------------------------------------------------------- #
# Transcript / re-stage round trips
# --------------------------------------------------------------------------- #


def test_delay_family_transcript_round_trip():
    g = _vector_job(
        ("move_xy_to", (365.0, 225.0)),
        ("pierce", (100.0,)),
        ("move_x_to", (360.0,)),
        ("add_delay", (5.0,)),
        ("laser_on_delay", (2.0,)),
        ("laser_off_delay", (3.0,)),
    )
    rpascript = list(g.rpascript)
    h = GlueScript()
    h.stage_gluescript(list(g.gluescript))
    assert h.rpascript == rpascript
    assert h.gluescript == g.gluescript


# --------------------------------------------------------------------------- #
# Job-running guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", DELAY_METHODS)
def test_delay_family_guarded_while_job_running(method):
    class FakeRunning(GlueScript):
        def _job_running(self):
            return True

    with pytest.raises(JobRunningError):
        getattr(FakeRunning(), method)(100.0)


# --------------------------------------------------------------------------- #
# RPC surface
# --------------------------------------------------------------------------- #


class _FakeServer:
    """Minimal in-process mirror of the RPC server's GlueScript surface."""

    def __init__(self):
        self.driver = GlueScript()

    def new_gluescript(self):
        self.driver.new_gluescript()

    def register_status_listener(self, _listener):
        """No-op: the in-process server has no live session."""

    def stage_gluescript_delta(self, flushed_count, delta_lines,
                               require_complete=True):
        return self.driver.stage_gluescript_delta(
            flushed_count, list(delta_lines), require_complete
        )

    def stage_gluescript(self, gluescript=None, require_complete=True):
        return self.driver.stage_gluescript(
            None if gluescript is None else list(gluescript), require_complete
        )

    def get_gluescript(self):
        return list(self.driver.gluescript)

    def get_rpascript(self):
        return list(self.driver.rpascript)

    def job_complete(self):
        return self.driver.job_complete


@pytest.mark.parametrize("method", DELAY_METHODS)
def test_delay_family_surface_exposed(method):
    assert hasattr(RpycTuiService, f"exposed_{method}")
    assert hasattr(TuiAdapter, f"gluescript_{method}")


def test_rpcdriver_buffers_and_stages_pierce():
    server = _FakeServer()
    driver = RpcRdDriver(server)
    driver.declare_job("RPC Pierce")
    driver.declare_layer("Cut", "#000000", mode="VECTOR", speed=100.0)
    driver.move_xy_to(365.0, 225.0)
    driver.pierce(100.0)
    driver.move_x_to(360.0)
    driver.add_delay(5.0)
    driver.laser_on_delay(2.0)
    driver.laser_off_delay(3.0)
    driver.end_job()
    assert server.driver.gluescript == driver.gluescript
    assert driver.rpascript == server.driver.rpascript
    server_actions = [line for line in server.driver.rpascript
                      if not line.startswith("#")]
    assert "CUT_PIERCE 100.000mS" in server_actions
    assert "ADD_CUT_DELAY 5.000mS" in server_actions
    assert "LASER_ON_DELAY 2.000mS" in server_actions
    assert "LASER_OFF_DELAY 3.000mS" in server_actions


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
