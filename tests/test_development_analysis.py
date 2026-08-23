from dataclasses import replace

import pytest

from app.ai.errors import AIProviderTimeoutError, AIValidationError
from app.ai.model_policy import DevelopmentModelRoleV1
from app.development_analysis.context import DevelopmentContextMapV1, build_context_digest
from app.development_analysis.models import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisRequestV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
    DevelopmentCitationKindV1,
    DevelopmentCitationRejectionV1,
    DevelopmentProposedScopeV1,
    assess_development_analysis_readiness,
    development_readiness,
    validate_development_analysis,
)
from app.development_analysis.service import (
    DevelopmentAnalysisServiceV1,
    _development_retrieval_request,
)
from app.development_analysis.synthesis import (
    DEVELOPMENT_SYNTHESIS_IDENTITY,
    DEVELOPMENT_SYNTHESIS_INSTRUCTION,
    DevelopmentCitationValidationError,
    DevelopmentStageError,
    DevelopmentSynthesisOutputV1,
    development_allowed_fact_ids,
    parse_development_synthesis,
    synthesize_development,
)
from app.knowledge import RepositorySnapshotV1, initial_records, manifest
from app.knowledge.retrieval import retrieve
from app.platform.bootstrap import ChappiePaths


def _paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    return ChappiePaths.resolve(root)


@pytest.mark.parametrize("role", tuple(DevelopmentModelRoleV1))
def test_analysis_builds_evidence_before_stubbed_inference(tmp_path, monkeypatch, role) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    observed = []
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))

    def runner(request, context, evidence):
        observed.append((request, context, evidence))
        owner = next(
            item for item in context.records if "app.development_analysis owns" in item.statement
        )
        contract = next(item for item in context.records if item.title == "DevelopmentAnalysisV1")
        return DevelopmentSynthesisOutputV1(
            facts=(
                f"knowledge.{owner.knowledge_id}.statement",
                f"knowledge.{contract.knowledge_id}.statement",
            ),
            inferences=("DevelopmentAnalysisV1 supports the requested bounded analysis.",),
            unknowns=(
                "The exact future implementation detail cannot be determined from current evidence.",
            ),
            architecture_owners=(owner.knowledge_type.value,),
            affected_contracts=(contract.title,),
            likely_files=("app/development_analysis/models.py",),
            risks=("The requested diff review must preserve the advisory boundary.",),
            required_tests=("Test the requested diff review readiness.",),
            non_goals=("No mutation.",),
            acceptance_criteria=("The requested diff review remains evidence-bound.",),
            confidence=0.5,
        )

    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path), runner=runner, role_available=lambda _role: True
    ).analyze(
        DevelopmentAnalysisRequestV1(
            "Design a bounded post-implementation diff review workflow using DevelopmentAnalysisV1 and CodexTaskV1.",
            role,
        )
    )

    assert result.state == DevelopmentAnalysisStateV1.OK
    assert result.evidence is not None
    assert result.evidence.source_commit == snapshot.commit
    assert result.evidence.knowledge_manifest_digest
    assert result.evidence.evidence_digest
    assert result.evidence.allowed_fact_ids == tuple(sorted(result.evidence.allowed_fact_ids))
    assert len(result.evidence.allowed_fact_ids) == len(set(result.evidence.allowed_fact_ids))
    assert result.evidence.allowed_fact_ids == development_allowed_fact_ids(observed[0][1])
    assert result.payload()["evidence"]["requested_role"] == role.value
    assert observed and observed[0][2] == result.evidence
    assert result.readiness == DevelopmentAnalysisReadinessV1.READY_FOR_TASK
    assert result.reason_code is None
    assert result.architecture_owners == ("MODULE_RESPONSIBILITY",)
    assert result.lifecycle is not None
    assert len(result.lifecycle.request_id) == 32
    assert result.lifecycle.request_id.isascii()
    assert result.lifecycle.operation == "DEVELOPMENT_ANALYSIS"
    assert result.lifecycle.requested_role == role
    assert result.lifecycle.effective_model == result.evidence.effective_model
    assert result.lifecycle.events[-1].stage.value == "COMPLETE"
    assert result.lifecycle.events[-1].status.value == "COMPLETED"
    assert all(0 <= event.elapsed_ms <= 120_000 for event in result.lifecycle.events)
    assert "Design a bounded post-implementation" not in str(result.payload()["lifecycle"])


