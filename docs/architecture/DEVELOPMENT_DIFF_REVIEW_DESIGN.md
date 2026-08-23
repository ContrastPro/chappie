# DevelopmentDiffReviewV1 — planned advisory diff review

Status: planned design. `DevelopmentDiffReviewV1` is not implemented, has no
Control Plane endpoint, and does not grant implementation, Git, shell, file,
Knowledge, trading, Risk, or execution authority.

## Ownership and scope

`app.development_analysis` will own the contract and deterministic review
projection. The canonical owner is the exact request-local
`DevelopmentProposedScopeV1.proposed_owner` that was validated against
`DevelopmentContextMapV1.owner_candidates`; legacy `architecture_owners`
evidence categories are not package ownership.

The future capability is post-implementation advisory review only. It compares
an original `DevelopmentAnalysisV1`, its `CodexHandoffV1`, bounded change
evidence, and bounded test evidence. It never applies a diff, executes a
command, mutates Git, writes source files, or self-authorizes acceptance.

## Proposed input contract

`DevelopmentDiffReviewRequestV1` will bind:

- `source_analysis_identity`, `source_analysis_evidence_digest`, and source
  commit;
- `source_handoff_identity`, handoff task ID, and handoff evidence/context
  digests;
- target commit or a bounded working-tree-diff identity;
- a sorted, unique changed-file list;
- bounded change evidence per file: path, change kind, before/after digest,
  changed-symbol identifiers, line-count summary, and deterministic authority
  markers; no arbitrary command text or unbounded repository dump;
- bounded test evidence: declared test identifier, PASS/FAIL status, and an
  evidence digest; and
- the canonical Knowledge identity, manifest digest, and context digest.

All source bindings must match the original approved analysis/handoff. Dirty,
stale, missing, or mismatched evidence is invalid rather than repaired.

## Proposed output contract

`DevelopmentDiffReviewV1` will expose immutable bounded fields:

- identity, state, validation status, review status, and reason code;
- all source/provenance bindings and the reviewed changed-file list;
- expected files, unexpected files, and unresolved unknowns;
- `owner_check`, `contract_check`, `tests_check`, `acceptance_check`, and
  `authority_check`;
- bounded risks; and
- `human_approval_status`, always `HUMAN_APPROVAL_REQUIRED`.

Each check is a deterministic `PASS`, `FAIL`, or `UNKNOWN` result with a
bounded reason code. The small review-status set is:

- `MATCHES_APPROVED_SCOPE`: every required deterministic check passes;
- `NEEDS_REVIEW`: evidence is incomplete or an approved unknown remains; and
- `REJECTED`: scope, provenance, contract, test, or authority validation fails.

Neither `MATCHES_APPROVED_SCOPE` nor any model output authorizes mutation or
acceptance without a human decision.

## Direct future implementation scope

The anticipated direct files are:

- `app/development_analysis/models.py` for immutable DTOs and enums;
- `app/development_analysis/synthesis.py` only if the existing staged contract
  needs a bounded projection adapter;
- `app/development_analysis/task.py` for source-handoff compatibility;
- `app/development_analysis/__init__.py` for public exports;
- `tests/test_development_staged_synthesis.py` for source binding and proposed
  contract compatibility;
- `tests/test_codex_task.py` for Handoff provenance/owner compatibility; and
- a new `tests/test_development_diff_review.py` for deterministic review
  behavior.

No endpoint is proposed until the input/output DTOs are implemented. A future
additive Control Plane route, if needed, must remain read-only and separately
versioned.

## Required future tests

- approved changed file and approved contract change pass;
- unexpected file and unapproved contract drift are flagged;
- required test present/passed succeeds and missing/failed test is flagged;
- satisfied and unsatisfied acceptance criteria are distinguished;
- authority escalation is rejected while advisory/read-only changes pass;
- unresolved unknowns are preserved as `NEEDS_REVIEW`;
- analysis/handoff digest mismatch, dirty/stale/unbound evidence, invented
  owner, and missing owner fail closed; and
- `HUMAN_APPROVAL_REQUIRED` is retained for every non-invalid review.
