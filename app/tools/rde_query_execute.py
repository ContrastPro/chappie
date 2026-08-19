"""Typed, research-only front end for existing-cache RDE query artifacts.

The tool reads only the canonical compact research store through its narrow
adapter. It never accepts SQL or Python expressions and never touches runtime
databases.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from app.ai.rde_canonical_storage import CanonicalExistingCacheStorageV1
from app.ai.rde_capabilities import CapabilityStatusV1, ExistingEventResearchQueryV1
from app.ai.rde_execution_cache import RDEResearchExecutionService, execution_cache_identity
from app.ai.rde_query_executor import assess_existing_cache_query
from app.research_paths import compact_db_path, rde_execution_cache_dir, rde_execution_results_dir


def load_query_artifact(path: Path) -> ExistingEventResearchQueryV1:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("QUERY_ARTIFACT_INVALID") from error
    if not isinstance(payload, dict):
        raise TypeError("QUERY_ARTIFACT_INVALID")
    try:
        return ExistingEventResearchQueryV1(**payload)
    except (TypeError, ValueError) as error:
        raise ValueError("QUERY_ARTIFACT_INVALID") from error


def execution_plan(query: ExistingEventResearchQueryV1) -> dict[str, object]:
    capability = assess_existing_cache_query(query)
    return {
        "query": asdict(query),
        "query_digest": query.query_digest,
        "capability": capability.value,
        "cache_identity": execution_cache_identity(query),
        "executable": capability == CapabilityStatusV1.EXECUTABLE_EXISTING_CACHE,
    }


def _result_payload(plan: dict[str, object], result) -> dict[str, object]:
    payload = dict(plan)
    payload.update(
        {
            "status": result.status,
            "cache_status": result.cache_status.value,
            "cache_identity": result.cache_identity,
        }
    )
    if result.evidence is not None:
        payload["evidence"] = {
            "matched": result.evidence.matched_count,
            "unmatched": result.evidence.unmatched_count,
            "outcome_unavailable": result.evidence.outcome_unavailable_count,
            "event_mean": result.evidence.event_mean,
            "control_mean": result.evidence.control_mean,
            "incremental_mean": result.evidence.incremental_mean,
            "evidence_status": result.evidence.evidence_status,
        }
    return payload


def _human(plan: dict[str, object]) -> str:
    query = plan["query"]
    assert isinstance(query, dict)
    return "\n".join(
        (
            "CHAPPIE RDE — Existing Cache Research",
            "",
            "Query",
            f"Asset        {query['asset']}",
            f"Event        {query['event_family']}",
            f"Variant      {query['variant']}",
            f"Metric       {query['target_metric']}",
            f"Horizon      {query['target_horizon']}",
            f"Expected     {query['expected_relationship']}",
            "",
            "Capability",
            str(plan["capability"]),
            "",
            "This is descriptive research planning. It does not establish profitability or a trade signal.",
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        query = load_query_artifact(args.query)
        plan = execution_plan(query)
    except (TypeError, ValueError) as error:
        payload = {"status": str(error)}
        print(json.dumps(payload, sort_keys=True) if args.json else payload["status"])
        return 2
    try:
        with CanonicalExistingCacheStorageV1(
            compact_db_path(),
            asset=query.asset,
            family=query.event_family,
            horizon=query.target_horizon,
        ) as storage:
            service = RDEResearchExecutionService(rde_execution_cache_dir())
            result = service.execute(
                query, storage.identity, storage, dry_run=args.dry_run, no_cache=args.no_cache
            )
            payload = _result_payload(plan, result)
            if result.evidence is not None and not args.dry_run:
                artifact_root = args.output_dir or rde_execution_results_dir()
                payload["artifact_dir"] = str(service.write_artifacts(artifact_root, query, result))
    except (OSError, ValueError) as error:
        payload = {"status": str(error) or "CANONICAL_STORAGE_UNAVAILABLE"}
        print(json.dumps(payload, sort_keys=True) if args.json else payload["status"])
        return 2
    print(json.dumps(payload, sort_keys=True) if args.json else _human(payload))
    return 0 if result.status in {"EXECUTED", "EXECUTABLE_EXISTING_CACHE"} else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