def test_unavailable_role_fails_closed_without_running_inference(tmp_path) -> None:
    calls = []
    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=lambda *_args: calls.append(True),
        role_available=lambda _role: False,
    ).analyze(DevelopmentAnalysisRequestV1("Analyze a request", DevelopmentModelRoleV1.CODE))
    assert result.state == DevelopmentAnalysisStateV1.MODEL_UNAVAILABLE
    assert result.reason_code == "MODEL_ROLE_UNAVAILABLE"
    assert calls == []


def test_lifecycle_assigns_bounded_distinct_request_ids_without_request_content(tmp_path) -> None:
    request_ids = iter(("a" * 32, "b" * 32))
    service = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=lambda *_args: pytest.fail("inference must not run"),
        role_available=lambda _role: False,
        request_id_factory=lambda: next(request_ids),
    )
    first = service.analyze(
        DevelopmentAnalysisRequestV1("first sensitive request", DevelopmentModelRoleV1.CODE)
    )
    second = service.analyze(
        DevelopmentAnalysisRequestV1("second sensitive request", DevelopmentModelRoleV1.CODE)
    )

    assert first.lifecycle is not None and second.lifecycle is not None
    assert (first.lifecycle.request_id, second.lifecycle.request_id) == ("a" * 32, "b" * 32)
    assert first.lifecycle.events[-1].stage.value == "ROLE_RESOLUTION"
    assert first.lifecycle.events[-1].reason_code == "MODEL_ROLE_UNAVAILABLE"
    assert "sensitive request" not in str(first.payload()["lifecycle"])


def test_provider_timeout_is_a_typed_development_analysis_failure(tmp_path, monkeypatch) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))
    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=lambda *_args: (_ for _ in ()).throw(AIProviderTimeoutError("timed out")),
        role_available=lambda _role: True,
    ).analyze(DevelopmentAnalysisRequestV1("Understand architecture", DevelopmentModelRoleV1.CODE))

    assert result.state == DevelopmentAnalysisStateV1.TIMEOUT
    assert result.reason_code == "AI_PROVIDER_TIMEOUT"
    assert result.lifecycle is not None
    assert result.lifecycle.events[-1].stage.value == "SYNTHESIS"
    assert result.lifecycle.events[-1].status.value == "FAILED"
    assert result.lifecycle.events[-1].reason_code == "AI_PROVIDER_TIMEOUT"
    assert result.payload()["evidence"]["requested_role"] == "CODE"


def test_model_validation_failure_is_a_typed_development_analysis_failure(
    tmp_path, monkeypatch
) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))
    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=lambda *_args: (_ for _ in ()).throw(AIValidationError("invalid model output")),
        role_available=lambda _role: True,
    ).analyze(DevelopmentAnalysisRequestV1("Understand architecture", DevelopmentModelRoleV1.CODE))

    assert result.state == DevelopmentAnalysisStateV1.ANALYSIS_INVALID
    assert result.reason_code == "DEVELOPMENT_SCHEMA_INVALID"
    assert result.lifecycle is not None
    assert result.lifecycle.events[-1].stage.value == "SYNTHESIS"
    assert result.lifecycle.events[-1].reason_code == "DEVELOPMENT_SCHEMA_INVALID"


def _development_context():
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    request = "Design a bounded post-implementation diff review workflow using DevelopmentAnalysisV1 and CodexTaskV1."
    result, _ = retrieve(
        records, manifest(records, snapshot), _development_retrieval_request(records, request, None)
    )
    by_id = {record.knowledge_id: record for record in records}
    return request, tuple(by_id[item] for item in result.selected_record_ids)


