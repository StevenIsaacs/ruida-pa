"""Tests for the GlueScript ``scan_rows()`` raster method and the
multi-line transcript serializer."""

import pytest

from rpalib.rpyc_client import RpcRdDriver
from rpalib.rpyc_service import RpycTuiService
from rpascript.tui_adapter import TuiAdapter
from ruidadriver.rd_gluescript import (
    GlueScript,
    JobRunningError,
    _join_continuation_lines,
)


def _raster_job(rows, origin, step, bidirectional=True, horizontal=True,
                mode="IMAGE", overscan="X"):
    """Build a completed GlueScript job containing one scan_rows()."""
    g = GlueScript()
    g.declare_job("Raster Test")
    g.declare_layer("Raster", "#000000", mode=mode, overscan=overscan, speed=200.0)
    g.scan_rows(rows, origin, step, bidirectional, horizontal)
    g.end_job()
    g.stage_gluescript()
    return g


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_scan_rows_requires_declared_layer():
    g = GlueScript()
    with pytest.raises(ValueError, match="declare_layer"):
        g.scan_rows([[50.0]], (0, 0), (0.1, 0.1))


def test_scan_rows_requires_image_depthmap_layer():
    g = GlueScript()
    g.declare_job("Job")
    g.declare_layer("Cut", "#000000", mode="VECTOR")
    with pytest.raises(ValueError, match="IMAGE/DEPTHMAP"):
        g.scan_rows([[50.0]], (0, 0), (0.1, 0.1))


def test_scan_rows_rejects_bad_geometry():
    g = GlueScript()
    g.declare_job("Job")
    g.declare_layer("Raster", "#000000", mode="IMAGE")
    with pytest.raises(ValueError, match="step must be non-zero"):
        g.scan_rows([[50.0]], (0, 0), (0.1, 0.0))
    with pytest.raises(ValueError, match="origin/step"):
        g.scan_rows([[50.0]], (0,), (0.1, 0.1))
    with pytest.raises(ValueError, match="rectangular"):
        g.scan_rows([[50.0], [50.0, 0.0]], (0, 0), (0.1, 0.1))
    with pytest.raises(ValueError, match="rectangular"):
        g.scan_rows([], (0, 0), (0.1, 0.1))
    with pytest.raises(ValueError, match="must be finite"):
        g.scan_rows([[50.0]], (0, 0), (float("inf"), 0.1))


# --------------------------------------------------------------------------- #
# Emission semantics
# --------------------------------------------------------------------------- #

def _actions(g):
    return [line for line in g.rpascript if not line.startswith("#")]


def _cut_lines(g):
    return [line for line in g.rpascript if line.startswith(("CUT_NEAR_", "CUT_FAR_"))]


def test_scan_rows_run_chunking():
    # One all-50 row then one all-0 row: one cut, one move, two power changes.
    g = _raster_job([[50.0, 50.0, 50.0], [0.0, 0.0, 0.0]], (0, 0), (0.1, 0.1))
    imd = [line for line in g.rpascript if line.startswith("IMD_POWER_1 ")]
    assert len(imd) == 2  # 50 then 0
    assert len(_cut_lines(g)) == 1  # the whole 50-run collapses
    assert any("MOVE_NEAR_X" in line for line in g.rpascript)  # the 0-run is a move


def test_scan_rows_change_gating_interleaves_with_power():
    g = GlueScript()
    g.declare_job("Interleave")
    g.declare_layer("R", "#000000", mode="IMAGE")
    g.power(50.0)
    g.scan_rows([[50.0, 50.0], [0.0, 0.0]], (0, 0), (0.1, 0.1), True, True)
    g.end_job()
    g.stage_gluescript()
    imd = [line for line in g.rpascript if line.startswith("IMD_POWER_1 ")]
    assert len(imd) == 2  # the leading 50 is already emitted by power()


def test_scan_rows_bidirectional_single_axis_between_rows():
    g_bidi = _raster_job([[50.0, 0.0], [50.0, 50.0]], (0, 0), (0.1, 0.1),
                         bidirectional=True)
    assert any(line.startswith("MOVE_NEAR_Y ") for line in g_bidi.rpascript)
    g_mono = _raster_job([[50.0, 0.0], [50.0, 50.0]], (0, 0), (0.1, 0.1),
                         bidirectional=False)
    assert not any(line.startswith("MOVE_NEAR_Y ") for line in g_mono.rpascript)
    assert any(line.startswith("MOVE_NEAR_XY ") for line in g_mono.rpascript)


def test_scan_rows_horizontal_false_scans_along_y():
    g = _raster_job([[50.0, 50.0, 50.0], [0.0, 50.0, 50.0]], (0, 0),
                    (0.1, 0.5), horizontal=False, overscan="Y")
    assert any(line.startswith("CUT_NEAR_Y ") for line in _actions(g))
    assert not any(line.startswith("CUT_NEAR_X ") for line in _actions(g))
    # Cross-axis advance uses X.
    assert any(line.startswith("MOVE_NEAR_X ") for line in _actions(g))


