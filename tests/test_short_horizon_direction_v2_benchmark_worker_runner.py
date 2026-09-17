from __future__ import annotations

import time

import pytest

from app.tools.short_horizon_direction_v2_benchmark_worker import BenchmarkWorkerTelemetry
from app.tools.short_horizon_direction_v2_benchmark_worker_runner import (
    BENCHMARK_WRITER_IDENTITY,
    run_worker,
)

ID = "CHAPPIE_V2_BENCHMARK_WORKER_CONTROL/1.0.0"


def test_success_and_failure(tmp_path):
    root = tmp_path / "benchmarks" / "x"
    calls = []

    def ok(ctx):
        calls.append(1)
        ctx.update("IDLE")
        time.sleep(0.02)

    run_worker(root, "p", ID, ok)
    t = BenchmarkWorkerTelemetry(root, "p")
    assert (
        calls == [1] and t.inspect()["classification"] == "COMPLETE" and not t.paths.lock.exists()
    )

    def bad(ctx):
        ctx.update("FIT")
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_worker(root, "p", ID, bad)
    assert t.inspect()["classification"] == "FAILED"


def test_duplicate_stale_and_isolation(tmp_path):
    root = tmp_path / "benchmarks" / "x"
    owner = BenchmarkWorkerTelemetry(root, "p")
    owner.acquire()
    with pytest.raises(RuntimeError):
        run_worker(root, "p", ID, lambda _: None)
    owner.release()
    with pytest.raises(PermissionError):
        run_worker(tmp_path / "production", "p", ID, lambda _: None)


def test_exact_benchmark_writer_identity_is_lifecycle_accepted(tmp_path):
    root = tmp_path / "benchmarks" / "writer"
    run_worker(root, "plan", BENCHMARK_WRITER_IDENTITY, lambda _: None)
    assert BenchmarkWorkerTelemetry(root, "plan").inspect()["classification"] == "COMPLETE"
    for identity in ("CHAPPIE_V2_BENCHMARK_WRITER/2.0.0", "production", "Validation"):
        with pytest.raises(PermissionError):
            run_worker(root, "plan", identity, lambda _: None)
