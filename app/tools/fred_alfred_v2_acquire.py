"""Bounded operator CLI for FRED/ALFRED V2 raw-evidence acquisition."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

import httpx

from app.research.capital_flow.fred_alfred_v2 import (
    FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY,
    FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2,
    FRED_SERIES_V2,
    SOURCE_RANGE_V2,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.acquire import (
    ACQUISITION_COMPLETE,
    FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE,
    AcquisitionResultV2,
    AllAcquisitionFailedV2,
    AllAcquisitionResultV2,
    acquire_all_fred_alfred_v2_raw_evidence,
    acquire_series_raw_evidence,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.raw_evidence import (
    EVIDENCE_COMPLETE,
    FRED_ALFRED_V2_RAW_EVIDENCE_ROOT,
    RawEvidenceCacheError,
    RawEvidencePartV2,
    inspect_raw_evidence_set,
    load_raw_evidence,
    raw_evidence_relative_path,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.request_plan import (
    build_fred_alfred_v2_request_plan,
    build_full_vintage_observation_parts,
)
from app.research.capital_flow.fred_alfred_v2_acquisition.transport import (
    FredAlfredTransportError,
)

STATUS_ABSENT = "ABSENT"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_COMPLETE = "COMPLETE"
STATUS_CORRUPT = "CORRUPT"


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="inspect validated local cache only")
    parser.add_argument("--series", choices=FRED_SERIES_V2, help="acquire one frozen V2 series")
    return parser


def _cache_path(root: Path, series_id: str, request_identity: str) -> Path:
    root_resolved = root.resolve()
    path = (root / raw_evidence_relative_path(series_id, request_identity)).resolve()
    if not path.is_relative_to(root_resolved):
        raise ValueError("raw evidence cache path escapes configured root")
    return path


def _local_series_status(root: Path, series_id: str) -> tuple[str, int, int]:
    plan = build_fred_alfred_v2_request_plan(series_id)
    evidence: list[RawEvidencePartV2] = []
    expected = list(plan.initial_parts)
    present = 0
    try:
        for part in plan.initial_parts:
            path = _cache_path(root, series_id, part.identity)
            if path.exists():
                present += 1
                evidence.append(load_raw_evidence(path, part))
        if plan.full_vintage_template is not None and evidence:
            enumeration = evidence[0]
            if not all(isinstance(value, str) for value in enumeration.provider_records):
                return STATUS_CORRUPT, present, len(expected)
            derived = build_full_vintage_observation_parts(
                series_id, tuple(enumeration.provider_records)
            )
            expected.extend(derived)
            for part in derived:
                path = _cache_path(root, series_id, part.identity)
                if path.exists():
                    present += 1
                    evidence.append(load_raw_evidence(path, part))
        evidence_set = inspect_raw_evidence_set(plan, evidence)
    except (RawEvidenceCacheError, ValueError):
        return STATUS_CORRUPT, present, len(expected)
    if not evidence:
        return STATUS_ABSENT, 0, len(expected)
    if evidence_set.status == EVIDENCE_COMPLETE:
        return STATUS_COMPLETE, present, len(evidence_set.required_request_part_identities)
    return STATUS_INCOMPLETE, present, len(evidence_set.required_request_part_identities)


def _print_status(
    root: Path,
    series: str | None,
    environment: Mapping[str, str],
    output: TextIO,
) -> int:
    credential = "PRESENT" if environment.get(FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2) else "ABSENT"
    selected = (series,) if series else FRED_SERIES_V2
    print(f"authority={FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY}", file=output)
    print(f"series={','.join(selected)}", file=output)
    print(f"authorized_source_range={SOURCE_RANGE_V2[0]}..{SOURCE_RANGE_V2[1]}", file=output)
    print(f"raw_evidence_root={root.as_posix()}", file=output)
    print(f"{FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2}={credential}", file=output)
    statuses: list[str] = []
    for series_id in selected:
        status, present, required = _local_series_status(root, series_id)
        statuses.append(status)
        print(
            f"series_status={series_id} status={status} verified_parts={present}/{required}",
            file=output,
        )
    if all(status == STATUS_COMPLETE for status in statuses):
        overall = STATUS_COMPLETE
    elif any(status == STATUS_CORRUPT for status in statuses):
        overall = STATUS_CORRUPT
    elif all(status == STATUS_ABSENT for status in statuses):
        overall = STATUS_ABSENT
    else:
        overall = STATUS_INCOMPLETE
    print(f"overall_raw_evidence={overall}", file=output)
    return 0


def _print_series_progress(result: AcquisitionResultV2, output: TextIO) -> None:
    kind = "full_vintage" if result.series_id in ("DTWEXBGS", "STLFSI4") else "release_vintage"
    print(
        f"progress series={result.series_id} kind={kind} "
        f"cache_reused={result.cache_reused_count} acquired={result.downloaded_count} "
        f"verified={len(result.raw_evidence_part_digests)}/{result.required_part_count}",
        file=output,
    )
    if kind == "full_vintage":
        print(
            "progress stages=vintage_enumeration,derived_observation_parts,verification",
            file=output,
        )


def _failure_label(error: BaseException) -> str:
    if isinstance(error, FredAlfredTransportError | RawEvidenceCacheError):
        return error.category
    if isinstance(error, AllAcquisitionFailedV2):
        return error.result.failure_category or "ACQUISITION_FAILED"
    if isinstance(error, ValueError):
        return "REQUEST_PLAN_VALIDATION_FAILURE"
    if isinstance(error, RuntimeError) and str(error) == "FRED_API_KEY_REQUIRED":
        return "FRED_API_KEY_REQUIRED"
    return "OPERATOR_FAILURE"


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = FRED_ALFRED_V2_RAW_EVIDENCE_ROOT,
    environment: Mapping[str, str] | None = None,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
    client_factory: Callable[[], httpx.Client] = httpx.Client,
    acquire_series: Callable[..., AcquisitionResultV2] = acquire_series_raw_evidence,
    acquire_all: Callable[..., AllAcquisitionResultV2] = (acquire_all_fred_alfred_v2_raw_evidence),
) -> int:
    """Run local status or the released bounded acquisition orchestrator."""
    stdout = output or sys.stdout
    stderr = error_output or sys.stderr
    env = environment if environment is not None else os.environ
    try:
        arguments = _parser().parse_args(argv)
    except _UsageError as error:
        print(f"USAGE_ERROR: {error}", file=stderr)
        return 2
    if arguments.status:
        return _print_status(root, arguments.series, env, stdout)
    if not env.get(FRED_CREDENTIAL_ENVIRONMENT_VARIABLE_V2):
        print("FRED_API_KEY_REQUIRED", file=stderr)
        return 2

    print("FRED/ALFRED V2 raw acquisition", file=stdout)
    print(f"authorized_source_range={SOURCE_RANGE_V2[0]}..{SOURCE_RANGE_V2[1]}", file=stdout)
    try:
        with client_factory() as client:
            if arguments.series:
                print(f"series 1/1 {arguments.series}", file=stdout)
                result = acquire_series(root, arguments.series, client)
                _print_series_progress(result, stdout)
                complete = result.status == FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE
            else:
                result_all = acquire_all(root, client)
                for index, result in enumerate(result_all.series_results, start=1):
                    print(f"series {index}/7 {result.series_id}", file=stdout)
                    _print_series_progress(result, stdout)
                complete = result_all.status == ACQUISITION_COMPLETE
    except (
        AllAcquisitionFailedV2,
        FredAlfredTransportError,
        RawEvidenceCacheError,
        ValueError,
        RuntimeError,
    ) as error:
        print(_failure_label(error), file=stderr)
        return 1
    if not complete:
        print(STATUS_INCOMPLETE, file=stderr)
        return 1
    print(FRED_ALFRED_V2_RAW_EVIDENCE_COMPLETE, file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
