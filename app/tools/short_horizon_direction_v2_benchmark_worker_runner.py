"""Non-scientific lifecycle runner for benchmark operational controls."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .short_horizon_direction_v2_benchmark_worker import (
    HEARTBEAT_INTERVAL_SECONDS,
    BenchmarkWorkerTelemetry,
)

CONTROL_IDENTITY = "CHAPPIE_V2_BENCHMARK_WORKER_CONTROL/1.0.0"
BENCHMARK_WRITER_IDENTITY = "CHAPPIE_V2_BENCHMARK_WRITER/1.0.0"
BENCHMARK_WRITER_CLASSIFICATION = "NON_AUTHORITATIVE_RUNTIME_BENCHMARK_WRITER"


@dataclass
class WorkerContext:
    telemetry: BenchmarkWorkerTelemetry
    phase: str = "IDLE"
    verified: int = 0
    pending: int = 0
    failed: int = 0

    def update(self, phase: str) -> None:
        self.phase = phase
        self.telemetry.heartbeat(phase, None, self.verified, self.pending, self.failed)


def run_worker(
    root: Path,
    plan_digest: str,
    command_identity: str,
    callback: Callable[[WorkerContext], None],
    *,
    cleanup_stale: bool = False,
) -> None:
    if command_identity not in {CONTROL_IDENTITY, BENCHMARK_WRITER_IDENTITY}:
        raise PermissionError("worker identity isolation failure")
    telemetry = BenchmarkWorkerTelemetry(root, plan_digest)
    acquired = telemetry.acquire(cleanup_stale=cleanup_stale)
    if acquired != "ACQUIRED":
        raise RuntimeError(acquired)
    stop = threading.Event()
    context = WorkerContext(telemetry)

    def beat() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            telemetry.heartbeat(
                context.phase, None, context.verified, context.pending, context.failed
            )

    try:
        telemetry.start(command_identity)
        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        callback(context)
        telemetry.mark_complete(context.verified, context.pending, context.failed)
    except Exception as exc:
        context.failed += 1
        telemetry.mark_failed(context.phase, exc, context.verified, context.pending, context.failed)
        raise
    finally:
        stop.set()
        if "thread" in locals():
            thread.join(timeout=HEARTBEAT_INTERVAL_SECONDS + 1)
        telemetry.release()
