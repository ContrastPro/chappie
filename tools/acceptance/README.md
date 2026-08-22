# Local Development Copilot acceptance

Run outside the Codex sandbox after a clean repository, ready canonical Knowledge, and ready
local Ollama/qwen3:4b:

```sh
.venv/bin/python tools/acceptance/chappie_copilot_acceptance.py
```

It calls the production Control Plane and `WorkspaceContextV1` contract using real repository
files, selections, and Git metadata. Results are written under
`data/acceptance/development_copilot/` as local, noncanonical `result.json` and `report.md`.
No internet, package installation, model download, or production-code modification is performed.

The run includes the semantic matrix plus a separate 30-turn mixed session, question limits up
to the production 12,000-character boundary, and WorkspaceContext limits up to its production
12,000-character boundary. Runtime is model-dependent and may take several minutes. Known-invalid
boundary cases are rejected before model inference where the production contract permits it.

The runner grades generated responses consistently across primary and session cases: a prompt echo,
empty synthesis, hard-authority violation, or non-substantive file/selection review is not a PASS.
Typed `UNKNOWN_CURRENT_CAPABILITY` responses are accepted only for questions that explicitly ask
about an unsupported current implementation.

## qwen3:14b one-command diagnostic

Run the bounded local diagnostic without changing configuration or installing models:

```sh
.venv/bin/python tools/acceptance/run_qwen3_14b_diagnostic.py
```

It writes ignored local evidence to `data/acceptance/model_ab/diagnostic/`, including
`report.md` and `result.json`.

## Focused qwen3:4b versus qwen3:14b A/B

```sh
.venv/bin/python tools/acceptance/run_model_ab.py
```

This explicitly selects each model for the same focused review/control cases and writes ignored
2.1.0 local evidence to `data/acceptance/model_ab/focused_2_1/report.md` and `result.json`.

### Evidence schema 2.1.0

Fresh runs use `chappie_model_ab/2.1.0`. They keep two deliberately separate evidence layers:

- **Pre-inference equality evidence** is the only comparison-validity gate: case/question,
  workspace binding, ordered retrieved record IDs and digests, expected and actual retrieval
  context digests, successful responses, and effective model IDs must agree.
- **Post-inference quality evidence** records each bounded answer, emitted
  `supporting_knowledge_fact_ids`, projected `supporting_record_ids`, validation metadata, and a
  digest of the canonical compact JSON representation of that evidence. The digest also binds the
  case, requested/effective model identities, and run ordinal, so a score cannot be copied across
  models, cases, or repeats.

Fact IDs are model citations in the `knowledge.<record_id>.<field>` namespace; projected record
IDs are the matching record-level source disclosure. They are not interchangeable, and neither
can invalidate pre-inference retrieval equality. Current production transport does not expose a
safe rejected structured response, so `safe_rejected_structured_output` is `null` unless an
acceptance-local, schema-safe source is available. Provider thinking, internal metadata, raw
transport payloads, and credentials are never captured.

Each result begins with an `UNSCORED` human manual rubric. A reviewer may perform the single
`UNSCORED -> SCORED` transition only when the scored response-evidence digest matches the current
response, the response is valid, all nine 0–2 dimensions are present, and notes are bounded.
Invalid responses are reliability evidence and cannot receive numeric quality scores. The runner
continues to declare no winner; selection requires a separate review decision.

Every runner write validates every response rubric before serialization and rejects malformed
states fail-closed. The runner does not load or edit an already persisted scoring artifact; its
available scoring API rejects re-scoring an already `SCORED` response. A future artifact-editing
workflow must retain that same transition check rather than treating JSON edits as a scoring API.

Existing 2.0.0 and older local artifacts are historical evidence. The runner does not upgrade or
rewrite them.