def _synthesis_payload(context, **changes):
    owner = next(
        item for item in context.records if "app.development_analysis owns" in item.statement
    )
    contract = next(item for item in context.records if item.title == "DevelopmentAnalysisV1")
    payload = {
        "facts": [
            f"knowledge.{owner.knowledge_id}.statement",
            f"knowledge.{contract.knowledge_id}.statement",
        ],
        "inferences": ["DevelopmentAnalysisV1 supports the requested bounded analysis."],
        "unknowns": [
            "The exact future implementation detail cannot be determined from current evidence."
        ],
        "architecture_owners": [owner.knowledge_type.value],
        "affected_contracts": [contract.title],
        "likely_files": ["app/development_analysis/models.py"],
        "risks": ["The requested diff review must preserve the advisory boundary."],
        "required_tests": ["Test the requested diff review readiness."],
        "non_goals": ["No mutation."],
        "acceptance_criteria": ["The requested diff review remains evidence-bound."],
        "confidence": 0.5,
    }
    payload.update(changes)
    return payload


def test_dedicated_development_contract_accepts_evidence_backed_output() -> None:
    request, records = _development_context()
    context = type("Context", (), {"records": records, "context_digest": "digest"})()
    output = parse_development_synthesis(_synthesis_payload(context), context)
    assert DEVELOPMENT_SYNTHESIS_IDENTITY == "chappie_development_synthesis/2.0.0"
    assert output.architecture_owners == ("MODULE_RESPONSIBILITY",)
    assert "diff review" in output.risks[0]
    assert request.startswith("Design a bounded")


def test_development_synthesis_input_binds_the_exact_explicit_fact_allowlist() -> None:
    _request, records = _development_context()
    context = type("Context", (), {"records": records, "context_digest": "digest"})()
    allowed_fact_ids = development_allowed_fact_ids(context)

    calls = []

    class Provider:
        def generate(self, request):
            calls.append(request.prompt_version)
            assert request.facts == ()
            if request.prompt_version == "chappie_development_grounding/1.0.0":
                payload = {
                    "relevant_evidence_handles": ["E1"],
                    "task_interpretation": "The supplied evidence supports a bounded analysis.",
                    "unknowns": [
                        "Exact future detail cannot be established from current evidence."
                    ],
                }
            elif request.prompt_version == "chappie_development_proposal/1.0.0":
                payload = {
                    "proposed_scope": {
                        "proposed_owner": request.context["development_context_map"][
                            "owner_candidates"
                        ][0],
                        "proposed_modified_files": [],
                        "proposed_new_files": ["app/development_analysis/diff_review.py"],
                        "proposed_contract_changes": ["Add bounded diff-review DTO."],
                        "proposed_components": ["Diff review"],
                        "proposed_behavior": ["Remain advisory and read-only."],
                        "rationale": ["The request requires a bounded proposal."],
                    },
                    "proposal_unknowns": ["Exact endpoint shape remains unknown."],
                }
            else:
                payload = {
                    "risks": ["Analyze must preserve the advisory boundary."],
                    "required_tests": ["Analyze diff review validation."],
                    "non_goals": ["No mutation."],
                    "acceptance_criteria": ["Analyze remains evidence-bound."],
                    "unresolved_unknowns": ["Exact endpoint shape remains unknown."],
                    "confidence": 0.5,
                }
            return type(
                "Result",
                (),
                {
                    "structured_output": payload,
                    "latency_ms": 1,
                },
            )()

    class AI:
        config = type("Config", (), {"profile": "LOCAL", "temperature": 0, "timeout_seconds": 1})()
        router = type("Router", (), {"provider_for": lambda _self, _profile: Provider()})()

    evidence = type(
        "Evidence",
        (),
        {
            "allowed_fact_ids": allowed_fact_ids,
            "source_commit": "a" * 40,
            "knowledge_identity": "chappie_knowledge/1.0.0",
            "knowledge_manifest_digest": "b" * 64,
            "knowledge_context_digest": "digest",
            "evidence_digest": "c" * 64,
            "requested_role": DevelopmentModelRoleV1.CODE,
            "effective_model": "qwen3-coder:30b",
        },
    )()
    assert synthesize_development(
        AI(), type("Request", (), {"request": "Analyze"})(), context, evidence
    )
    assert calls == [
        "chappie_development_grounding/1.0.0",
        "chappie_development_proposal/1.0.0",
        "chappie_development_review/1.0.0",
    ]
    assert "E-handles" in DEVELOPMENT_SYNTHESIS_INSTRUCTION
    assert "model memory is not evidence" in DEVELOPMENT_SYNTHESIS_INSTRUCTION


