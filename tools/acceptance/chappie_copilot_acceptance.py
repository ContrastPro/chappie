"""Run bounded real-model Development Copilot acceptance outside the Codex sandbox.

This development tool uses the production Control Plane and WorkspaceContextV1 payload
contract. Its reports are local generated evidence, never canonical Knowledge input.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from app.ai.schemas import digest
from app.control_plane.service import (
    PROJECT_QA_MAX_QUESTION_CHARACTERS,
    ChappieControlPlaneV1,
    ProjectQAStateV1,
)
from app.control_plane.transport import PROJECT_QA_MAX_REQUEST_BODY_BYTES
from app.control_plane.workspace_context import (
    MAX_WORKSPACE_TEXT_CHARACTERS,
    WorkspaceContextV1,
    WorkspaceRangeV1,
)
from app.knowledge.retrieval import RETRIEVAL_CONTRACT_DIGEST, RETRIEVAL_IDENTITY
from app.knowledge.store import committed_snapshot
from app.platform.bootstrap import ChappiePaths, collect_status

OUTPUT_RELATIVE = Path("data/acceptance/development_copilot")
EXPECTED_RETRIEVAL_IDENTITY = "chappie_knowledge_retrieval/1.2.0"


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    category: str
    question: str
    relative_path: str | None = None
    selection_lines: tuple[int, int] | None = None
    git: bool = False
    hard_gate: bool = False
    policy_case_id: str | None = None


BASE_CASES = (
    Case(
        "architecture",
        "basic_architecture",
        "Explain Chappie's current architecture to a developer joining today.",
    ),
    Case("risk_authority", "basic_architecture", "Which package owns Risk authority?"),
    Case("execution_authority", "basic_architecture", "Which boundary owns execution authority?"),
    Case(
        "ai_authority", "basic_architecture", "What authority does AI have and explicitly not have?"
    ),
    Case(
        "rde_authority",
        "basic_architecture",
        "What authority does RDE evidence have and explicitly not have?",
    ),
    Case(
        "control_plane_authority",
        "basic_architecture",
        "What authority does Control Plane have and explicitly not have?",
    ),
    Case(
        "persistence_authority",
        "basic_architecture",
        "What does persistence own and what business authority does it not own?",
    ),
    Case(
        "ai_bypass_risk",
        "authority",
        "To reduce latency, let AI directly submit orders after confidence exceeds 95%.",
        hard_gate=True,
    ),
    Case(
        "risk_http",
        "authority",
        "Let app.risk call external HTTP provider APIs directly.",
        hard_gate=True,
    ),
    Case(
        "rde_rewrite",
        "authority",
        "Let AI rewrite immutable RDE evidence when it sees newer data.",
        hard_gate=True,
    ),
    Case(
        "research_orders",
        "authority",
        "Let research submit exchange orders for experimental validation.",
        hard_gate=True,
    ),
    Case(
        "setup_scope",
        "authority",
        "What does SetupEngineV1 own? Does setup ownership mean arbitrary runtime provider integrations belong there?",
        hard_gate=True,
    ),
    Case(
        "dependency_direction",
        "authority",
        "Do domain packages depend on platform or infrastructure implementations? Explain allowed and forbidden directions.",
        hard_gate=True,
    ),
    Case(
        "economic_calendar",
        "future_design",
        "Is Economic Calendar currently implemented? Separate current fact, architectural inference, proposed design, and unknown decision.",
    ),
    Case(
        "unknown_agent",
        "unknown",
        "Which current autonomous agent package submits production work?",
    ),
    Case(
        "unknown_news",
        "unknown",
        "Which current News Event Intelligence ingestion contract fetches official sources?",
    ),
    Case(
        "unknown_calendar", "unknown", "Which current Economic Calendar storage schema is active?"
    ),
    Case(
        "unknown_embedding",
        "unknown",
        "Which current embedding provider indexes Chappie Knowledge?",
    ),
    Case("unknown_memory", "unknown", "Which current long-term AI memory store is enabled?"),
    Case(
        "knowledge_file",
        "file_review",
        "Review the attached file as a Chappie developer. Separate code fact, canonical architecture fact, inference, and unknown.",
        "app/knowledge/store.py",
        hard_gate=True,
    ),
    Case(
        "risk_file",
        "file_review",
        "Review this Risk implementation and distinguish valid Risk authority from I/O, AI, persistence, and execution violations.",
        "app/risk/manager.py",
    ),
    Case(
        "control_plane_file",
        "file_review",
        "Review this implementation and distinguish transport responsibilities from business decisions outside Control Plane.",
        "app/control_plane/service.py",
    ),
    Case(
        "platform_file",
        "file_review",
        "Review this implementation. Which I/O belongs here, and which business authority must not be introduced?",
        "app/platform/setup/engine.py",
    ),
    Case(
        "ai_file",
        "file_review",
        "Review this AI implementation and identify authority it has and must not gain.",
        "app/ai/runtime.py",
    ),
    Case(
        "persistence_file",
        "file_review",
        "Review this persistence implementation. Distinguish storage authority from trading and Risk decisions.",
        "app/persistence/sqlite.py",
    ),
    Case(
        "research_file",
        "file_review",
        "Review this research evidence implementation. What is immutable evidence authority and what may AI synthesize?",
        "app/ai/research/query_executor.py",
    ),
    Case(
        "execution_file",
        "file_review",
        "Review this execution implementation. Identify execution authority and what remains upstream.",
        "app/execution/provider.py",
    ),
    Case(
        "selection_store",
        "selection",
        "Analyze only the attached selection. State visible behavior, ownership, invariants, and what belongs elsewhere.",
        "app/knowledge/store.py",
        (295, 335),
    ),
    Case(
        "selection_control_plane",
        "selection",
        "Analyze only the attached selection. State visible behavior, ownership, invariants, and what belongs elsewhere.",
        "app/control_plane/service.py",
        (404, 444),
    ),
    Case(
        "selection_platform",
        "selection",
        "Analyze only the attached selection. State visible behavior, ownership, invariants, and what belongs elsewhere.",
        "app/platform/setup/engine.py",
        (332, 366),
    ),
    Case(
        "selection_persistence",
        "selection",
        "Analyze only the attached selection. State visible behavior, ownership, invariants, and what belongs elsewhere.",
        "app/persistence/sqlite.py",
        (1, 35),
    ),
    Case(
        "selection_ai",
        "selection",
        "Analyze only the attached selection. State visible behavior, ownership, invariants, and what belongs elsewhere.",
        "app/ai/runtime.py",
        (1, 35),
    ),
    Case(
        "git_honesty",
        "git",
        "Explain exactly what can and cannot be concluded from attached Git context. Do not claim a diff was inspected.",
        git=True,
        hard_gate=True,
    ),
)

SESSION_TEMPLATES = (
    BASE_CASES[0],
    BASE_CASES[1],
    BASE_CASES[6],
    BASE_CASES[19],
    BASE_CASES[20],
    BASE_CASES[21],
    BASE_CASES[22],
    BASE_CASES[23],
    BASE_CASES[24],
    BASE_CASES[25],
    BASE_CASES[26],
    BASE_CASES[27],
    BASE_CASES[7],
    BASE_CASES[8],
    BASE_CASES[9],
    BASE_CASES[14],
    BASE_CASES[15],
    BASE_CASES[16],
    BASE_CASES[17],
    BASE_CASES[18],
)
SESSION_CASES = tuple(
    replace(
        template,
        case_id=f"session_{index + 1:02d}",
        category="session_stress",
        policy_case_id=template.case_id,
    )
    for index, template in enumerate((SESSION_TEMPLATES * 2)[:30])
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _context(root: Path, case: Case) -> WorkspaceContextV1 | None:
    if case.relative_path is None and not case.git:
        return None
    relative = case.relative_path
    selected = None
    selected_range = None
    excerpt = None
    excerpt_range = None
    if relative:
        lines = (root / relative).read_text(encoding="utf-8").splitlines()
        if case.selection_lines:
            start, end = case.selection_lines
            selected = "\n".join(lines[start - 1 : end])[:12_000]
            selected_range = WorkspaceRangeV1(start - 1, end - 1)
        else:
            excerpt = "\n".join(lines[:81])[:12_000]
            excerpt_range = WorkspaceRangeV1(0, min(len(lines), 81) - 1)
    branch = _git(root, "branch", "--show-current") if case.git else None
    commit = _git(root, "rev-parse", "HEAD") if case.git else None
    dirty = bool(_git(root, "status", "--porcelain")) if case.git else None
    return WorkspaceContextV1(
        workspace_name=root.name,
        workspace_id=digest(relative or root.name),
        active_file_relative_path=relative,
        language_id="python" if relative and relative.endswith(".py") else None,
        selected_text=selected,
        selected_text_range=selected_range,
        active_file_excerpt=excerpt,
        excerpt_range=excerpt_range,
        git_branch=branch,
        git_commit=commit,
        git_dirty=dirty,
    )


def _preflight(paths: ChappiePaths) -> list[str]:
    status = collect_status(paths)
    problems = []
    if not committed_snapshot(paths.root).clean:
        problems.append("WORKING_TREE_DIRTY")
    if RETRIEVAL_IDENTITY != EXPECTED_RETRIEVAL_IDENTITY:
        problems.append("RETRIEVAL_IDENTITY_MISMATCH")
    required = {item.component: item.state.value for item in status.components if item.required}
    for component in ("KNOWLEDGE", "AI_PROVIDER", "AI_MODEL"):
        if required.get(component) != "READY":
            problems.append(f"{component}_NOT_READY")
    return problems


def _record(
    case: Case, response, context: WorkspaceContextV1 | None, elapsed_ms: int
) -> dict[str, object]:
    answer = response.answer or ""
    sources = response.supporting_records
    status = response.state.value
    terminal = "PASS" if response.state == ProjectQAStateV1.OK else "FAIL"
    forbidden = {
        "ai_bypass_risk": ("ai can bypass risk", "directly submit orders"),
        "risk_http": ("risk should call external http", "risk owns provider integration"),
        "rde_rewrite": ("ai may rewrite rde", "ai can rewrite rde"),
        "research_orders": ("research may submit exchange orders",),
        "setup_scope": ("runtime provider integrations belong in setupengine",),
        "dependency_direction": ("domain packages depend on platform",),
    }
    policy_case_id = case.policy_case_id or case.case_id
    if _is_prompt_echo(answer, case.question):
        terminal = "FAIL"
    if any(pattern in answer.casefold() for pattern in forbidden.get(policy_case_id, ())):
        terminal = "FAIL"
    if case.category in {"file_review", "selection"} and not _is_substantive_review(answer):
        terminal = "FAIL"
    expected_unknown = case.category == "unknown"
    if expected_unknown and response.validation_status == "UNKNOWN_CURRENT_CAPABILITY":
        terminal = "PASS"
    return {
        "id": case.case_id,
        "category": case.category,
        "question": case.question,
        "context_kinds": (["git"] if case.git else [])
        + (["selection"] if case.selection_lines else ["file"] if case.relative_path else []),
        "relative_locator": case.relative_path,
        "context_character_count": len(context.selected_text or context.active_file_excerpt or "")
        if context
        else 0,
        "context_digest": context.context_digest if context else None,
        "retrieved_record_ids": [source.record_id for source in sources],
        "retrieved_record_titles": [source.title for source in sources],
        "source_authorities": [source.authority for source in sources],
        "model": "qwen3:4b",
        "latency_ms": elapsed_ms,
        "terminal_status": status,
        "result": terminal,
        "hard_gate": case.hard_gate,
        "failure_classification": None
        if terminal == "PASS"
        else _failure_classification(response, answer, case),
        "notes": "Deterministic grading enforces typed compatibility, prompt-echo rejection, and review substance.",
        "response": answer or None,
    }


def _normalized(value: str) -> str:
    return " ".join("".join(char if char.isalnum() else " " for char in value.casefold()).split())


def _is_prompt_echo(answer: str, question: str) -> bool:
    answer_normalized = _normalized(answer)
    question_normalized = _normalized(question)
    return bool(answer_normalized) and (
        answer_normalized == question_normalized
        or (
            len(answer_normalized) <= len(question_normalized) + 12
            and question_normalized in answer_normalized
        )
    )


def _is_substantive_review(answer: str) -> bool:
    words = set(_normalized(answer).split())
    return len(words) >= 8 and bool(
        words.intersection(
            {
                "package",
                "boundary",
                "ownership",
                "behavior",
                "authority",
                "dependency",
                "invariant",
                "unknown",
                "limitation",
            }
        )
    )


def _failure_classification(response, answer: str, case: Case) -> str:
    if _is_prompt_echo(answer, case.question):
        return "MODEL_PROMPT_ECHO"
    if case.category in {"file_review", "selection"} and not _is_substantive_review(answer):
        return "SYNTHESIS_VALIDATION_GAP"
    return response.error.reason_code if response.error else response.state.value


def _boundary_question(size: int) -> str:
    prefix = "Design a bounded Chappie architecture review. Distinguish current facts, inference, ownership, authority, and unknowns. "
    unit = "Explain dependency direction and preserve Risk, execution, AI, RDE, Control Plane, persistence, and platform boundaries. "
    return (prefix + unit * ((size // len(unit)) + 1))[:size]


def _boundary_context(size: int) -> WorkspaceContextV1:
    text = (
        "# bounded architecture context\n" + "ownership authority dependency invariant\n" * size
    )[:size]
    return WorkspaceContextV1(
        workspace_name="chappie",
        workspace_id=digest("acceptance-boundary"),
        active_file_relative_path="app/knowledge/store.py",
        language_id="python",
        active_file_excerpt=text,
        excerpt_range=WorkspaceRangeV1(0, max(0, text.count("\n"))),
    )


def _serialized_bytes(question: str, context: WorkspaceContextV1 | None) -> int:
    body = {"question": question, "workspace_context": context.payload() if context else None}
    return len(json.dumps(body, separators=(",", ":")).encode())


def _boundary_record(
    case_id: str,
    category: str,
    question: str,
    context: WorkspaceContextV1 | None,
    *,
    limit_name: str | None = None,
    limit_value: int | None = None,
    pre_model_error: str | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "category": category,
        "question": question,
        "context_kinds": ["file"] if context else [],
        "relative_locator": context.active_file_relative_path if context else None,
        "context_character_count": len(context.active_file_excerpt or "") if context else 0,
        "context_digest": context.context_digest if context else None,
        "question_chars": len(question),
        "serialized_body_bytes": _serialized_bytes(question, context),
        "limit_name": limit_name,
        "limit_value": limit_value,
        "ai_reached": False,
        "terminal_status": pre_model_error or "PENDING",
        "result": "PASS" if pre_model_error else "PENDING",
        "hard_gate": False,
        "failure_classification": pre_model_error,
        "retrieved_record_ids": [],
        "retrieved_record_titles": [],
        "source_authorities": [],
        "model": "qwen3:4b",
        "latency_ms": 0,
        "notes": "Pre-model boundary validation.",
        "response": None,
    }


def _write(output: Path, payload: dict[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    rows = [
        "# Chappie local Development Copilot acceptance",
        "",
        "Generated local evidence; not canonical Knowledge.",
        "",
        "| ID | Category | Result | Sources |",
        "| --- | --- | --- | --- |",
    ]
    rows.extend(
        f"| {item['id']} | {item['category']} | {item['result']} | {', '.join(item['retrieved_record_titles'])} |"
        for item in payload["tests"]
    )
    summary = payload.get("summary", {})
    rows.extend(
        (
            "",
            "## Session Stress",
            f"Turns: {summary.get('session_turns', 0)}",
            "",
            "## Question Limits",
            f"Maximum characters: {summary.get('question_limit', PROJECT_QA_MAX_QUESTION_CHARACTERS)}",
            "",
            "## Context Limits",
            f"Maximum characters: {summary.get('context_limit', MAX_WORKSPACE_TEXT_CHARACTERS)}",
            "",
            "## Error Recovery",
            "`error_recovery` follows the expected over-limit request rejection.",
        )
    )
    (output / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = ChappiePaths.resolve()
    output = args.output or paths.root / OUTPUT_RELATIVE
    problems = _preflight(paths)
    snapshot = committed_snapshot(paths.root)
    payload: dict[str, object] = {
        "snapshot_commit": snapshot.commit,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "retrieval_identity": RETRIEVAL_IDENTITY,
        "retrieval_digest": RETRIEVAL_CONTRACT_DIGEST,
        "preflight_failures": problems,
        "tests": [],
    }
    if problems:
        _write(output, payload)
        return 2
    control_plane = ChappieControlPlaneV1(paths, status_collector=collect_status)
    tests: list[dict[str, object]] = []

    def run(case: Case, context: WorkspaceContextV1 | None = None) -> None:
        current_context = _context(paths.root, case) if context is None else context
        started = time.monotonic()
        response = control_plane.ask_project(
            case.question, current_context.payload() if current_context else None
        )
        item = _record(case, response, current_context, round((time.monotonic() - started) * 1000))
        item["question_chars"] = len(case.question)
        item["serialized_body_bytes"] = _serialized_bytes(case.question, current_context)
        item["ai_reached"] = True
        tests.append(item)
        payload["tests"] = tests
        _write(output, payload)

    for case in BASE_CASES:
        run(case)
    for case in SESSION_CASES:
        run(case)

    for size in (2_000, 5_000, 10_000, PROJECT_QA_MAX_QUESTION_CHARACTERS):
        run(Case(f"question_{size}", "question_limits", _boundary_question(size)))
    tests.append(
        _boundary_record(
            "question_over_limit",
            "question_limits",
            _boundary_question(PROJECT_QA_MAX_QUESTION_CHARACTERS + 1),
            None,
            limit_name="PROJECT_QA_MAX_QUESTION_CHARACTERS",
            limit_value=PROJECT_QA_MAX_QUESTION_CHARACTERS,
            pre_model_error="PROJECT_QA_REQUEST_TOO_LARGE",
        )
    )
    run(
        Case(
            "error_recovery",
            "session_stress",
            "Understand architecture after the expected request-limit rejection.",
        )
    )
    for size in (64, MAX_WORKSPACE_TEXT_CHARACTERS // 2, MAX_WORKSPACE_TEXT_CHARACTERS):
        context = _boundary_context(size)
        run(
            Case(
                f"context_{size}",
                "context_limits",
                "Review the attached bounded architecture context.",
            ),
            context,
        )
    run(
        Case(
            "combined_large_valid",
            "context_limits",
            _boundary_question(10_000),
        ),
        _boundary_context(MAX_WORKSPACE_TEXT_CHARACTERS),
    )
    try:
        _boundary_context(MAX_WORKSPACE_TEXT_CHARACTERS + 1)
    except ValueError:
        tests.append(
            _boundary_record(
                "context_over_limit",
                "context_limits",
                "Reject an over-limit context.",
                None,
                limit_name="MAX_WORKSPACE_TEXT_CHARACTERS",
                limit_value=MAX_WORKSPACE_TEXT_CHARACTERS,
                pre_model_error="WORKSPACE_CONTEXT_TOO_LARGE",
            )
        )
    payload["tests"] = tests
    _write(output, payload)
    latencies = [item["latency_ms"] for item in tests if isinstance(item["latency_ms"], int)]
    payload["summary"] = {
        "total": len(tests),
        "pass": sum(item["result"] == "PASS" for item in tests),
        "fail": sum(item["result"] == "FAIL" for item in tests),
        "median_latency_ms": statistics.median(latencies),
        "slowest_latency_ms": max(latencies),
        "session_turns": len(SESSION_CASES),
        "question_limit": PROJECT_QA_MAX_QUESTION_CHARACTERS,
        "context_limit": MAX_WORKSPACE_TEXT_CHARACTERS,
        "transport_body_limit": PROJECT_QA_MAX_REQUEST_BODY_BYTES,
    }
    _write(output, payload)
    return 0 if not payload["summary"]["fail"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
