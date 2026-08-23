"""Immutable, consumer-neutral implementation-context and sufficiency contracts.

This package is intentionally a data-only foundation for future adapters.  It
does not retrieve repository data, invoke models, or grant operational
authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

IMPLEMENTATION_CONTEXT_IDENTITY = "chappie_implementation_context/1.0.0"
_MAX_ITEMS = 64
_MAX_TEXT = 1_000
_MAX_RESPONSE_BUDGET = 16
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,199}")
_UNSAFE_AUTHORITY = re.compile(
    r"\b(?:write files?|mutate|execute orders?|place trades?|shell execution|git (?:commit|push)|"
    r"rebuild knowledge|deploy)\b",
    re.IGNORECASE,
)


class ImplementationContextStateV1(StrEnum):
    CURRENT = "CURRENT"
    PROPOSED = "PROPOSED"
    UNKNOWN = "UNKNOWN"


class ImplementationSymbolKindV1(StrEnum):
    MODULE = "MODULE"
    CLASS = "CLASS"
    FUNCTION = "FUNCTION"
    DTO = "DTO"
    ENDPOINT = "ENDPOINT"
    VALIDATOR = "VALIDATOR"
    TEST = "TEST"


class ImplementationRelationKindV1(StrEnum):
    OWNS = "OWNS"
    CALLS = "CALLS"
    CONSUMES = "CONSUMES"
    PRODUCES = "PRODUCES"
    VALIDATES = "VALIDATES"
    EXPOSES = "EXPOSES"


class MissingContextRequestKindV1(StrEnum):
    SYMBOL = "SYMBOL"
    CONTRACT = "CONTRACT"
    FILE = "FILE"
    RELATION = "RELATION"
    TEST = "TEST"


class ImplementationContextSufficiencyStateV1(StrEnum):
    IMPLEMENTATION_CONTEXT_SUFFICIENT = "IMPLEMENTATION_CONTEXT_SUFFICIENT"
    TARGETED_EXPANSION_REQUIRED = "TARGETED_EXPANSION_REQUIRED"
    INSUFFICIENT = "INSUFFICIENT"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: str | None, name: str) -> str:
    if value is None or not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{name} is invalid")
    return value.strip()


def _identifier(value: str, name: str) -> str:
    value = _required_text(value, name)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def _path(value: str, name: str) -> str:
    value = _required_text(value, name).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("./") or not path.parts:
        raise ValueError(f"{name} must be a safe repository-relative path")
    return path.as_posix()


def _sha256(value: str, name: str) -> str:
    value = _required_text(value, name)
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _commit(value: str, name: str) -> str:
    value = _required_text(value, name)
    if not _COMMIT.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase Git commit digest")
    return value


def _normalize_texts(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if len(values) > _MAX_ITEMS:
        raise ValueError(f"{name} is too large")
    return tuple(sorted({_required_text(value, name) for value in values}))


@dataclass(frozen=True, slots=True)
class ImplementationContextItemV1:
    """A bounded fact, future scope item, or explicitly missing fact."""

    identifier: str
    state: ImplementationContextStateV1
    summary: str
    canonical_owner: str | None = None
    current_locator: str | None = None
    proposed_locator: str | None = None
    source_commit: str | None = None
    evidence_refs: tuple[str, ...] = ()
    missing_kind: MissingContextRequestKindV1 | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.identifier, "item identifier")
        _required_text(self.summary, "item summary")
        if self.canonical_owner is not None:
            _identifier(self.canonical_owner, "item canonical owner")
        if self.current_locator is not None:
            object.__setattr__(
                self, "current_locator", _path(self.current_locator, "current locator")
            )
        if self.proposed_locator is not None:
            object.__setattr__(
                self, "proposed_locator", _path(self.proposed_locator, "proposed locator")
            )
        if self.source_commit is not None:
            _commit(self.source_commit, "item source commit")
        refs = _normalize_texts(self.evidence_refs, "evidence ref")
        object.__setattr__(self, "evidence_refs", refs)
        if self.state is ImplementationContextStateV1.CURRENT:
            if not (self.current_locator and self.source_commit and refs):
                raise ValueError("CURRENT item requires locator, source commit, and evidence")
            if self.proposed_locator or self.missing_kind or self.missing_reason:
                raise ValueError("CURRENT item cannot contain proposed or unknown state")
        elif self.state is ImplementationContextStateV1.PROPOSED:
            if not self.canonical_owner or self.current_locator or self.source_commit or refs:
                raise ValueError("PROPOSED item cannot claim current repository evidence")
            if self.missing_kind or self.missing_reason:
                raise ValueError("PROPOSED item cannot contain unknown state")
        else:
            if not (self.missing_kind and self.missing_reason):
                raise ValueError("UNKNOWN item requires bounded missing-context detail")
            if any(
                (
                    self.canonical_owner,
                    self.current_locator,
                    self.proposed_locator,
                    self.source_commit,
                    refs,
                )
            ):
                raise ValueError("UNKNOWN item cannot claim repository or proposed scope")

    def payload(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "state": self.state.value,
            "summary": self.summary,
            "canonical_owner": self.canonical_owner,
            "current_locator": self.current_locator,
            "proposed_locator": self.proposed_locator,
            "source_commit": self.source_commit,
            "evidence_refs": list(self.evidence_refs),
            "missing_kind": self.missing_kind.value if self.missing_kind else None,
            "missing_reason": self.missing_reason,
        }


@dataclass(frozen=True, slots=True)
class ImplementationSymbolV1:
    kind: ImplementationSymbolKindV1
    qualified_identifier: str
    canonical_owner: str
    repository_file: str
    shape_summary: str
    state: ImplementationContextStateV1
    relation_refs: tuple[str, ...] = ()
    source_commit: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.qualified_identifier, "symbol identifier")
        _identifier(self.canonical_owner, "symbol owner")
        object.__setattr__(self, "repository_file", _path(self.repository_file, "symbol file"))
        _required_text(self.shape_summary, "symbol shape summary")
        refs = _normalize_texts(self.relation_refs, "relation ref")
        evidence = _normalize_texts(self.evidence_refs, "symbol evidence ref")
        object.__setattr__(self, "relation_refs", refs)
        object.__setattr__(self, "evidence_refs", evidence)
        if self.state is ImplementationContextStateV1.CURRENT:
            if not self.source_commit or not evidence:
                raise ValueError("CURRENT symbol requires source binding")
            _commit(self.source_commit, "symbol source commit")
        elif self.state is ImplementationContextStateV1.PROPOSED:
            if self.source_commit or evidence:
                raise ValueError("PROPOSED symbol cannot claim current source evidence")
        else:
            raise ValueError("UNKNOWN symbols are represented by ImplementationContextItemV1")

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "qualified_identifier": self.qualified_identifier,
            "canonical_owner": self.canonical_owner,
            "repository_file": self.repository_file,
            "shape_summary": self.shape_summary,
            "state": self.state.value,
            "relation_refs": list(self.relation_refs),
            "source_commit": self.source_commit,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class ImplementationContractContextV1:
    identity: str
    canonical_owner: str
    state: ImplementationContextStateV1
    current_shape: tuple[str, ...] = ()
    proposed_delta: tuple[str, ...] = ()
    version: str | None = None
    compatibility_constraints: tuple[str, ...] = ()
    producer_refs: tuple[str, ...] = ()
    consumer_refs: tuple[str, ...] = ()
    frozen: bool = False
    versioned: bool = False
    source_commit: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.identity, "contract identity")
        _identifier(self.canonical_owner, "contract owner")
        for name in (
            "current_shape",
            "proposed_delta",
            "compatibility_constraints",
            "producer_refs",
            "consumer_refs",
            "evidence_refs",
        ):
            object.__setattr__(self, name, _normalize_texts(getattr(self, name), name))
        if self.version is not None:
            _required_text(self.version, "contract version")
        if self.state is ImplementationContextStateV1.CURRENT:
            if not self.current_shape or not self.source_commit or not self.evidence_refs:
                raise ValueError("CURRENT contract requires current shape and source binding")
            _commit(self.source_commit, "contract source commit")
        elif self.state is ImplementationContextStateV1.PROPOSED:
            if (
                self.current_shape
                or not self.proposed_delta
                or self.source_commit
                or self.evidence_refs
            ):
                raise ValueError("PROPOSED contract cannot claim current shape or source evidence")
        else:
            raise ValueError("UNKNOWN contracts are represented by ImplementationContextItemV1")
        if self.frozen and not self.versioned:
            raise ValueError("frozen contract must be versioned")

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "canonical_owner": self.canonical_owner,
            "state": self.state.value,
            "current_shape": list(self.current_shape),
            "proposed_delta": list(self.proposed_delta),
            "version": self.version,
            "compatibility_constraints": list(self.compatibility_constraints),
            "producer_refs": list(self.producer_refs),
            "consumer_refs": list(self.consumer_refs),
            "frozen": self.frozen,
            "versioned": self.versioned,
            "source_commit": self.source_commit,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class ImplementationTestContextV1:
    module_path: str
    state: ImplementationContextStateV1
    target_ref: str
    behavior: str
    expected_result: str
    failure_condition: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "module_path", _path(self.module_path, "test module path"))
        _identifier(self.target_ref, "test target ref")
        for name in ("behavior", "expected_result", "failure_condition"):
            _required_text(getattr(self, name), name)
        if self.state is ImplementationContextStateV1.UNKNOWN:
            raise ValueError("test context must be CURRENT or PROPOSED")
        if self.behavior.strip().lower() in {"test it", "coverage", "add tests", "test"}:
            raise ValueError("test behavior is too generic")

    def payload(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "state": self.state.value,
            "target_ref": self.target_ref,
            "behavior": self.behavior,
            "expected_result": self.expected_result,
            "failure_condition": self.failure_condition,
        }


@dataclass(frozen=True, slots=True)
class ImplementationRelationV1:
    source_ref: str
    relation: ImplementationRelationKindV1
    target_ref: str
    source_locator: str | None = None
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.source_ref, "relation source ref")
        _identifier(self.target_ref, "relation target ref")
        if self.source_ref == self.target_ref:
            raise ValueError("relation cannot be self-referential")
        if self.source_locator is not None:
            object.__setattr__(
                self, "source_locator", _path(self.source_locator, "relation locator")
            )
        if self.evidence_ref is not None:
            _required_text(self.evidence_ref, "relation evidence ref")

    def payload(self) -> dict[str, object]:
        return {
            "source_ref": self.source_ref,
            "relation": self.relation.value,
            "target_ref": self.target_ref,
            "source_locator": self.source_locator,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True, slots=True)
class AuthorityBoundaryV1:
    allowed_responsibilities: tuple[str, ...]
    forbidden_responsibilities: tuple[str, ...]
    approval_requirement: str

    def __post_init__(self) -> None:
        allowed = _normalize_texts(self.allowed_responsibilities, "allowed responsibility")
        forbidden = _normalize_texts(self.forbidden_responsibilities, "forbidden responsibility")
        if not allowed or not forbidden:
            raise ValueError("authority boundary must state allowed and forbidden responsibility")
        if any(_UNSAFE_AUTHORITY.search(value) for value in allowed):
            raise ValueError("implementation context cannot grant mutation authority")
        if self.approval_requirement != "HUMAN_APPROVAL_REQUIRED":
            raise ValueError("implementation context requires human approval")
        object.__setattr__(self, "allowed_responsibilities", allowed)
        object.__setattr__(self, "forbidden_responsibilities", forbidden)

    def payload(self) -> dict[str, object]:
        return {
            "allowed_responsibilities": list(self.allowed_responsibilities),
            "forbidden_responsibilities": list(self.forbidden_responsibilities),
            "approval_requirement": self.approval_requirement,
        }


@dataclass(frozen=True, slots=True)
class MissingContextRequestV1:
    requested_kind: MissingContextRequestKindV1
    exact_target: str
    reason: str
    requesting_context_digest: str
    response_budget: int = 1

    def __post_init__(self) -> None:
        _required_text(self.reason, "missing-context reason")
        _sha256(self.requesting_context_digest, "requesting context digest")
        if not 1 <= self.response_budget <= _MAX_RESPONSE_BUDGET:
            raise ValueError("missing-context response budget is invalid")
        if self.requested_kind is MissingContextRequestKindV1.FILE:
            object.__setattr__(
                self, "exact_target", _path(self.exact_target, "missing file target")
            )
        else:
            object.__setattr__(
                self, "exact_target", _identifier(self.exact_target, "missing target")
            )
        if self.exact_target in {"*", "all", "repository"}:
            raise ValueError("missing-context target must be exact and bounded")

    def payload(self) -> dict[str, object]:
        return {
            "requested_kind": self.requested_kind.value,
            "exact_target": self.exact_target,
            "reason": self.reason,
            "requesting_context_digest": self.requesting_context_digest,
            "response_budget": self.response_budget,
        }


@dataclass(frozen=True, slots=True)
class ImplementationContextV1:
    goal: str
    source_commit: str | None
    repository_clean: bool
    knowledge_identity: str | None
    knowledge_manifest_digest: str | None
    knowledge_context_digest: str | None
    evidence_digest: str | None
    canonical_owner: str | None
    current_items: tuple[ImplementationContextItemV1, ...] = ()
    proposed_items: tuple[ImplementationContextItemV1, ...] = ()
    unknown_items: tuple[ImplementationContextItemV1, ...] = ()
    direct_files: tuple[str, ...] = ()
    symbols: tuple[ImplementationSymbolV1, ...] = ()
    contracts: tuple[ImplementationContractContextV1, ...] = ()
    tests: tuple[ImplementationTestContextV1, ...] = ()
    relations: tuple[ImplementationRelationV1, ...] = ()
    authority: AuthorityBoundaryV1 | None = None
    acceptance_criteria: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    search_guidance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.goal, "implementation goal")
        if self.source_commit is not None:
            _commit(self.source_commit, "source commit")
        for name in (
            "knowledge_identity",
            "knowledge_manifest_digest",
            "knowledge_context_digest",
            "evidence_digest",
        ):
            value = getattr(self, name)
            if value is not None:
                _required_text(value, name)
        if self.knowledge_manifest_digest is not None:
            _sha256(self.knowledge_manifest_digest, "knowledge manifest digest")
        if self.knowledge_context_digest is not None:
            _sha256(self.knowledge_context_digest, "knowledge context digest")
        if self.evidence_digest is not None:
            _sha256(self.evidence_digest, "evidence digest")
        if self.canonical_owner is not None:
            _identifier(self.canonical_owner, "canonical owner")
        for name, state in (
            ("current_items", ImplementationContextStateV1.CURRENT),
            ("proposed_items", ImplementationContextStateV1.PROPOSED),
            ("unknown_items", ImplementationContextStateV1.UNKNOWN),
        ):
            values = tuple(
                sorted(
                    {item.identifier: item for item in getattr(self, name)}.values(),
                    key=lambda item: item.identifier,
                )
            )
            if len(values) > _MAX_ITEMS or any(item.state is not state for item in values):
                raise ValueError(f"{name} is invalid")
            object.__setattr__(self, name, values)
        for name in ("direct_files", "acceptance_criteria", "risks", "search_guidance"):
            values = _normalize_texts(getattr(self, name), name)
            if name == "direct_files":
                values = tuple(sorted({_path(value, "direct file") for value in values}))
            object.__setattr__(self, name, values)
        symbols = tuple(
            sorted(
                {item.qualified_identifier: item for item in self.symbols}.values(),
                key=lambda item: item.qualified_identifier,
            )
        )
        contracts = tuple(
            sorted(
                {item.identity: item for item in self.contracts}.values(),
                key=lambda item: item.identity,
            )
        )
        tests = tuple(
            sorted(
                {(item.module_path, item.target_ref): item for item in self.tests}.values(),
                key=lambda item: (item.module_path, item.target_ref),
            )
        )
        relations = tuple(
            sorted(
                {
                    (item.source_ref, item.relation.value, item.target_ref): item
                    for item in self.relations
                }.values(),
                key=lambda item: (item.source_ref, item.relation.value, item.target_ref),
            )
        )
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "contracts", contracts)
        object.__setattr__(self, "tests", tests)
        object.__setattr__(self, "relations", relations)
        identifiers = {
            item.identifier
            for item in self.current_items + self.proposed_items + self.unknown_items
        }
        if len(identifiers) != len(self.current_items) + len(self.proposed_items) + len(
            self.unknown_items
        ):
            raise ValueError("implementation item state is contradictory")
        refs = {item.qualified_identifier for item in symbols} | {
            item.identity for item in contracts
        }
        for relation in relations:
            if relation.source_ref not in refs or relation.target_ref not in refs:
                raise ValueError("relation reference is not present in context")

    def stable_payload(self) -> dict[str, object]:
        return {
            "identity": IMPLEMENTATION_CONTEXT_IDENTITY,
            "goal": self.goal,
            "source_commit": self.source_commit,
            "repository_clean": self.repository_clean,
            "knowledge_identity": self.knowledge_identity,
            "knowledge_manifest_digest": self.knowledge_manifest_digest,
            "knowledge_context_digest": self.knowledge_context_digest,
            "evidence_digest": self.evidence_digest,
            "canonical_owner": self.canonical_owner,
            "current_items": [item.payload() for item in self.current_items],
            "proposed_items": [item.payload() for item in self.proposed_items],
            "unknown_items": [item.payload() for item in self.unknown_items],
            "direct_files": list(self.direct_files),
            "symbols": [item.payload() for item in self.symbols],
            "contracts": [item.payload() for item in self.contracts],
            "tests": [item.payload() for item in self.tests],
            "relations": [item.payload() for item in self.relations],
            "authority": self.authority.payload() if self.authority else None,
            "acceptance_criteria": list(self.acceptance_criteria),
            "risks": list(self.risks),
            "search_guidance": list(self.search_guidance),
        }

    @property
    def digest(self) -> str:
        return _digest(self.stable_payload())


@dataclass(frozen=True, slots=True)
class ImplementationContextSufficiencyV1:
    state: ImplementationContextSufficiencyStateV1
    reasons: tuple[str, ...] = ()
    missing_context: tuple[MissingContextRequestV1, ...] = ()


def assess_implementation_context_sufficiency(
    context: ImplementationContextV1,
) -> ImplementationContextSufficiencyV1:
    """Return a deterministic assessment without retrieval or model inference."""

    baseline_missing: list[str] = []
    if not context.goal.strip():
        baseline_missing.append("OBJECTIVE_MISSING")
    if not (context.source_commit and context.repository_clean and context.evidence_digest):
        baseline_missing.append("PROVENANCE_MISSING")
    if not context.canonical_owner:
        baseline_missing.append("CANONICAL_OWNER_MISSING")
    if context.authority is None:
        baseline_missing.append("AUTHORITY_BOUNDARY_MISSING")
    if not (context.direct_files or context.symbols):
        baseline_missing.append("SCOPE_LOCALIZATION_MISSING")
    if baseline_missing:
        return ImplementationContextSufficiencyV1(
            ImplementationContextSufficiencyStateV1.INSUFFICIENT, tuple(baseline_missing)
        )
    requests = tuple(
        MissingContextRequestV1(
            item.missing_kind,
            item.identifier,
            item.missing_reason or "MISSING_CONTEXT",
            context.digest,
        )
        for item in context.unknown_items
    )
    if requests:
        return ImplementationContextSufficiencyV1(
            ImplementationContextSufficiencyStateV1.TARGETED_EXPANSION_REQUIRED,
            ("TARGETED_CONTEXT_MISSING",),
            requests,
        )
    missing: list[str] = []
    if not context.contracts:
        missing.append("CONTRACT_CONTEXT_MISSING")
    if not context.tests:
        missing.append("TEST_CONTEXT_MISSING")
    if not context.acceptance_criteria:
        missing.append("ACCEPTANCE_CRITERIA_MISSING")
    if missing:
        return ImplementationContextSufficiencyV1(
            ImplementationContextSufficiencyStateV1.INSUFFICIENT, tuple(missing)
        )
    return ImplementationContextSufficiencyV1(
        ImplementationContextSufficiencyStateV1.IMPLEMENTATION_CONTEXT_SUFFICIENT
    )
