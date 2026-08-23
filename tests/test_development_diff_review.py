from __future__ import annotations

from dataclasses import replace

from app.development_analysis.models import (
    DevelopmentDiffAcceptanceEvidenceV1,
    DevelopmentDiffChangeEvidenceV1,
    DevelopmentDiffContractEvidenceV1,
    DevelopmentDiffReviewCheckV1,
    DevelopmentDiffReviewRequestV1,
    DevelopmentDiffReviewStateV1,
    DevelopmentDiffReviewStatusV1,
    DevelopmentDiffTestEvidenceV1,
)
from app.development_analysis.task import (
    CodexTaskPreparationRequestV1,
    prepare_codex_task,
    review_development_diff,
)
from tests.test_codex_task import _analysis


def _request(**changes: object) -> DevelopmentDiffReviewRequestV1:
    analysis = changes.pop("analysis", replace(_analysis(), unknowns=()))
    handoff = changes.pop("handoff", None)
    if handoff is None:
        handoff = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    path = "app/development_analysis/task.py"
    defaults: dict[str, object] = {
        "analysis": analysis,
        "handoff": handoff,
        "target_identity": "target",
        "changed_files": (path,),
        "changes": (DevelopmentDiffChangeEvidenceV1(path, "MODIFIED", "a" * 64, "b" * 64, (), 1),),
        "tests": (
            DevelopmentDiffTestEvidenceV1(
                "tests/test_codex_task.py", DevelopmentDiffReviewCheckV1.PASS, "c" * 64
            ),
        ),
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "knowledge_manifest_digest": "b" * 64,
        "knowledge_context_digest": "c" * 64,
        "contract_changes": ("Add bounded DTO.",),
        "contract_evidence": (
            DevelopmentDiffContractEvidenceV1(
                "Add bounded DTO.", DevelopmentDiffReviewCheckV1.PASS, "d" * 64
            ),
        ),
        "acceptance_evidence": (
            DevelopmentDiffAcceptanceEvidenceV1(
                "Task remains advisory.", DevelopmentDiffReviewCheckV1.PASS, "e" * 64
            ),
        ),
    }
    defaults.update(changes)
    return DevelopmentDiffReviewRequestV1(**defaults)


def test_approved_bounded_diff_passes_and_preserves_human_approval() -> None:
    review = review_development_diff(_request())

    assert review.review_status is DevelopmentDiffReviewStatusV1.MATCHES_APPROVED_SCOPE
    assert review.state is DevelopmentDiffReviewStateV1.COMPLETE
    assert review.validation_status == "VALID"
    assert review.target_identity == "target"
    assert review.source_analysis_identity == "chappie_development_analysis/2.0.0"
    assert review.source_commit == "a" * 40
    assert review.source_analysis_evidence_digest == review.source_handoff_evidence_digest
    assert review.source_context_digest
    assert review.source_handoff_identity == "chappie_codex_handoff/1.0.0"
    assert review.source_handoff_task_id
    assert review.knowledge_context_digest == "c" * 64
    assert review.knowledge_identity == "chappie_knowledge/1.0.0"
    assert review.knowledge_manifest_digest == "b" * 64
    assert review.contract_check.status is DevelopmentDiffReviewCheckV1.PASS
    assert review.contract_check.reason_code == "CONTRACT_SCOPE_MATCH"
    assert review.tests_check.status is DevelopmentDiffReviewCheckV1.PASS
    assert review.tests_check.reason_code == "TESTS_PASS"
    assert review.acceptance_check.status is DevelopmentDiffReviewCheckV1.PASS
    assert review.acceptance_check.reason_code == "ACCEPTANCE_SATISFIED"
    assert review.authority_check.status is DevelopmentDiffReviewCheckV1.PASS
    assert review.authority_check.reason_code == "AUTHORITY_SAFE"
    assert review.human_approval_status == "HUMAN_APPROVAL_REQUIRED"


