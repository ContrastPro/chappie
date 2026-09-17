"""File-backed harmless control for the benchmark worker lifecycle."""

from __future__ import annotations

import time
from pathlib import Path

from app.tools.short_horizon_direction_v2_benchmark_worker_runner import run_worker

CONTROL_IDENTITY = "CHAPPIE_V2_BENCHMARK_WORKER_CONTROL/1.0.0"
CONTROL_ROOT = Path(
    "E:/projects/chappie/data/research/benchmarks/short_horizon_direction_v2/worker_control"
)
PLAN_DIGEST = "1181f07cd9036af426e028be5bc9670e3c62c1614d10776cb1968a155b5f6bdd"


def callback(context, duration: float = 240.0) -> None:
    started = time.monotonic()
    while time.monotonic() - started < duration:
        context.update("IDLE")
        sum(index * index for index in range(200_000))
        print("operational-control-progress", flush=True)
        time.sleep(5)


def main() -> int:
    run_worker(CONTROL_ROOT, PLAN_DIGEST, CONTROL_IDENTITY, callback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
