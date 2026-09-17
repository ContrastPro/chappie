from __future__ import annotations

import pytest

from app.tools.short_horizon_direction_v2_benchmark_worker import BenchmarkWorkerTelemetry


def test_lock_heartbeat_terminal_and_inspection(tmp_path):
    root = tmp_path / "benchmarks" / "v2"
    worker = BenchmarkWorkerTelemetry(root, "plan", pid=999999)
    assert worker.acquire() == "ACQUIRED"
    worker.start("control")
    worker.heartbeat("FIT", "x", 1, 6, 0)
    assert worker.inspect()["last_phase"] == "FIT"
    worker.mark_complete(7, 0)
    assert worker.inspect()["classification"] == "COMPLETE"
    worker.release()
    assert not worker.paths.lock.exists()


def test_stale_live_and_failed(tmp_path, monkeypatch):
    root = tmp_path / "benchmarks" / "v2"
    first = BenchmarkWorkerTelemetry(root, "p", pid=999999)
    first.acquire()
    assert BenchmarkWorkerTelemetry(root, "p", pid=999998).acquire() == "STALE_LOCK"
    assert BenchmarkWorkerTelemetry(root, "p", pid=999998).acquire(cleanup_stale=True) == "ACQUIRED"
    worker = BenchmarkWorkerTelemetry(root, "p", pid=999998)
    worker.start("x")
    worker.mark_failed("FIT", ValueError("x"), 0, 6, 1)
    assert worker.inspect()["classification"] == "FAILED"


def test_invalid_phase_and_isolation(tmp_path):
    with pytest.raises(PermissionError):
        BenchmarkWorkerTelemetry(tmp_path / "production", "p")
    worker = BenchmarkWorkerTelemetry(tmp_path / "benchmarks" / "x", "p")
    with pytest.raises(ValueError):
        worker.heartbeat("BAD", None, 0, 0, 0)