def test_target_identity_is_bound_and_invalid_target_fails_closed() -> None:
    first = review_development_diff(_request(target_identity="target-a"))
    second = review_development_diff(_request(target_identity="target-b"))
    invalid = review_development_diff(_request(target_identity=""))

    assert first.target_identity == "target-a"
    assert second.target_identity == "target-b"
    assert first != second
    assert invalid.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert invalid.review_status is DevelopmentDiffReviewStatusV1.REJECTED


def test_oversized_target_unsafe_path_and_invalid_digest_fail_closed() -> None:
    oversized_target = review_development_diff(_request(target_identity="x" * 161))
    unsafe_path = review_development_diff(_request(changed_files=("../unsafe.py",), changes=()))
    invalid_digest = review_development_diff(_request(knowledge_context_digest="g" * 64))

    assert oversized_target.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert unsafe_path.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert invalid_digest.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR


def test_oversized_and_malformed_evidence_fail_closed_without_exceptions() -> None:
    path = "app/development_analysis/task.py"
    oversized_files = review_development_diff(_request(changed_files=(path,) * 17))
    oversized_symbols = review_development_diff(
        _request(
            changes=(
                DevelopmentDiffChangeEvidenceV1(
                    path, "MODIFIED", "a" * 64, "b" * 64, tuple(f"symbol{n}" for n in range(17)), 1
                ),
            )
        )
    )
    malformed_test_evidence = review_development_diff(_request(tests=(object(),)))
    malformed_contract_evidence = review_development_diff(_request(contract_evidence=(object(),)))
    malformed_acceptance_evidence = review_development_diff(
        _request(acceptance_evidence=(object(),))
    )

    assert oversized_files.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert oversized_symbols.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert malformed_test_evidence.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert malformed_contract_evidence.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert malformed_acceptance_evidence.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR


def test_malformed_handoff_returns_bounded_invalid_review() -> None:
    valid_handoff = prepare_codex_task(
        CodexTaskPreparationRequestV1(replace(_analysis(), unknowns=()))
    )
    malformed_object = review_development_diff(_request(handoff=object()))
    wrong_type_scope = review_development_diff(
        _request(handoff=replace(valid_handoff, proposed_new_files="wrong"))
    )

    assert malformed_object.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert malformed_object.reason_code == "SOURCE_BINDING_MISMATCH"
    assert wrong_type_scope.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert wrong_type_scope.reason_code == "SOURCE_BINDING_MISMATCH"
    assert malformed_object == review_development_diff(_request(handoff=object()))


def test_malformed_nested_analysis_and_optional_owner_fail_closed() -> None:
    analysis = replace(_analysis(), unknowns=())
    handoff = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    malformed_reviews = (
        review_development_diff(
            _request(analysis=replace(analysis, evidence=object()), handoff=handoff)
        ),
        review_development_diff(
            _request(analysis=replace(analysis, context_map=object()), handoff=handoff)
        ),
        review_development_diff(
            _request(analysis=replace(analysis, proposed_scope=object()), handoff=handoff)
        ),
        review_development_diff(
            _request(
                analysis=replace(
                    analysis, context_map=replace(analysis.context_map, context_digest=object())
                ),
                handoff=handoff,
            )
        ),
        review_development_diff(
            _request(
                analysis=replace(
                    analysis,
                    proposed_scope=replace(analysis.proposed_scope, proposed_owner=object()),
                ),
                handoff=handoff,
            )
        ),
        review_development_diff(_request(handoff=replace(handoff, proposed_owner=object()))),
        review_development_diff(_request(handoff=replace(handoff, proposed_owner="x" * 161))),
        review_development_diff(
            _request(analysis=replace(analysis, unknowns=(object(),)), handoff=handoff)
        ),
        review_development_diff(
            _request(analysis=replace(analysis, risks=(object(),)), handoff=handoff)
        ),
    )

    assert all(
        item.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR for item in malformed_reviews
    )
    assert all(
        item.review_status is DevelopmentDiffReviewStatusV1.REJECTED for item in malformed_reviews
    )


