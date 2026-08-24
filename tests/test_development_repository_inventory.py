from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.model_policy import DevelopmentModelRoleV1
from app.ai.schemas import canonical_json
from app.development_analysis import (
    DevelopmentAnalysisReadinessV1,
    DevelopmentAnalysisRequestV1,
    DevelopmentAnalysisStateV1,
    DevelopmentAnalysisV1,
)
from app.development_analysis import repository_inventory as integration
from app.development_analysis.models import DevelopmentEvidencePackV1, evidence_digest
from app.development_analysis.outcome import DevelopmentOutcomeStatusV1, project_development_outcome
from app.development_analysis.repository_inventory import (
    MAX_REPOSITORY_INVENTORY_ATTACHMENT_CHARACTERS,
    DevelopmentContextCapabilityV1,
    RepositoryInventoryIntegrationError,
    analyze_development_with_repository_inventory,
    build_repository_inventory_attachment,
    development_context_requirement,
)
from app.repository_inventory import (
    RepositoryInventoryCompletenessV1,
    RepositoryInventoryRequestV1,
    RepositoryInventoryStateV1,
    RepositoryRelationKindV1,
    build_repository_inventory,
)

GAPS = (
    "Full repository structure unavailable.",
    "Module usage patterns unavailable.",
    "Non-import consumer evidence unavailable.",
    "Test/dependency coverage incomplete.",
    "Insufficient evidence to prove safe cleanup candidates.",
)


@pytest.fixture
def bound_values(tmp_path: Path):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "config", "user.email", "inventory@example.invalid"), cwd=tmp_path, check=True
    )
    subprocess.run(("git", "config", "user.name", "Inventory Test"), cwd=tmp_path, check=True)
    source = tmp_path / "app/example.py"
    test = tmp_path / "tests/test_example.py"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    test.write_text(
        "from app.example import run\n\ndef test_run():\n    assert run() == 1\n", encoding="utf-8"
    )
    subprocess.run(("git", "add", "."), cwd=tmp_path, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=tmp_path, check=True)
    commit = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=tmp_path, text=True).strip()
    payload = {
        "source_commit": commit,
        "repository_clean": True,
        "knowledge_identity": "chappie_knowledge/1.0.0",
        "knowledge_manifest_digest": "2" * 64,
        "knowledge_context_digest": "3" * 64,
        "workspace_context_digest": None,
        "requested_role": DevelopmentModelRoleV1.CODE.value,
        "requested_model": "qwen3-coder:30b",
        "effective_model": "qwen3-coder:30b",
        "profile_identity": "profile",
        "capability_identity": "capability",
        "record_ids": (),
        "record_digests": (),
        "allowed_fact_ids": (),
    }
    evidence = DevelopmentEvidencePackV1(
        **{**payload, "requested_role": DevelopmentModelRoleV1.CODE},
        evidence_digest=evidence_digest(payload),
    )
    analysis = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Review repository hygiene.",
        evidence,
        unknowns=GAPS,
        acceptance_criteria=("Identify only evidence-backed cleanup candidates.",),
        validation_status="VALID",
        readiness=DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE,
        context_map=SimpleNamespace(context_digest="4" * 64),
    )
    request = RepositoryInventoryRequestV1(
        commit,
        tuple(RepositoryRelationKindV1),
        "5" * 64,
        evidence.knowledge_identity,
        evidence.knowledge_manifest_digest,
    )
    inventory = build_repository_inventory(request, tmp_path)
    return analysis, inventory


def test_repository_wide_gap_projects_typed_requirement_and_outcome(bound_values) -> None:
    analysis, _ = bound_values
    requirement = development_context_requirement(analysis)
    assert requirement is not None
    assert requirement.capability is DevelopmentContextCapabilityV1.REPOSITORY_INVENTORY
    assert requirement.blocker_code == "REPOSITORY_INVENTORY_REQUIRED"
    outcome = project_development_outcome(analysis)
    assert outcome.status is DevelopmentOutcomeStatusV1.NEEDS_CONTEXT
    assert outcome.required_context == "REPOSITORY_INVENTORY"
    assert outcome.safe_to_retry is False


@pytest.mark.parametrize("kind", ("FILE", "CONTRACT", "SYMBOL", "RELATION", "TEST"))
def test_exact_targeted_context_behavior_is_unchanged(kind: str) -> None:
    analysis = DevelopmentAnalysisV1(
        DevelopmentAnalysisStateV1.OK,
        "Analyze exact context.",
        unknowns=(f"Missing context: {kind.lower()} exact-target",),
        validation_status="VALID",
        readiness=DevelopmentAnalysisReadinessV1.INSUFFICIENT_EVIDENCE,
    )
    assert project_development_outcome(analysis).required_context == kind


