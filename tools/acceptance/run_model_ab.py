"""Run the bounded real Project-QA qwen3:4b versus qwen3:14b comparison."""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
from chappie_copilot_acceptance import BASE_CASES, SESSION_CASES, _context, _is_prompt_echo
from run_qwen3_14b_diagnostic import (
    CHAPPIE_TIMEOUT_SECONDS,
    _free_loopback_port,
    _preflight,
)

from app.ai.tasks import AITask, task_contract
from app.control_plane.service import CONTROL_PLANE_CONTRACT_DIGEST, CONTROL_PLANE_IDENTITY
from app.knowledge.retrieval import (
    RETRIEVAL_CONTRACT_DIGEST,
    RETRIEVAL_IDENTITY,
    KnowledgeRetrievalRequestV1,
    architecture_query_tokens,
    retrieve,
    review_record_ids,
)
from app.knowledge.store import read_store
from app.platform.bootstrap import ChappiePaths, load_local_config

OUTPUT_RELATIVE = Path("data/acceptance/model_ab/focused")
MODEL_IDS = ("QWEN3_4B", "QWEN3_14B")
FOCUSED_IDS = (
    "knowledge_file",
    "control_plane_file",
    "platform_file",
    "ai_file",
    "persistence_file",
    "selection_store",
    "selection_control_plane",
    "selection_platform",
    "selection_persistence",
    "selection_ai",
    "session_04",
    "session_24",
)
CONTROL_IDS = (
    "architecture",
    "ai_bypass_risk",
    "research_orders",
    "setup_scope",
    "dependency_direction",
    "economic_calendar",
    "git_honesty",
)
STABILITY_IDS = ("knowledge_file", "selection_control_plane")


def _cases():
    lookup = {case.case_id: case for case in (*BASE_CASES, *SESSION_CASES)}
    return tuple(lookup[item] for item in (*FOCUSED_IDS, *CONTROL_IDS))