def test_malformed_analysis_scope_collections_fail_closed() -> None:
    analysis = replace(_analysis(), unknowns=())
    handoff = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    malformed_scope = replace(analysis.proposed_scope, proposed_new_files="wrong")
    malformed_contracts = replace(analysis.proposed_scope, proposed_contract_changes=(object(),))

    scope_review = review_development_diff(
        _request(analysis=replace(analysis, proposed_scope=malformed_scope), handoff=handoff)
    )
    contracts_review = review_development_diff(
        _request(analysis=replace(analysis, proposed_scope=malformed_contracts), handoff=handoff)
    )

    assert scope_review.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR
    assert contracts_review.state is DevelopmentDiffReviewStateV1.VALIDATION_ERROR


def test_unexpected_file_and_unapproved_contract_drift_are_rejected() -> None:
    unexpected_file_review = review_development_diff(
        _request(
            changed_files=("app/unapproved.py",),
            changes=(
                DevelopmentDiffChangeEvidenceV1(
                    "app/unapproved.py", "ADDED", "a" * 64, "b" * 64, (), 1
                ),
            ),
        )
    )
    contract_drift_review = review_development_diff(
        _request(contract_changes=("Unapproved contract.",))
    )

    assert unexpected_file_review.review_status is DevelopmentDiffReviewStatusV1.REJECTED
    assert unexpected_file_review.contract_check.status is DevelopmentDiffReviewCheckV1.FAIL
    assert unexpected_file_review.contract_check.reason_code == "CONTRACT_DRIFT"
    assert unexpected_file_review.unexpected_files == ("app/unapproved.py",)
    assert contract_drift_review.review_status is DevelopmentDiffReviewStatusV1.REJECTED
    assert contract_drift_review.contract_check.status is DevelopmentDiffReviewCheckV1.FAIL
    assert contract_drift_review.contract_check.reason_code == "CONTRACT_DRIFT"


def test_failed_required_test_and_acceptance_evidence_are_rejected() -> None:
    review = review_development_diff(
        _request(
            tests=(
                DevelopmentDiffTestEvidenceV1(
                    "tests/test_codex_task.py", DevelopmentDiffReviewCheckV1.FAIL, "c" * 64
                ),
            ),
            acceptance_evidence=(
                DevelopmentDiffAcceptanceEvidenceV1(
                    "Task remains advisory.", DevelopmentDiffReviewCheckV1.FAIL, "e" * 64
                ),
            ),
        )
    )

    assert review.review_status is DevelopmentDiffReviewStatusV1.REJECTED
    assert review.tests_check.status is DevelopmentDiffReviewCheckV1.FAIL
    assert review.tests_check.reason_code == "TEST_FAILED"
    assert review.acceptance_check.status is DevelopmentDiffReviewCheckV1.FAIL
    assert review.acceptance_check.reason_code == "ACCEPTANCE_UNSATISFIED"


def test_incomplete_test_acceptance_and_authority_evidence_need_review() -> None:
    review = review_development_diff(
        _request(
            tests=(),
            acceptance_evidence=(),
            changes=(),
        )
    )

    assert review.review_status is DevelopmentDiffReviewStatusV1.NEEDS_REVIEW
    assert review.tests_check.status is DevelopmentDiffReviewCheckV1.UNKNOWN
    assert review.tests_check.reason_code == "TEST_EVIDENCE_INCOMPLETE"
    assert review.acceptance_check.status is DevelopmentDiffReviewCheckV1.UNKNOWN
    assert review.acceptance_check.reason_code == "ACCEPTANCE_EVIDENCE_INCOMPLETE"
    assert review.authority_check.status is DevelopmentDiffReviewCheckV1.UNKNOWN
    assert review.authority_check.reason_code == "AUTHORITY_EVIDENCE_INCOMPLETE"


def test_incomplete_contract_evidence_needs_review() -> None:
    review = review_development_diff(_request(contract_evidence=()))

    assert review.review_status is DevelopmentDiffReviewStatusV1.NEEDS_REVIEW
    assert review.contract_check.status is DevelopmentDiffReviewCheckV1.UNKNOWN
    assert review.contract_check.reason_code == "CONTRACT_EVIDENCE_INCOMPLETE"