def test_staged_synthesis_stops_before_proposal_when_grounding_is_invalid() -> None:
    _request, records = _development_context()
    context = type("Context", (), {"records": records, "context_digest": "digest"})()
    calls = []

    class Provider:
        def generate(self, request):
            calls.append(request.prompt_version)
            return type(
                "Result",
                (),
                {
                    "structured_output": {
                        "relevant_evidence_handles": ["E999"],
                        "task_interpretation": "Invalid supplied handle.",
                        "unknowns": ["Unknown."],
                    },
                    "latency_ms": 1,
                },
            )()

    class AI:
        config = type("Config", (), {"profile": "LOCAL", "temperature": 0, "timeout_seconds": 1})()
        router = type("Router", (), {"provider_for": lambda _self, _profile: Provider()})()

    evidence = type(
        "Evidence",
        (),
        {
            "allowed_fact_ids": development_allowed_fact_ids(context),
            "source_commit": "a" * 40,
            "knowledge_identity": "chappie_knowledge/1.0.0",
            "knowledge_manifest_digest": "b" * 64,
            "knowledge_context_digest": "digest",
            "evidence_digest": "c" * 64,
            "requested_role": DevelopmentModelRoleV1.CODE,
            "effective_model": "qwen3-coder:30b",
        },
    )()
    with pytest.raises(DevelopmentStageError, match="staged synthesis failed") as error:
        synthesize_development(
            AI(), type("Request", (), {"request": "Analyze"})(), context, evidence
        )
    assert error.value.code == "GROUNDING_UNSUPPORTED_EVIDENCE"
    assert calls == ["chappie_development_grounding/1.0.0"]
    assert error.value.stage_elapsed_ms == (("GROUNDING_SYNTHESIS", 1),)


def test_stage_parse_failure_keeps_completed_provider_stage_telemetry(
    tmp_path, monkeypatch
) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))

    def runner(*_args):
        raise DevelopmentStageError(
            "PROPOSAL",
            "PROPOSAL_SCHEMA_INVALID",
            stage_elapsed_ms=(("GROUNDING_SYNTHESIS", 11), ("PROPOSAL_SYNTHESIS", 22)),
        )

    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path), runner=runner, role_available=lambda _role: True
    ).analyze(
        DevelopmentAnalysisRequestV1("Analyze DevelopmentAnalysisV1", DevelopmentModelRoleV1.CODE)
    )

    assert result.reason_code == "PROPOSAL_SCHEMA_INVALID"
    assert result.lifecycle is not None
    assert [event.stage.value for event in result.lifecycle.events[-6:]] == [
        "GROUNDING_PARSE",
        "GROUNDING_PARSE",
        "PROPOSAL_SYNTHESIS",
        "PROPOSAL_SYNTHESIS",
        "PROPOSAL_PARSE",
        "PROPOSAL_PARSE",
    ]
    assert result.lifecycle.events[-3].elapsed_ms == 22
    assert result.lifecycle.events[-1].stage.value == "PROPOSAL_PARSE"
    assert result.lifecycle.events[-1].status.value == "FAILED"