def _write(output: Path, result: dict[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _atomic_write(output / "result.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
    _atomic_write(output / "report.md", _report(result))


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _report(result: dict[str, object]) -> str:
    summary = result.get("summary", {})
    lines = [
        "# Chappie focused model A/B",
        "",
        "Generated local evidence; not canonical Knowledge.",
        "",
        "## Environment",
        f"- Source commit: {result.get('source_commit')}",
        f"- Default model: {result.get('default_model')}",
        "",
        "## Preconditions",
        f"- {result.get('preflight', {}).get('status') if isinstance(result.get('preflight'), dict) else 'PENDING'}",
        "",
        "## Summary",
        f"- {json.dumps(summary, sort_keys=True)}",
        "",
        "## Code review cases",
        "See result.json for bounded response evidence and manual 0–10 rubric fields.",
        "",
        "## Architecture controls",
        "See result.json for deterministic grades and authority checks.",
        "",
        "## Stability",
        f"- {json.dumps(result.get('stability', {}), sort_keys=True)}",
        "",
        "## Timing",
        f"- {json.dumps(result.get('timing_summary', {}), sort_keys=True)}",
        "",
        "## Timeout behavior",
        f"- {json.dumps(result.get('timeout_behavior', {}), sort_keys=True)}",
        "",
        "## Model-state isolation",
        f"- {json.dumps(result.get('state_isolation', {}), sort_keys=True)}",
        "",
        "## Raw evidence summary",
        "- Every comparison records pre-inference retrieval IDs, model-supported citation IDs, context identity, effective model, and bounded answer text in result.json.",
        "",
        "## Next decision",
        "- Manual review must score correctness, relevance, grounding, completeness, and authority discipline; this runner declares no winner.",
    ]
    return "\n".join(lines) + "\n"


def _retrieval_evidence(paths: ChappiePaths, case, context) -> dict[str, object]:
    """Mirror the read-only service retrieval that supplies this one model input."""
    records, knowledge_manifest = read_store(paths.knowledge_file)
    workspace_path = getattr(context, "active_file_relative_path", None)
    record_ids = review_record_ids(records, workspace_path)
    _result, knowledge_context = retrieve(
        records,
        knowledge_manifest,
        KnowledgeRetrievalRequestV1(
            topic_tokens=architecture_query_tokens(case.question, workspace_path=workspace_path),
            record_ids=record_ids,
            maximum_records=4 if record_ids else 8,
        ),
    )
    return {
        "retrieved_record_ids": tuple(record.knowledge_id for record in knowledge_context.records),
        "retrieved_record_digests": tuple(
            record.record_digest for record in knowledge_context.records
        ),
        "expected_retrieval_context_digest": knowledge_context.context_digest,
        "expected_workspace_context_digest": context.context_digest if context else None,
    }


def _call(
    client: httpx.Client,
    case,
    model_id: str | None,
    context,
    *,
    retrieval_evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    retrieval_evidence = retrieval_evidence or {
        "retrieved_record_ids": (),
        "retrieved_record_digests": (),
        "expected_retrieval_context_digest": None,
        "expected_workspace_context_digest": None,
    }
    started = time.monotonic()
    body: dict[str, object] = {"question": case.question}
    if model_id is not None:
        body["requested_model_id"] = model_id
    if context is not None:
        body["workspace_context"] = context.payload()
    try:
        wire = client.post("/v1/project/qa", json=body, timeout=CHAPPIE_TIMEOUT_SECONDS).json()
        data = wire.get("data") if isinstance(wire, dict) else None
        if not isinstance(data, dict):
            return {
                "status": "TRANSPORT_ERROR",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error": wire.get("error") if isinstance(wire, dict) else None,
                **retrieval_evidence,
            }
        answer = str(data.get("answer") or "")
        status = str(data.get("state", "UNKNOWN"))
        grade = "PASS" if status == "OK" and not _is_prompt_echo(answer, case.question) else "FAIL"
        return {
            "case_id": case.case_id,
            "model_id": model_id or "DEFAULT",
            "status": status,
            "deterministic_grade": grade,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "question": case.question,
            "context_kind": "selection"
            if getattr(case, "selection_lines", None)
            else "file"
            if getattr(case, "relative_path", None)
            else "git"
            if getattr(case, "git", False)
            else "none",
            "locator": getattr(case, "relative_path", None),
            "context_digest": context.context_digest if context else None,
            "context_character_count": len(
                context.selected_text or context.active_file_excerpt or ""
            )
            if context
            else 0,
            **retrieval_evidence,
            "retrieved_record_ids": list(retrieval_evidence["retrieved_record_ids"]),
            "retrieved_record_digests": list(retrieval_evidence["retrieved_record_digests"]),
            "retrieval_context_digest": data.get("context_digest"),
            "workspace_context_digest": data.get("workspace_context_digest"),
            "supporting_record_ids": [
                item.get("record_id")
                for item in data.get("supporting_records", [])
                if isinstance(item, dict)
            ],
            "effective_model_id": data.get("effective_model_id"),
            "effective_model": data.get("effective_model"),
            "answer": answer[:4000],
            "error": data.get("error"),
            "manual_rubric": {
                "correctness": None,
                "relevance": None,
                "grounding": None,
                "completeness": None,
                "authority_discipline": None,
            },
        }
    except httpx.TimeoutException:
        return {
            "case_id": case.case_id,
            "model_id": model_id or "DEFAULT",
            "status": "OUTER_TIMEOUT",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": "OUTER_TIMEOUT",
            **retrieval_evidence,
        }
    except (httpx.HTTPError, ValueError) as error:
        return {
            "case_id": case.case_id,
            "model_id": model_id or "DEFAULT",
            "status": "TRANSPORT_ERROR",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": type(error).__name__,
            **retrieval_evidence,
        }


def _compare(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    context_equal = all(
        left.get(key) == right.get(key)
        for key in (
            "question",
            "context_kind",
            "locator",
            "context_digest",
            "context_character_count",
        )
    )
    case_equal = left.get("case_id") == right.get("case_id")
    retrieved_records_equal = left.get("retrieved_record_ids") == right.get("retrieved_record_ids")
    retrieved_digests_equal = left.get("retrieved_record_digests") == right.get(
        "retrieved_record_digests"
    )
    actual_retrieval_context_equal = left.get("retrieval_context_digest") == right.get(
        "retrieval_context_digest"
    )
    actual_retrieval_matches_expected = all(
        item.get("retrieval_context_digest")
        and item.get("retrieval_context_digest") == item.get("expected_retrieval_context_digest")
        for item in (left, right)
    )
    actual_workspace_matches_expected = all(
        item.get("workspace_context_digest") == item.get("expected_workspace_context_digest")
        for item in (left, right)
    )
    supporting_records_equal = left.get("supporting_record_ids") == right.get(
        "supporting_record_ids"
    )
    model_ok = (
        left.get("effective_model_id") == "QWEN3_4B"
        and right.get("effective_model_id") == "QWEN3_14B"
    )
    return {
        "case_id": left.get("case_id"),
        "valid": (
            left.get("status") == "OK"
            and right.get("status") == "OK"
            and case_equal
            and context_equal
            and retrieved_records_equal
            and retrieved_digests_equal
            and actual_retrieval_context_equal
            and actual_retrieval_matches_expected
            and actual_workspace_matches_expected
            and model_ok
        ),
        "case_equal": case_equal,
        "context_equal": context_equal,
        "retrieved_records_equal": retrieved_records_equal,
        "retrieved_digests_equal": retrieved_digests_equal,
        "actual_retrieval_context_equal": actual_retrieval_context_equal,
        "actual_retrieval_matches_expected": actual_retrieval_matches_expected,
        "actual_workspace_matches_expected": actual_workspace_matches_expected,
        "supporting_records_equal": supporting_records_equal,
        "effective_model_gate": model_ok,
    }


def _timing(cases: list[dict[str, object]]) -> dict[str, object]:
    values = [int(item["elapsed_ms"]) for item in cases if isinstance(item.get("elapsed_ms"), int)]
    return {
        "count": len(values),
        "median_ms": statistics.median(values) if values else None,
        "p95_ms": sorted(values)[max(0, int(len(values) * 0.95) - 1)] if values else None,
        "slowest_ms": max(values) if values else None,
        "timeouts": sum(item.get("status") in {"TIMEOUT", "OUTER_TIMEOUT"} for item in cases),
    }


def main() -> int:
    paths = ChappiePaths.resolve()
    output = paths.root / OUTPUT_RELATIVE
    preflight, failures = _preflight(paths)
    config = load_local_config(paths)
    result: dict[str, object] = {
        "evidence_schema": "chappie_model_ab/2.0.0",
        "source_commit": preflight.get("snapshot_commit"),
        "preflight": preflight,
        "default_model": config.model if config else None,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "retrieval_identity": RETRIEVAL_IDENTITY,
        "retrieval_digest": RETRIEVAL_CONTRACT_DIGEST,
        "prompt_identity": task_contract(AITask.PROJECT_QA_V1).prompt_identity,
        "control_plane_identity": CONTROL_PLANE_IDENTITY,
        "control_plane_digest": CONTROL_PLANE_CONTRACT_DIGEST,
        "cases": [],
        "comparisons": [],
    }
    _write(output, result)
    if failures:
        result["summary"] = {"status": "PREFLIGHT_FAILED", "failures": failures}
        _write(output, result)
        return 0
    log = (output / "control_plane.log").open("w", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "serve", "--port", str(_free_loopback_port())],
        cwd=paths.root,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        port = int(child.args[-1])
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            for _ in range(40):
                try:
                    if client.get("/v1/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                result["summary"] = {"status": "CONTROL_PLANE_START_FAILED"}
                _write(output, result)
                return 0
            runs: list[dict[str, object]] = result["cases"]  # type: ignore[assignment]
            comparisons: list[dict[str, object]] = result["comparisons"]  # type: ignore[assignment]
            for case in (
                *_cases(),
                *[
                    next(item for item in _cases() if item.case_id == name)
                    for name in STABILITY_IDS
                    for _ in range(2)
                ],
            ):
                context = _context(paths.root, case)
                retrieval_evidence = _retrieval_evidence(paths, case, context)
                left, right = (
                    _call(
                        client,
                        case,
                        "QWEN3_4B",
                        context,
                        retrieval_evidence=retrieval_evidence,
                    ),
                    _call(
                        client,
                        case,
                        "QWEN3_14B",
                        context,
                        retrieval_evidence=retrieval_evidence,
                    ),
                )
                runs.extend((left, right))
                comparisons.append(_compare(left, right))
                _write(output, result)
            default = _call(
                client,
                next(item for item in _cases() if item.case_id == "architecture"),
                None,
                None,
            )
            result["state_isolation"] = {
                "default_after_ab_effective_model_id": default.get("effective_model_id"),
                "pass": default.get("effective_model_id") == "QWEN3_4B",
                "durable_default_after": load_local_config(paths).model
                if load_local_config(paths)
                else None,
            }
            result["timing_summary"] = {
                model: _timing([item for item in runs if item.get("model_id") == model])
                for model in MODEL_IDS
            }
            result["timeout_behavior"] = {
                model: result["timing_summary"][model]["timeouts"] for model in MODEL_IDS
            }  # type: ignore[index]
            result["stability"] = {
                name: {
                    model: sum(
                        item.get("status") == "OK"
                        for item in runs
                        if item.get("case_id") == name and item.get("model_id") == model
                    )
                    for model in MODEL_IDS
                }
                for name in STABILITY_IDS
            }
            result["summary"] = {
                "unique_cases": len(_cases()),
                "comparisons_valid": sum(item["valid"] for item in comparisons),
                "comparisons_invalid": sum(not item["valid"] for item in comparisons),
                "winner": "NOT_DECLARED",
            }
            _write(output, result)
    finally:
        child.terminate()
        child.wait(timeout=5)
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
