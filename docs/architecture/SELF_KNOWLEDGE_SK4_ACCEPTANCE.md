# Self-Knowledge SK-4 Acceptance

## Forensic baseline

The real local acceptance snapshot `9c302fba592e6222fcf861710daa6d288cb1fd9f` recorded
74 cases: 64 PASS and 10 FAIL. The primary evidence is the generated local file
`data/acceptance/development_copilot/result.json`; it is noncanonical evidence.

| Case | Retrieval / provider result | Runner result | Root cause |
| --- | --- | --- | --- |
| `ai_bypass_risk` | Correct AI authority records; provider answer exactly echoed question | FAIL | `MODEL_PROMPT_ECHO`, `SYNTHESIS_VALIDATION_GAP` |
| `knowledge_file` | Knowledge records selected; provider answer echoed question | false PASS | `MODEL_PROMPT_ECHO`, `ACCEPTANCE_GRADER_BUG` |
| `selection_store` | Knowledge records selected; provider answer echoed question | false PASS | `MODEL_PROMPT_ECHO`, `ACCEPTANCE_GRADER_BUG` |
| `research_orders` | No records selected; no provider call | FAIL | `RETRIEVAL_COMPOSITION_GAP` |
| `economic_calendar` | No records selected; no provider call | FAIL | `ZERO_KNOWLEDGE_POLICY_GAP`, `RETRIEVAL_COMPOSITION_GAP` |
| `unknown_news`, `unknown_calendar`, `unknown_memory` | No records selected; no provider call | FAIL | `ZERO_KNOWLEDGE_POLICY_GAP` |
| `git_honesty` | No records selected; no provider call despite bounded Git context | FAIL | `CONTEXT_ONLY_REASONING_GAP` |
| `session_13` | Same echo as `ai_bypass_risk` | false PASS | `ACCEPTANCE_GRADER_BUG` |

The Project QA parser does not project the question into an answer: it validates the provider's
structured `answer` field unchanged. Therefore the recorded exact echoes originate in model output,
not parser fallback or UI projection. The prior prompt did not clearly separate QUESTION, canonical
facts, workspace observations, and the required answer role.

## SK-4 policy

- A question echo is a typed synthesis failure.
- Current implementation questions with no canonical record return an honest, deterministic
  `UNKNOWN_CURRENT_CAPABILITY` result without calling AI.
- A bounded Git snapshot can produce a `CONTEXT_OBSERVATION`, explicitly labelled as workspace
  evidence rather than canonical Knowledge.
- Architecture/design intent composes a bounded set of general governance records; it does not
  create feature-specific facts.
- The acceptance runner rejects echoes everywhere and requires substantive file/selection analysis.

No News, Economic Calendar, or other feature-specific Knowledge record is introduced by SK-4.

## Clean-snapshot local A/B acceptance

SK-4 was committed as `7c4c949` and canonical Knowledge was rebuilt from that clean snapshot.
The prior local result remains preserved as `result_sk3_baseline.json`; the new generated evidence
is `result.json`. Both are ignored, noncanonical local artifacts.

| Case | SK-3 result | SK-4 result | Change |
| --- | --- | --- | --- |
| `ai_bypass_risk` | FAIL / echoed prompt | PASS / grounded | authority rejection now synthesizes from Execution authority |
| `research_orders` | FAIL / no Knowledge | PASS / grounded | general authority composition selected Execution authority |
| `economic_calendar` | FAIL / no Knowledge | PASS / grounded | architecture/current-vs-planned record selected without claiming implementation |
| `unknown_news`, `unknown_calendar`, `unknown_memory` | FAIL / validation error | PASS / deterministic unknown | no model call and no current-capability hallucination |
| `git_honesty` | FAIL / no Knowledge | PASS / context observation | bounded Git snapshot only; no diff claim |
| `knowledge_file`, `session_04`, `session_24` | false PASS / echoed prompt | FAIL / synthesis invalid | echo is now rejected, never accepted |
| `selection_store`, `selection_control_plane`, `selection_platform`, `selection_ai` | false PASS / echoed prompt | FAIL / concise non-substantive answer | echo eliminated; review-quality floor now detects incomplete review |
| `session_12`, `session_13` | false PASS / echoed prompt | PASS / grounded | session grading now agrees with primary policy |