def test_scan_rows_bounding_boxes():
    rows = [[50.0, 0.0, 50.0], [0.0, 50.0, 0.0]]
    g = _raster_job(rows, (10.0, 20.0), (0.5, 0.25), True, True)
    min_x, max_x = 10.0, 10.0 + 0.5 * 2
    min_y, max_y = 20.0, 20.0 + 0.25 * 1
    assert g.doc_tr_x == pytest.approx(min_x)
    assert g.doc_bl_x == pytest.approx(max_x)
    assert g.doc_tr_y == pytest.approx(min_y)
    assert g.doc_bl_y == pytest.approx(max_y)


# --------------------------------------------------------------------------- #
# Transcript / re-stage round trips
# --------------------------------------------------------------------------- #

def test_scan_rows_transcript_round_trip():
    g = _raster_job([[50.0, 0.0, 50.0], [0.0, 50.0, 0.0]], (0, 0),
                    (0.1, 0.1), True, True)
    rpascript = list(g.rpascript)
    h = GlueScript()
    h.stage_gluescript(list(g.gluescript))
    assert h.rpascript == rpascript
    assert h.gluescript == g.gluescript


def test_scan_rows_multiline_transcript():
    rows = [[float((c % 3) * 25) for c in range(40)] for _ in range(3)]
    g = _raster_job(rows, (0, 0), (0.1, 0.1), True, True)
    scan_index = next(
        i for i, line in enumerate(g.gluescript) if line.startswith("scan_rows")
    )
    assert scan_index + 1 < len(g.gluescript)  # wraps over multiple lines
    logical = _join_continuation_lines(g.gluescript)
    assert len(logical) == 4  # declare_job, declare_layer, scan_rows, end_job
    name, args, _kwargs = g._parse_gluescript_line(logical[2])
    assert name == "scan_rows"
    assert args[0] == rows
    assert args[1] == (0.0, 0.0)
    assert args[2] == (0.1, 0.1)
    h = GlueScript()
    h.stage_gluescript(list(g.gluescript))
    assert h.gluescript == g.gluescript  # deterministic re-serialization


def test_serializer_short_commands_stay_single_line():
    g = GlueScript()
    g.declare_job("Short")
    g.declare_layer("Cut", "#000000", mode="VECTOR", speed=100.0)
    assert len(g.gluescript) == 2
    assert g.gluescript[0] == "declare_job('Short', 'MACHINE', [0.0, 0.0], 1, 1, 0.0, 0.0)"
    assert len(g.gluescript[1].splitlines()) == 1


def test_serializer_output_is_literal_eval_valid():
    import ast

    rows = [[float(c) for c in range(70)] for _ in range(2)]
    lines = GlueScript()._format_gluescript_call("scan_rows", rows, (0.0, 0.0))
    joined = _join_continuation_lines(lines)
    assert len(joined) == 1
    tree = ast.parse(joined[0], mode="eval")
    assert isinstance(tree.body, ast.Call)
    assert ast.literal_eval(tree.body.args[0]) == rows


def test_scan_rows_guarded_while_job_running():
    class FakeRunning(GlueScript):
        def _job_running(self):
            return True

    # The job-running guard fires before any body validation, so no job setup
    # is needed — scan_rows is a guarded command and must raise immediately.
    with pytest.raises(JobRunningError):
        FakeRunning().scan_rows([[50.0]], (0, 0), (0.1, 0.1))


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


def test_scan_rows_surface_exposed():
    assert hasattr(RpycTuiService, "exposed_scan_rows")
    assert hasattr(TuiAdapter, "gluescript_scan_rows")


def test_rpcdriver_buffers_and_stages_scan_rows():
    server = _FakeServer()
    driver = RpcRdDriver(server)
    driver.declare_job("RPC Raster")
    driver.declare_layer("R", "#000000", mode="IMAGE", overscan="X")
    driver.scan_rows([[50.0, 0.0], [0.0, 50.0]], (0, 0), (0.1, 0.1),
                     True, True)
    driver.end_job()
    assert server.driver.gluescript == driver.gluescript
    assert driver.rpascript == server.driver.rpascript
    assert any(line.startswith("IMD_POWER_1 ") for line in server.driver.rpascript)
    assert any(line.startswith(("CUT_NEAR_", "CUT_FAR_")) for line in server.driver.rpascript)


def test_rpcdriver_sync_handles_wrapped_declare_layer():
    server = _FakeServer()
    driver = RpcRdDriver(server)
    long_label = "Very Long Raster Layer Name " * 8
    driver.declare_job("Sync Test")
    driver.declare_layer(long_label, "#123456", mode="IMAGE", overscan="X_BI")
    assert any(
        line.startswith("declare_layer(") and not line.rstrip().endswith(")")
        for line in driver.gluescript
    )  # wrapped over lines
    driver.sync()
    assert driver._current_layer_mode == "IMAGE"
    assert driver._current_layer_overscan == "X_BI"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