@pytest.mark.parametrize(
    ("failed_stage", "expected_calls", "reason_code"),
    (
        ("PROPOSAL", 2, "PROPOSAL_SCHEMA_INVALID"),
        ("REVIEW", 3, "REVIEW_SCHEMA_INVALID"),
    ),
)
def test_staged_parser_failure_requires_its_provider_call(
    failed_stage, expected_calls, reason_code
) -> None:
    _request, records = _development_context()
    context = type("Context", (), {"records": records, "context_digest": "digest"})()
    calls = []

    class Provider:
        def generate(self, request):
            calls.append(request.prompt_version)
            if request.prompt_version == "chappie_development_grounding/1.0.0":
                payload = {
                    "relevant_evidence_handles": ["E1"],
                    "task_interpretation": "Analyze supplied development evidence.",
                    "unknowns": ["Unknown."],
                }
            elif request.prompt_version == "chappie_development_proposal/1.0.0":
                payload = (
                    {}
                    if failed_stage == "PROPOSAL"
                    else {
                        "proposed_scope": {
                            "proposed_owner": request.context["development_context_map"][
                                "owner_candidates"
                            ][0],
                            "proposed_modified_files": [],
                            "proposed_new_files": ["app/development_analysis/diff_review.py"],
                            "proposed_contract_changes": ["Add bounded DTO."],
                            "proposed_components": ["Diff review"],
                            "proposed_behavior": ["Remain advisory."],
                            "rationale": ["Analyze a bounded proposal."],
                        },
                        "proposal_unknowns": ["Unknown."],
                    }
                )
            else:
                payload = {} if failed_stage == "REVIEW" else pytest.fail("unexpected review")
            return type("Result", (), {"structured_output": payload, "latency_ms": len(calls)})()

    class AI:
        config = type("Config", (), {"profile": "LOCAL", "temperature": 0, "timeout_seconds": 1})()
        router = type("Router", (), {"provider_for": lambda _self, _profile: Provider()})()

    evidence = type(
        "Evidence",
        (),
        {
            "allowed_fact_ids": development_allowed_fact_ids(context),
            "source_commit": "a" * 40,
            "knowledge_identity": "chappie_knowledge/1.0.0",
            "knowledge_manifest_digest": "b" * 64,
            "knowledge_context_digest": "digest",
            "evidence_digest": "c" * 64,
            "requested_role": DevelopmentModelRoleV1.CODE,
            "effective_model": "qwen3-coder:30b",
        },
    )()
    with pytest.raises(DevelopmentStageError) as error:
        synthesize_development(
            AI(), type("Request", (), {"request": "Analyze development"})(), context, evidence
        )
    assert error.value.code == reason_code
    assert len(calls) == expected_calls
    assert error.value.stage_elapsed_ms == tuple(
        (f"{stage}_SYNTHESIS", index)
        for index, stage in enumerate(("GROUNDING", "PROPOSAL", "REVIEW")[:expected_calls], start=1)
    )


@pytest.mark.parametrize(
    ("changes", "kind", "rejection"),
    (
        (
            {"facts": ["knowledge.real-but-unsupplied.statement"]},
            DevelopmentCitationKindV1.FACT_ID,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        ),
        (
            {"facts": ["knowledge.invented.statement"]},
            DevelopmentCitationKindV1.FACT_ID,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        ),
        (
            {"architecture_owners": ["INVENTED_OWNER"]},
            DevelopmentCitationKindV1.OWNER,
            DevelopmentCitationRejectionV1.UNSUPPORTED_BY_CITED_FACT,
        ),
        (
            {"affected_contracts": ["InventedContractV1"]},
            DevelopmentCitationKindV1.CONTRACT,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        ),
        (
            {"likely_files": ["app/not-in-evidence.py"]},
            DevelopmentCitationKindV1.FILE,
            DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST,
        ),
    ),
)
def test_citation_rejection_retains_only_bounded_safe_metadata(changes, kind, rejection) -> None:
    _request, records = _development_context()
    context = type("Context", (), {"records": records, "context_digest": "digest"})()
    with pytest.raises(DevelopmentCitationValidationError) as error:
        parse_development_synthesis(_synthesis_payload(context, **changes), context)
    assert error.value.code == "UNSUPPORTED_KNOWLEDGE_CITATION"
    assert error.value.failure.citation_kind == kind
    assert error.value.failure.rejection == rejection
    assert len(error.value.failure.rejected_identifier) <= 160