def test_attachment_is_deterministic_bounded_fact_only_and_reconstructable(bound_values) -> None:
    analysis, inventory = bound_values
    first = build_repository_inventory_attachment(analysis, inventory)
    second = build_repository_inventory_attachment(analysis, inventory)
    assert first == second
    assert first.inventory_digest == inventory.inventory_digest
    assert len(canonical_json(first.payload())) <= MAX_REPOSITORY_INVENTORY_ATTACHMENT_CHARACTERS
    text = canonical_json(first.payload()).casefold()
    assert "return 1" not in text and "knowledge statement" not in text
    assert "unused" not in text and "obsolete" not in text and "safe to delete" not in text
    assert [item.handle for item in first.evidence_groups] == ["RI1"]


@pytest.mark.parametrize(
    ("change", "code"),
    (
        (
            {
                "state": RepositoryInventoryStateV1.REPOSITORY_NOT_CLEAN,
                "completeness": RepositoryInventoryCompletenessV1.INCOMPLETE,
                "inventory_digest": None,
            },
            "REPOSITORY_INVENTORY_INCOMPLETE",
        ),
        ({"repository_clean": False}, "REPOSITORY_INVENTORY_DIRTY"),
        ({"source_commit": "f" * 40}, "REPOSITORY_INVENTORY_SOURCE_MISMATCH"),
        ({"knowledge_manifest_digest": "f" * 64}, "REPOSITORY_INVENTORY_KNOWLEDGE_MISMATCH"),
        ({"policy_identity": "unsupported/1.0.0"}, "REPOSITORY_INVENTORY_POLICY_UNSUPPORTED"),
        ({"policy_digest": "f" * 64}, "REPOSITORY_INVENTORY_POLICY_UNSUPPORTED"),
        ({"identity": "unsupported/1.0.0"}, "REPOSITORY_INVENTORY_CONTRACT_UNSUPPORTED"),
    ),
)
def test_inventory_binding_failures_are_typed(bound_values, change, code: str) -> None:
    analysis, inventory = bound_values
    invalid = replace(inventory, **change)
    with pytest.raises(RepositoryInventoryIntegrationError, match=code):
        build_repository_inventory_attachment(analysis, invalid)


def test_null_inventory_digest_is_rejected(bound_values) -> None:
    analysis, inventory = bound_values
    object.__setattr__(inventory, "inventory_digest", None)
    with pytest.raises(RepositoryInventoryIntegrationError, match="DIGEST_MISSING"):
        build_repository_inventory_attachment(analysis, inventory)


def test_true_attachment_budget_overflow_fails_closed(bound_values, monkeypatch) -> None:
    analysis, inventory = bound_values
    monkeypatch.setattr(integration, "MAX_REPOSITORY_INVENTORY_ATTACHMENT_CHARACTERS", 1)
    with pytest.raises(RepositoryInventoryIntegrationError, match="ATTACHMENT_BUDGET_EXCEEDED"):
        build_repository_inventory_attachment(analysis, inventory)


def test_explicit_rerun_preserves_original_request_and_supplies_attachment(bound_values) -> None:
    analysis, inventory = bound_values
    request = DevelopmentAnalysisRequestV1(analysis.request, DevelopmentModelRoleV1.CODE)

    class Service:
        def analyze(self, value):
            return value

    replay = analyze_development_with_repository_inventory(Service(), request, analysis, inventory)
    assert replay.request == analysis.request
    assert replay.repository_inventory_attachment.inventory_digest == inventory.inventory_digest
    assert project_development_outcome(analysis).status is DevelopmentOutcomeStatusV1.NEEDS_CONTEXT


def test_supplied_inventory_clears_only_the_inventory_blocker(bound_values) -> None:
    analysis, inventory = bound_values
    attachment = build_repository_inventory_attachment(analysis, inventory)
    supplied = replace(
        analysis,
        evidence=replace(
            analysis.evidence,
            repository_inventory_identity=attachment.inventory_identity,
            repository_inventory_digest=attachment.inventory_digest,
            repository_inventory_policy_identity=attachment.policy_identity,
            repository_inventory_policy_digest=attachment.policy_digest,
            repository_inventory_attachment_digest=attachment.attachment_digest,
            original_evidence_digest=attachment.original_evidence_digest,
            original_context_digest=attachment.original_context_digest,
        ),
    )
    outcome = project_development_outcome(supplied)
    assert outcome.blocker_code != "REPOSITORY_INVENTORY_REQUIRED"
    assert outcome.required_context != "REPOSITORY_INVENTORY"


def test_non_inventory_request_does_not_trigger_or_attach_inventory(bound_values) -> None:
    analysis, inventory = bound_values
    ordinary = replace(analysis, unknowns=("A bounded implementation detail remains unknown.",))
    assert development_context_requirement(ordinary) is None
    with pytest.raises(RepositoryInventoryIntegrationError, match="NOT_REQUIRED"):
        analyze_development_with_repository_inventory(object(), object(), ordinary, inventory)
