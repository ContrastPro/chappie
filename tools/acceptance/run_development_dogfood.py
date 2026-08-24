"""Run one bounded, advisory Development Reasoning 2.0 dogfood case locally.

This acceptance utility is deliberately outside ``app``. It records only public
Development contracts needed to diagnose a failed real request; it never changes
model policy, Knowledge, Git state, or workspace files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.ai.model_policy import DevelopmentModelRoleV1, resolve_development_model_role
from app.control_plane.service import ChappieControlPlaneV1
from app.development_analysis import (
    CodexTaskPreparationRequestV1,
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisStateV1,
    prepare_codex_handoff,
)
from app.knowledge import KNOWLEDGE_IDENTITY, read_store
from app.knowledge.store import committed_snapshot
from app.platform.bootstrap import ChappiePaths, collect_status, load_local_config
from app.platform.ollama.model_state import LocalOllamaModelStateAdapterV1
from tools.acceptance.development_authorization_pair import (
    AUTHORIZATION_PAIR_FILENAME,
    persist_authorization_pair,
)

EVIDENCE_SCHEMA = "chappie_development_dogfood/1.2.0"
OUTPUT_BASE_RELATIVE = Path("data/acceptance/development_dogfood")
SUPPORTED_MODELS = frozenset({"qwen3-coder:30b", "qwen3:14b"})
MODEL_ROLES = {
    "qwen3-coder:30b": DevelopmentModelRoleV1.CODE,
    "qwen3:14b": DevelopmentModelRoleV1.DEEP,
}
STAGES = (
    "PREFLIGHT",
    "CONTEXT_MAP",
    "EVIDENCE_HANDLES",
    "GROUNDING_SYNTHESIS",
    "GROUNDING_PARSE",
    "PROPOSAL_SYNTHESIS",
    "PROPOSAL_PARSE",
    "REVIEW_SYNTHESIS",
    "REVIEW_PARSE",
    "STRUCTURAL_VALIDATION",
    "READINESS",
    "HANDOFF",
)
CASES = {
    "diff_review": (
        "Review the bounded development design for a read-only diff-review advisory flow in "
        "app.development_analysis. Identify CURRENT evidence, INFERENCES, UNKNOWNs, a safe "
        "PROPOSED scope, ownership, contracts, tests, risks, non-goals, and acceptance criteria. "
        "Do not claim edits, shell, Git, Knowledge, trading, Risk, or execution authority."
    )
}


@dataclass(frozen=True, slots=True)
class Preflight:
    status: str
    failures: tuple[str, ...]
    source_commit: str | None
    metadata: dict[str, object]


def _safe_segment(value: str) -> str:
    value = value.replace(":", "_")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("unsafe local evidence path")
    return value


def output_path(root: Path, model: str, run_id: str) -> Path:
    return root / OUTPUT_BASE_RELATIVE / _safe_segment(model) / _safe_segment(run_id)


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _stage(
    status: str = "NOT_REACHED",
    elapsed_ms: int = 0,
    reason_code: str | None = None,
    **metadata: object,
) -> dict[str, object]:
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "reason_code": reason_code,
        "metadata": metadata,
    }


def _checkpoint(result: dict[str, object], destination: Path) -> None:
    """Make every stage failure inspectable, including a preflight failure."""
    _atomic_json(destination / "result.json", result)


def _preflight(paths: ChappiePaths, model: str) -> Preflight:
    failures: list[str] = []
    metadata: dict[str, object] = {"requested_model": model}
    if model not in SUPPORTED_MODELS:
        return Preflight("FAIL", ("MODEL_UNSUPPORTED",), None, metadata)
    try:
        snapshot = committed_snapshot(paths.root)
        metadata.update(repository_clean=snapshot.clean, source_commit=snapshot.commit)
        if not snapshot.clean:
            failures.append("REPOSITORY_DIRTY")
    except (OSError, ValueError):
        snapshot = None
        failures.append("REPOSITORY_UNAVAILABLE")
    try:
        _records, manifest = read_store(paths.knowledge_file)
        metadata.update(
            knowledge_identity=KNOWLEDGE_IDENTITY,
            knowledge_manifest_digest=manifest.manifest_digest,
        )
        if snapshot is None or manifest.snapshot != snapshot:
            failures.append("KNOWLEDGE_STALE")
    except (OSError, ValueError, TypeError):
        failures.append("KNOWLEDGE_UNAVAILABLE")
    try:
        with httpx.Client(base_url="http://127.0.0.1:11434", timeout=2.0) as client:
            state = LocalOllamaModelStateAdapterV1(client).read()
        metadata.update(
            ollama_reachable=state.runtime_ready, installed_models=list(state.installed_models)
        )
        if not state.runtime_ready:
            failures.append("OLLAMA_NOT_READY")
        elif model not in state.installed_models:
            failures.append("MODEL_NOT_INSTALLED")
    except httpx.HTTPError:
        failures.append("OLLAMA_NOT_READY")
    try:
        config = load_local_config(paths)
    except ValueError:
        config = None
        failures.append("CONFIG_UNAVAILABLE")
    metadata["durable_default_model"] = config.model if config else None
    if config is None or config.model != "qwen3:4b":
        failures.append("DURABLE_DEFAULT_NOT_QWEN3_4B")
    return Preflight(
        "PASS" if not failures else "FAIL",
        tuple(failures),
        getattr(snapshot, "commit", None),
        metadata,
    )


def _lifecycle_stage(analysis: object, name: str) -> tuple[str, int, str | None]:
    expected = {
        "GROUNDING_SYNTHESIS": "GROUNDING_SYNTHESIS",
        "GROUNDING_PARSE": "GROUNDING_PARSE",
        "PROPOSAL_SYNTHESIS": "PROPOSAL_SYNTHESIS",
        "PROPOSAL_PARSE": "PROPOSAL_PARSE",
        "REVIEW_SYNTHESIS": "REVIEW_SYNTHESIS",
        "REVIEW_PARSE": "REVIEW_PARSE",
        "STRUCTURAL_VALIDATION": "STRUCTURAL_VALIDATION",
        "READINESS": "READINESS_EVALUATION",
    }.get(name)
    events = getattr(getattr(analysis, "lifecycle", None), "events", ())
    selected = [
        event
        for event in events
        if getattr(getattr(event, "stage", None), "value", None) == expected
    ]
    if not selected:
        return "NOT_REACHED", 0, None
    event = selected[-1]
    return (
        "PASS" if getattr(getattr(event, "status", None), "value", None) == "COMPLETED" else "FAIL",
        int(getattr(event, "elapsed_ms", 0)),
        getattr(event, "reason_code", None),
    )


def _public_analysis(analysis: object) -> dict[str, object]:
    """Only public DTO content; no prompts, hidden reasoning, or Knowledge bodies."""
    payload = analysis.payload()
    keys = (
        "state",
        "facts",
        "inferences",
        "unknowns",
        "architecture_owners",
        "affected_contracts",
        "likely_files",
        "risks",
        "required_tests",
        "non_goals",
        "acceptance_criteria",
        "confidence",
        "validation_status",
        "readiness",
        "reason_code",
        "current_evidence_handles",
        "proposed_scope",
        "staged_results",
    )
    return {key: payload.get(key) for key in keys}


def _completeness(analysis: object, handoff: object | None) -> dict[str, bool]:
    return {
        "has_concrete_owner": bool(getattr(analysis, "architecture_owners", ())),
        "has_current_evidence": bool(getattr(analysis, "current_evidence_handles", ())),
        "has_existing_contracts": bool(getattr(analysis, "affected_contracts", ())),
        "has_file_scope": bool(getattr(analysis, "likely_files", ())),
        "has_proposed_scope": getattr(analysis, "proposed_scope", None) is not None,
        "has_unknowns": bool(getattr(analysis, "unknowns", ())),
        "has_task_specific_tests": bool(getattr(analysis, "required_tests", ())),
        "has_acceptance_criteria": bool(getattr(analysis, "acceptance_criteria", ())),
        "has_provenance": bool(
            getattr(analysis, "evidence", None) and getattr(analysis, "context_map", None)
        ),
        "has_codex_search_guidance": bool(
            handoff and "Codex consumption guidance" in handoff.projection()
        ),
    }


def run_dogfood(
    *,
    root: Path,
    model: str,
    case: str,
    run_id: str,
    preflight_fn: Callable[[ChappiePaths, str], Preflight] = _preflight,
    analyze_fn: Callable[[ChappiePaths, str, DevelopmentModelRoleV1], object] | None = None,
) -> dict[str, object]:
    """Execute the real read-only pipeline or an injected deterministic test double."""
    if case not in CASES:
        raise ValueError("unsupported dogfood case")
    paths = ChappiePaths.resolve(root)
    destination = output_path(paths.root, model, run_id)
    if (destination / "result.json").exists():
        raise FileExistsError("dogfood evidence output already exists")
    result: dict[str, object] = {
        "schema": EVIDENCE_SCHEMA,
        "status": "RUNNING",
        "case": case,
        "requested_model": model,
        "effective_model": None,
        "stages": {name: _stage() for name in STAGES},
    }
    started = time.monotonic()
    preflight = preflight_fn(paths, model)
    result["preflight"] = {
        "status": preflight.status,
        "failures": list(preflight.failures),
        **preflight.metadata,
    }
    result["source_commit"] = preflight.source_commit
    result["stages"]["PREFLIGHT"] = _stage(
        preflight.status,
        round((time.monotonic() - started) * 1000),
        preflight.failures[0] if preflight.failures else None,
        failures=list(preflight.failures),
    )
    _checkpoint(result, destination)
    if preflight.status != "PASS":
        result["status"] = "PREFLIGHT_FAILED"
        _checkpoint(result, destination)
        _write_report(result, destination / "report.md")
        return result
    role = MODEL_ROLES[model]
    if resolve_development_model_role(role)[1].concrete_model != model:
        result.update(status="FAILED", reason_code="REQUESTED_EFFECTIVE_MODEL_MISMATCH")
        _checkpoint(result, destination)
        _write_report(result, destination / "report.md")
        return result
    if analyze_fn is None:
        plane = ChappieControlPlaneV1(paths, status_collector=collect_status)
        proposal_diagnostics: list[object] = []
        analyze_fn = lambda item, question, requested_role: plane.analyze_development(
            question,
            requested_role=requested_role.value,
            acceptance_diagnostics_sink=proposal_diagnostics.append,
        )
    else:
        proposal_diagnostics = []
    analysis = analyze_fn(paths, CASES[case], role)
    evidence = getattr(analysis, "evidence", None)
    effective_model = getattr(evidence, "effective_model", None)
    result["effective_model"] = effective_model
    if effective_model is not None and effective_model != model:
        result.update(status="FAILED", reason_code="REQUESTED_EFFECTIVE_MODEL_MISMATCH")
        _checkpoint(result, destination)
        _write_report(result, destination / "report.md")
        return result
    context = getattr(analysis, "context_map", None)
    if context is None:
        result.update(status="FAILED", reason_code="CONTEXT_MAP_NOT_AVAILABLE")
        _checkpoint(result, destination)
        _write_report(result, destination / "report.md")
        return result
    result["context"] = {
        "context_digest": context.context_digest,
        "knowledge_identity": context.knowledge_identity,
        "knowledge_manifest_digest": context.knowledge_manifest_digest,
        "knowledge_context_digest": context.knowledge_context_digest,
        "record_ids": list(getattr(evidence, "record_ids", ())),
    }
    result["stages"]["CONTEXT_MAP"] = _stage("PASS", 0, None, context_digest=context.context_digest)
    result["handles"] = [
        {key: value for key, value in item.payload().items() if key != "statement"}
        for item in context.evidence_handles
    ]
    result["stages"]["EVIDENCE_HANDLES"] = _stage(
        "PASS", 0, None, count=len(context.evidence_handles)
    )
    _checkpoint(result, destination)
    for stage in (
        "GROUNDING_SYNTHESIS",
        "GROUNDING_PARSE",
        "PROPOSAL_SYNTHESIS",
        "PROPOSAL_PARSE",
        "REVIEW_SYNTHESIS",
        "REVIEW_PARSE",
        "STRUCTURAL_VALIDATION",
        "READINESS",
    ):
        status, elapsed, reason = _lifecycle_stage(analysis, stage)
        result["stages"][stage] = _stage(status, elapsed, reason)
        _checkpoint(result, destination)
    invariant_failure = _stage_invariant_failure(result["stages"])
    if invariant_failure is not None:
        result.update(status="FAILED", reason_code=invariant_failure)
        _checkpoint(result, destination)
        _write_report(result, destination / "report.md")
        return result
    result["analysis"] = _public_analysis(analysis)
    if proposal_diagnostics:
        result["proposal_parse_diagnostics"] = proposal_diagnostics[-1].payload()
    analysis_state = getattr(getattr(analysis, "state", None), "value", "ANALYSIS_INVALID")
    handoff = None
    if getattr(analysis, "readiness", None) == DevelopmentAnalysisReadinessV1.READY_FOR_TASK:
        handoff = prepare_codex_handoff(CodexTaskPreparationRequestV1(analysis))
        result["handoff"] = handoff.payload()
        result["stages"]["HANDOFF"] = _stage(
            "PASS" if handoff.validation_status == "VALID" else "FAIL", 0, handoff.reason_code
        )
        if handoff.validation_status == "VALID":
            try:
                pair = persist_authorization_pair(
                    destination / AUTHORIZATION_PAIR_FILENAME, analysis, handoff
                )
                result["authorization_pair"] = {
                    "identity": "chappie_development_authorization_pair/1.0.0",
                    "authorization_digest": pair.authorization_digest,
                }
            except (OSError, ValueError, FileExistsError):
                result.update(status="FAILED", reason_code="AUTHORIZATION_PAIR_PERSIST_FAILED")
                _checkpoint(result, destination)
                _write_report(result, destination / "report.md")
                return result
    else:
        result["stages"]["HANDOFF"] = _stage(
            "NOT_REACHED", 0, getattr(analysis, "reason_code", None)
        )
    result["completeness"] = _completeness(analysis, handoff)
    result["provider_metrics"] = {
        "grounding_inference_elapsed_ms": result["stages"]["GROUNDING_SYNTHESIS"]["elapsed_ms"],
        "proposal_inference_elapsed_ms": result["stages"]["PROPOSAL_SYNTHESIS"]["elapsed_ms"],
        "review_inference_elapsed_ms": result["stages"]["REVIEW_SYNTHESIS"]["elapsed_ms"],
        "total_inference_elapsed_ms": sum(
            result["stages"][name]["elapsed_ms"]
            for name in ("GROUNDING_SYNTHESIS", "PROPOSAL_SYNTHESIS", "REVIEW_SYNTHESIS")
        ),
        "load_duration_ms": None,
        "prompt_eval_count": None,
        "eval_count": None,
        "generation_duration_ms": None,
    }
    result["status"] = (
        "COMPLETED" if analysis_state == DevelopmentAnalysisStateV1.OK.value else "FAILED"
    )
    result["reason_code"] = getattr(analysis, "reason_code", None)
    _checkpoint(result, destination)
    _write_report(result, destination / "report.md")
    return result


def _stage_invariant_failure(stages: dict[str, object]) -> str | None:
    """Reject impossible telemetry rather than mislabeling it as model output failure."""
    dependencies = (
        ("GROUNDING_PARSE", "GROUNDING_SYNTHESIS"),
        ("PROPOSAL_SYNTHESIS", "GROUNDING_PARSE"),
        ("PROPOSAL_PARSE", "PROPOSAL_SYNTHESIS"),
        ("REVIEW_SYNTHESIS", "PROPOSAL_PARSE"),
        ("REVIEW_PARSE", "REVIEW_SYNTHESIS"),
        ("STRUCTURAL_VALIDATION", "REVIEW_PARSE"),
        ("READINESS", "STRUCTURAL_VALIDATION"),
    )
    for stage, prerequisite in dependencies:
        value = stages[stage]
        predecessor = stages[prerequisite]
        if value["status"] != "NOT_REACHED" and predecessor["status"] != "PASS":
            return "PIPELINE_STAGE_INVARIANT_VIOLATION"
    return None


def _write_report(result: dict[str, object], path: Path) -> None:
    stages = result["stages"]
    lines = [
        "# Development Reasoning 2.0 dogfood",
        "",
        "NON-CANONICAL LOCAL ACCEPTANCE EVIDENCE",
        "",
        f"- Status: {result['status']}",
        f"- Requested model: {result['requested_model']}",
        f"- Effective model: {result['effective_model']}",
        "",
        "## Stages",
    ]
    lines.extend(
        f"- {name}: {value['status']} ({value['elapsed_ms']} ms){(' - ' + value['reason_code']) if value['reason_code'] else ''}"
        for name, value in stages.items()
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=sorted(SUPPORTED_MODELS))
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    parser.add_argument("--run-id", default=time.strftime("run-%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    try:
        result = run_dogfood(root=Path.cwd(), model=args.model, case=args.case, run_id=args.run_id)
    except (ValueError, FileExistsError) as error:
        print(f"development dogfood runner: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"status": result["status"], "reason_code": result.get("reason_code")}, sort_keys=True
        )
    )
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
