"""Operator CLI for the released Capital Flow Free V2 streaming materializer."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from time import monotonic

from app.research.capital_flow.free_v2_formulas import (
    FEATURE_TIMELINE_END_EXCLUSIVE,
    FEATURE_TIMELINE_MINUTES,
    FEATURE_TIMELINE_START,
)
from app.research.capital_flow.free_v2_materializer import (
    CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
    FEATURE_ORDER,
)
from app.research.capital_flow.free_v2_publication import FREE_V2_PUBLICATION_ROOT
from app.research.capital_flow.free_v2_streaming_runner import (
    FREE_V2_STAGING_ROOT,
    MaterializationStatusV1,
    active_run_state,
    calendar_month_chunks,
    inspect_streaming_materialization,
    run_streaming_materialization,
)

PRODUCTION_RUN_ID = "capital-flow-free-v2-production-v1"
SUCCESS_CLASSIFICATION = "CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_VALIDATED"


def _print_status(status: MaterializationStatusV1) -> None:
    print(f"dataset identity: {CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY}")
    print(f"runner/contract compatible: {'YES' if status.contract_compatible else 'NO'}")
    print(f"source authority status: {status.source_authority_status}")
    for item in status.chunks:
        label = item.chunk.start.strftime("%Y-%m")
        suffix = "" if item.error is None else f" ({item.error})"
        print(f"{label} {item.status}{suffix}")
    verified = sum(item.status == "VERIFIED" for item in status.chunks)
    pending = sum(item.status == "PENDING" for item in status.chunks)
    print(f"completed verified chunks: {verified} / {len(status.chunks)}")
    print(f"pending chunks: {pending}")
    print(f"invalid/corrupt chunks: {len(status.invalid_chunks)}")
    for item in status.invalid_chunks:
        print(f"invalid item: {item}")
    if status.invalid_chunks:
        print(
            "safe action: confirm no run is active; inspect the named non-authoritative "
            "item and remove only that invalid item before resuming"
        )
    print(f"dataset status: {status.dataset_status}")
    print(f"processed minutes: {status.processed_minutes} / {status.total_minutes}")
    percentage = status.processed_minutes * 100 / status.total_minutes
    print(f"percentage complete: {percentage:.6f}%")
    print(f"final manifest present: {'YES' if status.final_manifest_present else 'NO'}")
    print(f"active run marker state: {status.active_run_state}")


def _progress_printer():
    chunks = calendar_month_chunks(FEATURE_TIMELINE_START, FEATURE_TIMELINE_END_EXCLUSIVE)
    by_identity = {chunk.identity: (index, chunk) for index, chunk in enumerate(chunks, 1)}
    started = monotonic()

    def report(phase: str) -> None:
        if not phase.startswith(("chunk_completed:", "chunk_reused:")):
            print(phase)
            return
        category, identity = phase.split(":", 1)
        index, chunk = by_identity[identity]
        processed = int((chunk.end_exclusive - FEATURE_TIMELINE_START) / timedelta(minutes=1))
        percentage = processed * 100 / FEATURE_TIMELINE_MINUTES
        action = "REUSED" if category == "chunk_reused" else "CALCULATED"
        print(
            f"chunk {index}/{len(chunks)} {chunk.start:%Y-%m} {action} "
            f"minutes={processed} percentage={percentage:.6f}% elapsed={monotonic() - started:.1f}s"
        )

    return report


def _final_causal_violations() -> int:
    manifest = json.loads((FREE_V2_PUBLICATION_ROOT / "manifest.json").read_text(encoding="utf-8"))
    return sum(int(item["causal_violation_count"]) for item in manifest["quality"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize or inspect the exact Capital Flow Free V2 production dataset."
    )
    parser.add_argument("--status", action="store_true", help="read-only verified progress report")
    arguments = parser.parse_args(argv)
    if arguments.status:
        try:
            status = inspect_streaming_materialization(run_id=PRODUCTION_RUN_ID)
        except Exception as error:  # noqa: BLE001 - operator boundary preserves category
            print(f"CAPITAL_FLOW_FREE_V2_STATUS_FAILED: {type(error).__name__}: {error}")
            return 1
        _print_status(status)
        return 0

    lock_state = active_run_state(FREE_V2_STAGING_ROOT)
    if lock_state == "ACTIVE":
        print("CAPITAL_FLOW_FREE_V2_RUN_ALREADY_ACTIVE")
        return 1
    if lock_state == "STALE_REQUIRES_MANUAL_INTERVENTION":
        print("CAPITAL_FLOW_FREE_V2_STALE_RUN_MARKER")
        print(
            "Confirm no materialization process is running, then remove only "
            f"{FREE_V2_STAGING_ROOT / 'active.lock'} before resuming."
        )
        return 1
    try:
        result = run_streaming_materialization(
            run_id=PRODUCTION_RUN_ID, phase_hook=_progress_printer()
        )
        causal_violations = _final_causal_violations()
        if (
            result.row_count != FEATURE_TIMELINE_MINUTES * len(FEATURE_ORDER)
            or causal_violations != 0
        ):
            raise ValueError("final structural or causal verification failed")
    except Exception as error:  # noqa: BLE001 - operator boundary preserves runner error
        category = (
            "CAPITAL_FLOW_FREE_V2_RUN_ALREADY_ACTIVE"
            if "another materialization runner is active" in str(error)
            else "CAPITAL_FLOW_FREE_V2_MATERIALIZATION_FAILED"
        )
        print(f"{category}: {type(error).__name__}: {error}")
        return 1
    print(SUCCESS_CLASSIFICATION)
    print(f"minute rows: {FEATURE_TIMELINE_MINUTES}")
    print(f"features: {len(FEATURE_ORDER)}")
    print(f"feature states: {FEATURE_TIMELINE_MINUTES * len(FEATURE_ORDER)}")
    print("manifest verification: PASS")
    print("causal violations: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
