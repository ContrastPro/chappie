"""Benchmark-only PID lock and heartbeat primitives; never scientific evidence."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_RUNTIME_BENCHMARK/1.0.0"
HEARTBEAT_INTERVAL_SECONDS = 5
PHASES = frozenset(
    {
        "STARTING",
        "PREFLIGHT",
        "LOAD",
        "PREPROCESS",
        "FIT",
        "PREDICT",
        "SERIALIZE",
        "PERSIST",
        "IDLE",
        "COMPLETE",
        "FAILED",
    }
)


def _root(root: Path) -> Path:
    root = root.resolve()
    if "benchmarks" not in root.parts:
        raise PermissionError("benchmark telemetry root isolation failure")
    return root


def _atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    json.loads(temp.read_text(encoding="utf-8"))
    os.replace(temp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class WorkerPaths:
    root: Path

    @property
    def lock(self) -> Path:
        return self.root / "worker" / "lock.json"

    @property
    def pid(self) -> Path:
        return self.root / "worker" / "pid.json"

    @property
    def heartbeat(self) -> Path:
        return self.root / "worker" / "heartbeat.json"


class BenchmarkWorkerTelemetry:
    def __init__(self, root: Path, plan_digest: str, *, pid: int | None = None):
        self.paths = WorkerPaths(_root(root))
        self.plan_digest = plan_digest
        self.pid = os.getpid() if pid is None else pid

    def acquire(self, *, cleanup_stale: bool = False) -> str:
        if self.paths.lock.exists():
            owner = json.loads(self.paths.lock.read_text(encoding="utf-8"))
            alive = _alive(int(owner["pid"]))
            if alive:
                raise RuntimeError("ACTIVE_WRITER")
            if not cleanup_stale:
                return "STALE_LOCK"
            self.paths.lock.unlink()
        _atomic(
            self.paths.lock,
            {
                "identity": IDENTITY,
                "plan_digest": self.plan_digest,
                "pid": self.pid,
                "acquired_at": _now(),
            },
        )
        return "ACQUIRED"

    def start(self, command_identity: str, launcher_pid: int | None = None) -> None:
        _atomic(
            self.paths.pid,
            {
                "identity": IDENTITY,
                "plan_digest": self.plan_digest,
                "pid": self.pid,
                "launcher_pid": launcher_pid,
                "process_start_time": _now(),
                "command_identity": command_identity,
                "heartbeat_path": str(self.paths.heartbeat),
                "stdout_path": str(self.paths.root / "worker" / "stdout.log"),
                "stderr_path": str(self.paths.root / "worker" / "stderr.log"),
            },
        )
        self.heartbeat("STARTING", None, 0, 0, 0)

    def heartbeat(
        self, phase: str, scenario: str | None, verified: int, pending: int, failed: int
    ) -> None:
        if phase not in PHASES:
            raise ValueError("invalid benchmark heartbeat phase")
        _atomic(
            self.paths.heartbeat,
            {
                "identity": IDENTITY,
                "plan_digest": self.plan_digest,
                "pid": self.pid,
                "timestamp": _now(),
                "current_scenario": scenario,
                "current_phase": phase,
                "verified_units": verified,
                "pending_units": pending,
                "failed_units": failed,
                "cpu_seconds": time.process_time(),
            },
        )

    def mark_complete(self, verified: int, pending: int, failed: int = 0) -> None:
        self.heartbeat("COMPLETE", None, verified, pending, failed)

    def mark_failed(
        self, phase: str, exc: Exception, verified: int, pending: int, failed: int
    ) -> None:
        self.heartbeat("FAILED", None, verified, pending, failed)
        data = json.loads(self.paths.heartbeat.read_text())
        data.update(
            {
                "failed_phase": phase,
                "exception_class": type(exc).__name__,
                "exception_message": str(exc),
            }
        )
        _atomic(self.paths.heartbeat, data)

    def release(self) -> None:
        if self.paths.lock.exists() and json.loads(self.paths.lock.read_text())["pid"] == self.pid:
            self.paths.lock.unlink()

    def inspect(self) -> dict[str, object]:
        if not self.paths.heartbeat.exists():
            return {"classification": "UNKNOWN", "pid": self.pid, "pid_alive": _alive(self.pid)}
        data = json.loads(self.paths.heartbeat.read_text())
        phase = data["current_phase"]
        age = time.time() - datetime.fromisoformat(data["timestamp"]).timestamp()
        alive = _alive(int(data["pid"]))
        return {
            "classification": "COMPLETE"
            if phase == "COMPLETE"
            else "FAILED"
            if phase == "FAILED"
            else "ACTIVE"
            if alive and age <= HEARTBEAT_INTERVAL_SECONDS * 2
            else "STALE",
            "pid": data["pid"],
            "pid_alive": alive,
            "heartbeat_age_seconds": age,
            "last_phase": phase,
            "verified": data["verified_units"],
            "pending": data["pending_units"],
            "failed": data["failed_units"],
        }