def test_citation_failure_survives_analysis_completion_without_request_or_model_text(
    tmp_path, monkeypatch
) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))

    def runner(_request, context, _evidence):
        payload = {
            "facts": ["knowledge.invented.statement"],
            "inferences": ["bounded inference"],
            "unknowns": ["Unknown from current evidence."],
            "architecture_owners": [],
            "affected_contracts": [],
            "likely_files": [],
            "risks": ["bounded risk"],
            "required_tests": ["bounded test"],
            "non_goals": ["No mutation."],
            "acceptance_criteria": ["bounded criterion"],
            "confidence": 0.5,
        }
        return parse_development_synthesis(payload, context)

    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path),
        runner=runner,
        role_available=lambda _role: True,
        request_id_factory=lambda: "a" * 32,
    ).analyze(DevelopmentAnalysisRequestV1("sensitive request", DevelopmentModelRoleV1.CODE))

    assert result.reason_code == "UNSUPPORTED_KNOWLEDGE_CITATION"
    assert result.citation_failure is not None
    assert result.citation_failure.rejected_identifier == "knowledge.invented.statement"
    assert result.lifecycle is not None
    assert result.lifecycle.events[-1].stage.value == "SYNTHESIS"
    assert result.payload()["request"] == "sensitive request"
    assert "sensitive request" not in str(result.payload()["lifecycle"])
    assert "sensitive request" not in str(result.payload()["citation_failure"])


def test_empty_allowlist_is_not_a_valid_synthesis_evidence_pack() -> None:
    _request, records = _development_context()
    context = type("Context", (), {"records": records, "context_digest": "digest"})()
    with pytest.raises(AIValidationError, match="allowlist"):
        synthesize_development(
            object(),
            type("Request", (), {"request": "Analyze"})(),
            context,
            type("Evidence", (), {"allowed_fact_ids": ()})(),
        )


def test_evidence_allowlist_rejects_duplicate_or_noncanonical_ids() -> None:
    from app.development_analysis.models import DevelopmentEvidencePackV1

    raw = {
        "source_commit": "a" * 40,
        "repository_clean": True,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "knowledge_manifest_digest": "b" * 64,
        "knowledge_context_digest": "c" * 64,
        "workspace_context_digest": None,
        "requested_role": DevelopmentModelRoleV1.CODE,
        "requested_model": "qwen3-coder:30b",
        "effective_model": "qwen3-coder:30b",
        "profile_identity": "CODE_ADVISORY",
        "capability_identity": "development_model_role/CODE_ADVISORY",
        "record_ids": ("record",),
        "record_digests": ("d" * 64,),
        "evidence_digest": "e" * 64,
    }
    with pytest.raises(ValueError, match="allowlist"):
        DevelopmentEvidencePackV1(
            **raw,
            allowed_fact_ids=("knowledge.record.statement", "knowledge.record.statement"),
        )


def test_real_canonical_but_unsupplied_fact_remains_rejected() -> None:
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    context = type("Context", (), {"records": (records[0],), "context_digest": "digest"})()
    payload = {
        "facts": [f"knowledge.{records[1].knowledge_id}.statement"],
        "inferences": ["bounded inference"],
        "unknowns": ["Unknown from current evidence."],
        "architecture_owners": [],
        "affected_contracts": [],
        "likely_files": [],
        "risks": ["bounded risk"],
        "required_tests": ["bounded test"],
        "non_goals": ["No mutation."],
        "acceptance_criteria": ["bounded criterion"],
        "confidence": 0.5,
    }
    with pytest.raises(DevelopmentCitationValidationError) as error:
        parse_development_synthesis(payload, context)
    assert error.value.failure.rejection == DevelopmentCitationRejectionV1.NOT_IN_ALLOWLIST


@pytest.mark.parametrize(
    ("payload_changes", "code"),
    (
        ({"unexpected": []}, "DEVELOPMENT_SCHEMA_INVALID"),
        ({"likely_files": ["app/not-in-evidence.py"]}, "UNSUPPORTED_KNOWLEDGE_CITATION"),
        ({"affected_contracts": ["InventedContractV1"]}, "UNSUPPORTED_KNOWLEDGE_CITATION"),
    ),
)
def test_dedicated_development_contract_rejects_ungrounded_or_malformed_output(
    payload_changes, code
) -> None:
    _request, records = _development_context()
    context = type("Context", (), {"records": records, "context_digest": "digest"})()
    with pytest.raises(AIValidationError) as error:
        parse_development_synthesis(_synthesis_payload(context, **payload_changes), context)
    assert error.value.code == code