The identical matrix recorded 74 total cases: **62 PASS / 12 FAIL**. It recorded **zero exact or
near prompt echoes**, including zero accepted echoes. All authority decisions (`ai_bypass_risk`,
`risk_http`, `rde_rewrite`, `research_orders`, `setup_scope`, and `dependency_direction`) passed;
Git honesty and unknown-current-capability discipline passed. The 30-turn session had 27 PASS / 3
FAIL, with no context/source leakage recorded and expected error recovery passing.

Question and workspace limits remained intact: 2K, 5K, 10K, 12K, 12K context, and combined legal
context requests passed; over-limit question/context requests were rejected pre-model. Median model
latency was 15,590 ms, p95 was 26,757 ms, and slowest was 41,450 ms.

The remaining 12 failures are not silently repaired here. Two are typed prompt-echo rejections;
the other ten are `SYNTHESIS_VALIDATION_GAP` findings where the local model produced a short
ownership sentence but did not satisfy the required substantive code/selection review dimensions.
This is a real local-model/review-quality limitation to review, not a reason to weaken the gate.

**Readiness:** `CHAPPIE_DEVELOPMENT_COPILOT_PARTIAL`; architecture, unknown-feature planning,
code-review, Codex-plan, and News readiness remain `NOT_READY` until substantive file/selection
review reliability is resolved.

## SK4_REMAINING_FAILURE_FORENSICS

This is manual evidence review of the 12 SK-4 failures. Scores are
correctness/relevance/grounding/completeness/authority discipline, each 0–2.

| ID | Context and selected evidence | Response finding | Score | Classification |
| --- | --- | --- | ---: | --- |
| `knowledge_file` | `app/knowledge/store.py`; no projected source after validation | no answer; same complex prompt later repeatedly rejected | 2 | REAL_SYNTHESIS_FAILURE, PROMPT_CONTRACT_GAP |
| `control_plane_file` | Control Plane ownership | correct ownership/transport sentence; omits outside-business distinction | 7 | INSUFFICIENT_DETAIL |
| `platform_file` | Platform setup ownership | correct ownership sentence; omits I/O and forbidden business authority | 7 | INSUFFICIENT_DETAIL |
| `ai_file` | RDE evidence authority, not AI runtime ownership | answers RDE authority rather than the attached AI implementation | 3 | REAL_SYNTHESIS_FAILURE, RETRIEVAL_QUALITY_GAP |
| `persistence_file` | Persistence ownership | correct ownership sentence; omits Risk/trading separation | 7 | INSUFFICIENT_DETAIL |
| `selection_store` | Knowledge ownership | correct ownership; omits selected behavior, invariants, and elsewhere boundary | 7 | INSUFFICIENT_DETAIL |
| `selection_control_plane` | Persistence ownership, not Control Plane | irrelevant persistence statement for Control Plane selection | 2 | REAL_SYNTHESIS_FAILURE, RETRIEVAL_QUALITY_GAP |
| `selection_platform` | Platform setup ownership | correct ownership; omits visible selection and forbidden authority | 7 | INSUFFICIENT_DETAIL |
| `selection_persistence` | Persistence state ownership | correct boundary; omits visible selection/invariants | 7 | INSUFFICIENT_DETAIL |
| `selection_ai` | AI research ownership, not AI runtime | answers AI research ownership, not attached runtime surface | 4 | REAL_SYNTHESIS_FAILURE, RETRIEVAL_QUALITY_GAP |
| `session_04` | duplicate `knowledge_file` context | `PROJECT_QA_SYNTHESIS_INVALID`; no answer | 2 | REAL_SYNTHESIS_FAILURE, MODEL_STABILITY_LIMITATION |
| `session_24` | duplicate `knowledge_file` context | `PROJECT_QA_SYNTHESIS_INVALID`; no answer | 2 | REAL_SYNTHESIS_FAILURE, MODEL_STABILITY_LIMITATION |

