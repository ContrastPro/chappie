# Self-Knowledge SK-3 Acceptance

## Scope

SK-3 improves deterministic retrieval composition for architecture-oriented Project QA.
It does not add Knowledge sources or records, change authority, introduce embeddings, or
invoke a model as part of its automated acceptance.

## Retrieval contract

`chappie_knowledge_retrieval/1.1.0`

`3b4323b6a2e696c1e0589f2af0ee6439bc42ce53eae1c439406759949d1b32f5`

The request compiler adds bounded, deterministic architecture dimensions for architecture,
ownership, dependency, authority, provider, and I/O questions. When a validated workspace
context supplies an active relative `app/...` path, its package segments are used as retrieval
hints only; source bodies are neither read nor supplied to the model.

Relation expansion is now opt-in: a caller must provide `related_to_ids`. Generic architecture
queries no longer acquire unrelated records merely because a selected record has a relation.

## Automated evidence

The deterministic regression suite proves both accepted SK-3 retrieval cases:

- `app/knowledge/store.py` with an architecture/dependency review selects
  `Package ownership: Knowledge`.
- An external-provider question scoped to `app.risk` selects `Platform I/O boundary` and does
  not expand `Setup Engine` implicitly. This preserves the correct boundary: provider I/O
  belongs behind infrastructure/platform adapters, while Setup Engine owns workstation setup
  lifecycle only.

The prior final-acceptance report records 10 PASS and 5 PARTIAL categories, but it explicitly
does not retain all original verbatim Workbench prompts. SK-3 therefore does not claim an exact
re-run of an unavailable 15-prompt corpus. The documented direct regressions are the durable,
deterministic evidence for the concrete NO_MATCH and misleading-relation failures.

## Limits and next acceptance

This source-only change has not rebuilt canonical Knowledge: the repository is intentionally
dirty until review and commit. No Ollama/model run was performed, so semantic Project QA
acceptance is `NOT_EXECUTED`; no claim is made about model prose, only retrieval context.

After review and a clean commit, canonical Knowledge must be rebuilt and the bounded real
Project QA acceptance rerun before declaring self-knowledge development readiness.