def test_source_unknown_and_incomplete_owner_need_review() -> None:
    analysis = _analysis()
    source_unknown_review = review_development_diff(_request(analysis=analysis))
    owner_unknown_review = review_development_diff(
        _request(
            handoff=replace(
                prepare_codex_task(CodexTaskPreparationRequestV1(_analysis())), proposed_owner=None
            )
        )
    )

    assert source_unknown_review.review_status is DevelopmentDiffReviewStatusV1.NEEDS_REVIEW
    assert source_unknown_review.unresolved_unknowns == ("Human approval remains required.",)
    assert owner_unknown_review.review_status is DevelopmentDiffReviewStatusV1.NEEDS_REVIEW
    assert owner_unknown_review.owner_check.status is DevelopmentDiffReviewCheckV1.UNKNOWN
    assert owner_unknown_review.owner_check.reason_code == "OWNER_EVIDENCE_MISSING"


def test_handoff_context_binding_mismatch_is_rejected() -> None:
    analysis = replace(_analysis(), unknowns=())
    handoff = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    review = review_development_diff(
        _request(analysis=analysis, handoff=replace(handoff, context_digest="z" * 64))
    )

    assert review.review_status is DevelopmentDiffReviewStatusV1.REJECTED
    assert review.validation_status == "INVALID"
    assert review.reason_code == "SOURCE_BINDING_MISMATCH"


def test_source_analysis_binding_mismatch_is_rejected() -> None:
    analysis = replace(_analysis(), unknowns=())
    handoff = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    review = review_development_diff(
        _request(
            analysis=analysis, handoff=replace(handoff, source_analysis_identity="other/1.0.0")
        )
    )

    assert review.review_status is DevelopmentDiffReviewStatusV1.REJECTED
    assert review.validation_status == "INVALID"


def test_source_commit_and_knowledge_binding_mismatches_are_rejected() -> None:
    analysis = replace(_analysis(), unknowns=())
    handoff = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    source_commit_review = review_development_diff(
        _request(analysis=analysis, handoff=replace(handoff, source_commit="z" * 40))
    )
    knowledge_review = review_development_diff(
        _request(analysis=analysis, knowledge_identity="other")
    )

    assert source_commit_review.review_status is DevelopmentDiffReviewStatusV1.REJECTED
    assert knowledge_review.review_status is DevelopmentDiffReviewStatusV1.REJECTED


def test_invalid_owner_authority_and_provenance_are_rejected() -> None:
    analysis = replace(_analysis(), unknowns=())
    handoff = prepare_codex_task(CodexTaskPreparationRequestV1(analysis))
    review = review_development_diff(
        _request(
            analysis=analysis,
            handoff=handoff,
            observed_owner="descriptive owner",
            changes=(
                DevelopmentDiffChangeEvidenceV1(
                    "app/development_analysis/task.py",
                    "MODIFIED",
                    "a" * 64,
                    "b" * 64,
                    (),
                    1,
                    ("git push",),
                ),
            ),
        )
    )

    assert review.review_status is DevelopmentDiffReviewStatusV1.REJECTED
    assert review.validation_status == "VALID"
    assert review.owner_check.status is DevelopmentDiffReviewCheckV1.FAIL
    assert review.owner_check.reason_code == "OWNER_CONFLICT"
    assert review.authority_check.status is DevelopmentDiffReviewCheckV1.FAIL
    assert review.authority_check.reason_code == "AUTHORITY_VIOLATION"

    invalid_provenance = review_development_diff(
        _request(analysis=analysis, handoff=replace(handoff, evidence_digest="z" * 64))
    )
    assert invalid_provenance.validation_status == "INVALID"


def test_identical_input_produces_identical_logical_review() -> None:
    request = _request()

    assert review_development_diff(request) == review_development_diff(request)
