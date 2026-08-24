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

## Focused CODE benchmark for qwen2.5-coder:14b

The CODE candidate remains an advisory, unimplemented role.  The following acceptance-only runner
does not change the durable `qwen3:4b` default, Chappie configuration, Knowledge, Git state, or
production model policy:

```powershell
.\.venv\Scripts\python.exe tools\acceptance\run_code_model_benchmark.py --model qwen2.5-coder:14b
```

It first requires a clean committed repository, verified canonical Knowledge, reachable local
Ollama, the exact installed requested candidate, and a durable default of `qwen3:4b`. Supported
candidates are `qwen2.5-coder:14b` and `qwen3-coder:30b`; model selection is request-local and
has no fallback to the configured default.
It then sends bounded real repository evidence and safe synthetic proposals directly to the local
Ollama API. This is necessary because the production Control Plane allowlist intentionally does
not yet expose a CODE role.

The runner writes non-canonical local evidence under
`data/acceptance/code_model/focused_1_2/<sanitized-model>/`, for example
`qwen3-coder_30b`. Schema
`chappie_code_model_benchmark/1.2.0` records separate pre-inference context bindings and
post-inference response evidence, exact requested/effective model identity, status, bounded
answers, digests, latency, deterministic authority gates, three runs each of `DIFF_REVIEW` and
`IMPLEMENTATION_PLAN`, and an initially blank `UNSCORED` human 0–2 rubric. It never asks the
candidate to modify files, invoke Git or shell commands, alter Knowledge, make Risk decisions,
or submit orders.

The runner prints bounded preflight failure codes to stderr and performs no inference on a failed
preflight. It never replaces an existing `result.json`: choose a fresh explicit local run directory
when evidence already exists, for example:

```powershell
.\.venv\Scripts\python.exe tools\acceptance\run_code_model_benchmark.py --model qwen2.5-coder:14b --run-id focused_1_1_retry
```

`--run-id` is a single safe path segment beneath the selected model directory; it preserves
model-separated local evidence isolation.

Schema 1.2 accepts either one raw JSON object or exactly one outer `json` fence containing that
object; prose, other fence labels, multiple objects, arrays, nested fences, and oversized payloads
remain fail-closed. Its plan contract explicitly identifies a single-candidate CODE benchmark,
the CODE schema/output root, reference-only supplied Model A/B evidence, and no production CODE
role. Historical 1.0/1.1 evidence is never rewritten. A completed 1.1 artifact may be derived
offline into a fresh 1.2 directory without provider access:

```powershell
.\.venv\Scripts\python.exe tools\acceptance\run_code_model_benchmark.py --regrade-from <path-to-1.1-result.json> --run-id derived_1_2
```

The derived artifact records its source schema, source-artifact SHA-256, `OFFLINE_DERIVED`, and
`inference_rerun: false`; it preserves each original response and response digest.

The 1.1.0 methodology adds strict JSON contracts only for four semantic cases: explicit
per-proposal authority decisions, behavior-to-package ownership routing, AI-execution diff review,
and acceptance-tooling implementation planning. The runner validates decisions and mappings, not
incidental words in prose. It also bounds plan files and contracts to the supplied tooling scope.
Historical `1.0.0` evidence remains unchanged and is `LEGACY_UNSCORABLE` under these new structured
contracts; it may still be used for read-only forensic findings, never converted into a new PASS.

## Development Reasoning 2.0 dogfood harness

`run_development_dogfood.py` is a local, non-canonical acceptance/debug runner for one bounded
Development Reasoning request. It uses the existing read-only Development pipeline and never
changes FAST/CODE/DEEP mappings, configured defaults, Knowledge, Git state, or workspace files.

```powershell
.\.venv\Scripts\python.exe tools\acceptance\run_development_dogfood.py --model qwen3-coder:30b --case diff_review --run-id first_30b
```

The only currently supported candidates are `qwen3-coder:30b` (CODE) and `qwen3:14b` (DEEP),
bound exactly with no fallback. Before inference it requires a clean commit-bound Knowledge store,
reachable Ollama, the selected installed model, and durable default `qwen3:4b`.

Ignored evidence is atomically checkpointed under
`data/acceptance/development_dogfood/<model>/<run-id>/result.json`, plus a concise `report.md`.
Schema `chappie_development_dogfood/1.2.0` records PASS/FAIL/NOT_REACHED timing and bounded reason
codes for PREFLIGHT, CONTEXT_MAP, EVIDENCE_HANDLES, the three GROUNDING/PROPOSAL/REVIEW synthesis
and parse stages, STRUCTURAL_VALIDATION, READINESS, and HANDOFF. Historical 1.0.0 evidence is not
rewritten. It captures
only public synthesis DTO fields, safe provenance and handle metadata, and provider timing when
available; prompts, hidden reasoning, raw Knowledge bodies, credentials, and provider payloads
are not persisted. Existing evidence is never overwritten implicitly.

### Authorization-pair replay evidence

When a Development Analysis is `VALID` and `READY_FOR_TASK` and its
`CodexHandoffV1` is valid, the dogfood harness additionally writes an immutable
local `authorization_pair.json` beside its result artifact.  Its schema is
`chappie_development_authorization_pair/1.0.0`.  It preserves the exact bounded
public `DevelopmentAnalysisV1.payload()` and `CodexHandoffV1.payload()`, their
source/Knowledge/evidence/context bindings, `HUMAN_APPROVAL_REQUIRED`, and a
deterministic authorization-pair digest.

This is the only supported source for later deterministic DiffReview replay:

```text
Development Analysis (VALID / READY_FOR_TASK)
  -> CodexHandoffV1
  -> AuthorizationPairV1
  -> approved implementation evidence
  -> DevelopmentDiffReviewV1 replay
```

The artifact is acceptance-only and does not persist provider prompts, hidden
reasoning, raw provider payloads, raw Knowledge documents, source bodies, or
secrets.  Failed or non-ready analyses never create an authorization pair.