@pytest.mark.parametrize("unsafe", ("C:/secret.py", "../secret.py"))
def test_validator_rejects_unsafe_likely_files(unsafe: str) -> None:
    value = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "request",
        facts=("fact",),
        inferences=("inference",),
        likely_files=(unsafe,),
        required_tests=("test",),
        non_goals=("No mutation.",),
        acceptance_criteria=("criterion",),
    )
    with pytest.raises(ValueError, match="evidence"):
        validate_development_analysis(value)


def test_exact_dogfood_identifiers_select_development_records_deterministically() -> None:
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    store_manifest = manifest(records, snapshot)
    request = "Design a bounded post-implementation diff review workflow using DevelopmentAnalysisV1 and CodexTaskV1."
    first, _ = retrieve(
        records, store_manifest, _development_retrieval_request(records, request, None)
    )
    second, _ = retrieve(
        records, store_manifest, _development_retrieval_request(records, request, None)
    )
    selected = {
        record.title for record in records if record.knowledge_id in first.selected_record_ids
    }
    assert first.selected_record_ids == second.selected_record_ids
    assert {
        "DevelopmentAnalysisV1",
        "CodexTaskV1",
        "Package ownership: Development Intelligence",
        "POST /v1/development/analyze",
        "POST /v1/development/task",
        "Development Intelligence advisory boundary",
        "Development Intelligence analysis tests",
    } <= selected
    assert "Package ownership: Market" not in selected
    assert "Package ownership: Trading" not in selected


@pytest.mark.parametrize(
    "identifier",
    ("DevelopmentAnalysisV1", "CodexTaskV1", "app.development_analysis", "/v1/development/analyze"),
)
def test_development_identifier_has_exact_retrieval_priority(identifier: str) -> None:
    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    result, _ = retrieve(
        records,
        manifest(records, snapshot),
        _development_retrieval_request(records, f"Review {identifier}", None),
    )
    assert result.selected_record_ids
    assert result.selection_reasons[0][1][0].value == "EXACT_ID"


def _ready_analysis(**changes) -> DevelopmentAnalysisV1:
    base = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Design a bounded diff review for DevelopmentAnalysisV1.",
        evidence=object(),  # type: ignore[arg-type]
        facts=(
            "app.development_analysis owns read-only development analysis.",
            "DevelopmentAnalysisV1 is the contract for the requested diff review.",
        ),
        inferences=("The requested diff review must remain advisory.",),
        unknowns=(
            "The exact future diff-review endpoint cannot be determined from current evidence.",
        ),
        architecture_owners=("MODULE_RESPONSIBILITY",),
        affected_contracts=("DevelopmentAnalysisV1",),
        likely_files=("app/development_analysis/models.py",),
        risks=("The requested diff review must preserve its advisory boundary.",),
        required_tests=("Test the requested diff review readiness gate.",),
        non_goals=("No mutation.",),
        acceptance_criteria=("The requested diff review remains evidence-bound.",),
        validation_status="VALID",
        context_map=_test_context_map(),
        current_evidence_handles=("E1",),
        proposed_scope=_test_proposed_scope(),
    )
    return replace(base, **changes)


def _test_context_map() -> DevelopmentContextMapV1:
    value = DevelopmentContextMapV1(
        "a" * 40,
        "chappie_knowledge/1.0.0",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "CODE",
        "qwen3-coder:30b",
        (),
        ("MODULE_RESPONSIBILITY",),
        ("DevelopmentAnalysisV1",),
        ("app/development_analysis/models.py",),
        ("tests/test_development_analysis.py",),
        ("Advisory only.",),
        "",
    )
    return replace(value, context_digest=build_context_digest(value))


def _test_proposed_scope() -> DevelopmentProposedScopeV1:
    return DevelopmentProposedScopeV1(
        "MODULE_RESPONSIBILITY",
        ("app/development_analysis/models.py",),
        ("app/development_analysis/diff_review.py",),
        ("Add bounded review DTO.",),
        ("Diff review",),
        ("Remain advisory.",),
        ("Bounded design proposal.",),
    )