The generated runner explicitly records two typed synthesis-invalid cases (`session_04` and
`session_24`). `knowledge_file` is the same input and has a null answer/validation terminal state,
but its original runner field says `SYNTHESIS_VALIDATION_GAP`; the runner did not persist the
underlying error reason for that primary occurrence. Three fresh identical probes all returned
`PROJECT_QA_SYNTHESIS_INVALID`, establishing stable failure for the input rather than one-off
session contamination.

### Retrieval quality and saturation

Relevant single-record contexts were supplied for Control Plane, Platform setup, Persistence, and
Knowledge ownership. They support short ownership answers but not the multi-dimensional review
requested. AI runtime was given RDE authority, Control Plane selection was given Persistence
ownership, and AI selection was given AI research ownership: these are materially mismatched.
The short Control Plane selection control retrieved eight mostly unrelated records (Exchange
execution, Shadow, AI research, Infrastructure, Research strategy, Market, and Research
methodology), demonstrating context saturation and weak path/selection precision.

### Local qwen3:4b controls

- Complex Knowledge-store file review: three of three attempts were typed synthesis-invalid.
- Shorter Knowledge-store review: grounded, useful answer identifying deterministic/provenance
  ownership and no visible violation.
- Shorter Control Plane selection review: detailed, but introduced an unsupported validation-risk
  claim; not safe as a review result.
- Two-stage Knowledge-store review: stage one validation-invalid; stage two returned generic
  architecture, not placement analysis.
- Two-stage Control Plane selection: stage one correctly named `ask_project`; stage two answered
  an unrelated AI-research placement claim.

Simple ownership/authority QA remains reliable. Multi-part source review shows a combination of
instruction-composition weakness, poor selected evidence for some paths/selections, and a LIGHT
model capacity limit. This supports `READY` for basic project questions, authority policing, and
bounded architecture design; `LIMITED` for package-ownership inspection; and `NOT_READY` for deep
code review or Codex implementation-plan generation.

No failure is a justified automated false negative at the current safety bar: the six
insufficient-detail answers are useful fragments but do not satisfy their explicit review tasks.
The remaining six are true synthesis failures. Counts: REAL_SYNTHESIS_FAILURE 6,
INSUFFICIENT_DETAIL 6, RUNNER_THRESHOLD_TOO_STRICT 0, MODEL_LIMITATION 3 (the stable complex
review input and its duplicates), RETRIEVAL_QUALITY_GAP 3, PROMPT_CONTRACT_GAP 1.

**Recommended next direction:** `E. MIXED — requires a small bounded combination`: first improve
selection/path retrieval precision, then test a minimal structured review prompt. Do not change
the LIGHT profile now; consider a larger model only later for an ADVANCED Development/Code Review
capability if the bounded prompt/retrieval experiment does not close the gap.

## Code-review precision experiment (uncommitted)

The bounded follow-up changes File and Selection composition only. Validated `app/...` paths now
resolve package-directory identity before lexical ranking, place the exact package-ownership record
first, include at most one package-specific authority/boundary record, and cap this review context
at four records. The five deterministic ownership paths are Knowledge, Control Plane, Platform
setup, AI, and Persistence. In particular, the AI runtime path now selects AI ownership plus the
AI authority boundary rather than AI research, and a Platform setup path cannot fall back to the
broader Platform Ollama record.

Project QA adds a compact review-only instruction for attached File/Selection context. It requests
visible code behavior, canonical package fact, architecture fit or bounded risk, and an explicit
limit. General Project QA keeps its existing instruction. Retrieval is versioned as
`chappie_knowledge_retrieval/1.3.0`; the Project QA prompt is versioned as `project_qa/1.0.4`.
No Knowledge record, source allowlist, authority rule, model, or Workbench protocol changed.

The deterministic retrieval/Project-QA/Control-Plane/acceptance-runner/architecture subset passed
63 tests. The requested real qwen3:4b review matrix was started after that check, but local
provider availability changed during the attempt: every AI-backed case returned typed
`AI_UNAVAILABLE` before model invocation, while the context-only Git-honesty gate remained valid.
No provider start, model pull, retry, or model change was attempted. Consequently this experiment
does not claim a 12/12 real-model result; rerun the same limited matrix once the configured local
provider is independently READY.