def test_structural_validity_is_distinct_from_task_readiness() -> None:
    assert (
        assess_development_analysis_readiness(_ready_analysis())
        == DevelopmentAnalysisReadinessV1.READY_FOR_TASK
    )
    assert (
        assess_development_analysis_readiness(_ready_analysis(likely_files=()))
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert development_readiness(_ready_analysis(likely_files=())) == (
        DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE,
        "NO_GROUNDED_FILE_SCOPE",
    )
    assert (
        assess_development_analysis_readiness(_ready_analysis(affected_contracts=()))
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert (
        assess_development_analysis_readiness(
            _ready_analysis(required_tests=("Focused deterministic analysis and boundary tests.",))
        )
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert (
        assess_development_analysis_readiness(
            _ready_analysis(
                acceptance_criteria=("Analysis remains bound to the returned evidence digest.",)
            )
        )
        == DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE
    )
    assert (
        assess_development_analysis_readiness(
            _ready_analysis(unknowns=("No unknowns identified based on provided facts.",))
        )
        == DevelopmentAnalysisReadinessV1.NEEDS_REVIEW
    )
    assert development_readiness(
        _ready_analysis(unknowns=("No unknowns identified based on provided facts.",))
    ) == (DevelopmentAnalysisReadinessV1.NEEDS_REVIEW, "INSUFFICIENT_EVIDENCE")


def test_grounded_unknowns_allow_ready_analysis_without_invented_scope() -> None:
    no_contract = _ready_analysis(
        affected_contracts=(),
        unknowns=("The exact contract cannot be determined from current evidence.",),
    )
    no_file = _ready_analysis(
        likely_files=(),
        unknowns=("The exact file cannot be determined from current evidence.",),
    )
    assert (
        assess_development_analysis_readiness(no_contract)
        == DevelopmentAnalysisReadinessV1.READY_FOR_TASK
    )
    assert (
        assess_development_analysis_readiness(no_file)
        == DevelopmentAnalysisReadinessV1.READY_FOR_TASK
    )


def test_forbidden_authority_output_is_a_typed_invalid_result(tmp_path, monkeypatch) -> None:
    from app.development_analysis import service

    snapshot = RepositorySnapshotV1("a" * 40, True)
    records = initial_records(snapshot=snapshot)
    monkeypatch.setattr(service, "committed_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(service, "read_store", lambda _path: (records, manifest(records, snapshot)))

    def runner(_request, context, _evidence):
        owner = next(
            item for item in context.records if "app.development_analysis owns" in item.statement
        )
        contract = next(item for item in context.records if item.title == "DevelopmentAnalysisV1")
        return DevelopmentSynthesisOutputV1(
            facts=(
                f"knowledge.{owner.knowledge_id}.statement",
                f"knowledge.{contract.knowledge_id}.statement",
            ),
            inferences=("Use shell access to inspect the requested diff review.",),
            unknowns=(
                "The exact future implementation detail cannot be determined from current evidence.",
            ),
            architecture_owners=(owner.knowledge_type.value,),
            affected_contracts=(contract.title,),
            likely_files=("app/development_analysis/models.py",),
            risks=("The requested diff review must preserve the advisory boundary.",),
            required_tests=("Test the requested diff review readiness.",),
            non_goals=("No mutation.",),
            acceptance_criteria=("The requested diff review remains evidence-bound.",),
            confidence=0.5,
        )

    result = DevelopmentAnalysisServiceV1(
        paths=_paths(tmp_path), runner=runner, role_available=lambda _role: True
    ).analyze(
        DevelopmentAnalysisRequestV1(
            "Design a bounded post-implementation diff review workflow using DevelopmentAnalysisV1 and CodexTaskV1.",
            DevelopmentModelRoleV1.CODE,
        )
    )

    assert result.state == DevelopmentAnalysisStateV1.ANALYSIS_INVALID
    assert result.validation_status == "INVALID"
    assert result.reason_code == "FORBIDDEN_AUTHORITY_TEXT"
    assert result.facts == ()
