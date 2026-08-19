# Chappie AI v1

Chappie AI is a research-only reasoning subsystem shared by future agents. An
agent owns a workflow and responsibility; Chappie AI supplies model-backed,
provider-neutral interpretation through `ChappieAI.generate(...)`. Agents never
import Ollama or another concrete backend directly.

## Architecture and authority

The call boundary is:

`Agent → ChappieAI → ModelRouter → ModelProvider → backend`

AI may read supplied facts, interpret them, generate unvalidated hypotheses,
critique hypotheses, and request research. It may not alter source provenance,
events, timestamps, the frozen RDE methodology, Risk, orders, or execution state.
It cannot approve an order or declare deterministic research evidence. No AI
package imports an execution adapter.

The canonical output separates observed fact references, interpretations,
unvalidated hypotheses, contradictions, unknowns, evidence requests, and bounded
confidence. An observed fact is only a stable reference to an input `fact_id`;
unknown references fail closed. Interpretations remain model reasoning rather
than factual provenance. Hypotheses always begin `UNVALIDATED`; later RDE evidence
alone can support, reject, or contradict them.

Every request has an information cutoff. Input facts later than that cutoff are
rejected before a provider call. The `HistoricalEventAIContextV1` adapter
preserves event provenance and its cutoff, exposes no future outcomes, and has a
deterministic input identity.

## Providers and prompts

`ModelProvider` exposes provider/model identity, readiness, structured generation,
typed failures, and non-sensitive usage/latency metadata. `ModelRouter` maps model
profiles without changing callers. The current development profile is:

- profile: `LOCAL_DEFAULT`
- provider: Ollama at `http://localhost:11434`
- model: `qwen3:8b`

Endpoint, model, timeout, and temperature are configuration. Ollama credentials
do not exist. The adapter requests JSON-schema output, but Chappie independently
validates JSON shape, confidence, hypothesis status, and fact references.

Prompts are versioned as `news_interpretation/1.0.0`,
`market_interpretation/1.0.0`, `hypothesis_generation/1.0.0`, and
`hypothesis_critique/1.0.0`. Prompt identity and content contribute to the input
digest. They require supplied facts only, explicit unknowns/contradictions, no
invented causes, no trade instructions, and structured output only.

Provider chain-of-thought is neither required nor canonical. Ollama thinking is
disabled when supported and discarded if returned. It is not stored as evidence,
placed in a market hypothesis, or passed to Risk or execution. Observability is
limited to request/task/provider/model/prompt identity, latency, optional token
counts, digests, and validation status.

## Local development

Start Ollama separately and install the configured model, then run the optional
smoke test:

```bash
python -m app.tools.chappie_ai_smoke --provider ollama --model qwen3:8b
```

The smoke is opt-in and is not part of CI. Normal tests use `MockProvider` and
require no running model or network. The smoke prints validation metadata and
counts only; it never prints chain-of-thought.

V1 is request/validated-response only. It contains no agents, trading authority,
OpenAI or paid API integration, vector database, embeddings, semantic retrieval,
or long-term memory. Future providers and profiles may be added behind the same
router after separate review.

## Historical benchmark v1

`chappie_ai_market_benchmark/1.0.0` is a research-only, model-independent
benchmark. Its digest covers controlled cases, scoring, task contracts, horizons,
fixed temporal splits, and acceptance thresholds/policy; it deliberately excludes provider
and model identity. The benchmark reports two separate tracks:

- `REASONING_SAFETY`: factual fidelity, fact-reference validity, unsupported
  fact/cause rates, unknown admission, false contradictions, hypothesis quality,
  and schema validity.
- `BLIND_HISTORICAL`: separately evaluates forecast and hypothesis value after a
  response has been durably recorded and the hidden outcome is revealed.

The controlled safety inventory covers price/OI/funding combinations, breadth and
relative-strength divergence, high-beta strength/weakness, unavailable data,
conflicting signals, and supplied/no-supplied external context. It includes the
observed Qwen failure mode: “technical correction” can only be an explicitly
unvalidated hypothesis, while positive funding and rising OI are not inherently a
contradiction to falling price.

Historical `MARKET_ONLY` cases are sampled deterministically from verified compact
RDE rows by timestamp and state, never by future return. `EVENT_PLUS_MARKET` cases
will use `HistoricalEventAIContextV1` when the official macro corpus is available.
Development is 2023, validation is 2024, and 2025 onward is held out. Prompt or
threshold changes must never be chosen from held-out results.

Hidden outcomes are structurally absent from the AI-visible context. Existing RDE
forward returns (1h, 3h, 6h, 12h, 24h), MFE, MAE, and touches are revealed only
after the immutable model run has been written. Optional scenario forecasts use
typed UPSIDE/NEUTRAL/DOWNSIDE probabilities that sum to one; directional accuracy,
Brier score, log loss, horizon, and regime can then be reported. Hypotheses are
scored separately for fact references, falsifiability, target, horizon, and
RDE-queryability. They remain `UNVALIDATED` until research evidence is evaluated.

Uniform scenarios and a simple causal market-state baseline are retained for every
future comparison. Result classes are `UNSAFE`, `SAFE_NO_CLEAR_VALUE`,
`SAFE_WEAK_VALUE`, `SAFE_MEANINGFUL_VALUE`, and `INCONCLUSIVE`; none is a trading
approval.

Generated cases, immutable responses, scores, and reports live under ignored
`data/research/results/ai/benchmarks/`. A completed identical
benchmark/case/provider/model/prompt record is reused with `--resume`; `--rerun`
always creates a distinct immutable evidence record. Provider chain-of-thought is
never written.

The reasoning-safety case manifest always contains the full frozen 15-case
universe. `--limit`, `--case-id`, and `--split` select work for one invocation;
they never redefine that manifest. Without `--limit`, a reasoning-safety run
selects all 15 canonical cases. A validated legacy subset manifest may be
upgraded atomically to the complete universe without touching its immutable run
records. Aggregate reports list all completed evidence available at publication.

## Observable benchmark trace

The benchmark publishes provider-independent `AITraceEvent` records to a renderer.
They describe case selection, resume decisions, request/response lifecycle,
validation, canonical hypotheses/unknowns, scores, and safe latency/token metadata.
This is a Chappie system trace, not provider chain-of-thought: raw provider bodies
and private `thinking`/reasoning fields are discarded. A trace may state only that
provider reasoning was present and ignored.

The default CLI is compact and readable. `--verbose` adds validated canonical
interpretations, hypotheses, contradictions, unknowns, and evidence requests.
`--trace` adds request/case/response digests, prompt identity, and metadata.
`--quiet` suppresses console trace output, while `--json` preserves machine-only
console output. Rendering is additive and never changes execution, scoring,
immutable run identity, or generated report format. The event contract is reusable
by future research/UI consumers without granting any agent trading authority.

On a local Mac with Ollama already running, start conservatively with five safety
cases:

```bash
python -m app.tools.chappie_ai_benchmark \
  --provider ollama --model qwen3:8b --track reasoning-safety --limit 5 --resume
```

Local inference is bounded by the provider-neutral `--timeout-seconds` option
(30 seconds by default). A timeout, provider error, or validation rejection is
recorded as append-only operational attempt evidence, never as a scored model
run. The benchmark continues with later cases. `--resume` reuses only completed
immutable responses, so an incomplete case remains eligible for a later local
attempt; earlier failure evidence is retained separately.

Validation-rejected attempts may additionally contain a sanitized contract
diagnostic: validation code, field path, received value, expected vocabulary, and
structured JSON with provider-private `thinking`/reasoning fields removed. These
diagnostics are operational debugging evidence only, not canonical model output
or benchmark scores.

The command is opt-in, makes only the local Ollama request, and does not interact
with Risk, Core, strategy selection, orders, or execution. Do not change prompts,
benchmark rules, or thresholds after inspecting these results.

## Research reasoning contract v1.1

`research_reasoning/1.1.0` is a separate, opt-in output contract for local
research reasoning. It does not modify the frozen
`chappie_ai_market_benchmark/1.0.0` cases, scoring, prompts, digest, or existing
run evidence. Invoke it explicitly with `--reasoning-contract 1.1.0`; the
default remains the v1 market-interpretation contract.

Each v1.1 hypothesis is `UNVALIDATED` and must specify an exact supplied-fact
condition, asset, RDE metric, horizon, matched-control comparison, supporting
fact IDs, unknown dependencies, and a falsification condition. The runtime
converts only a valid hypothesis into an RDE query descriptor. It is not a
trading signal or an approved edge.

The separate `market_semantics/1.0.0` system contract has a deterministic
digest and is supplied as system context, not model evidence. It records that
OI change is exposure change rather than direction, funding is a payment
relationship rather than a forecast or participant identity, missing breadth
members are not negative members, relative strength is not absolute return, and
BTC is never `HIGH_BETA_L1`. The output validator rejects unsupported identity,
causal, and missing-data explanations. Future outcomes remain excluded from
the request and are evaluated only after the causal output is complete.

v1.1 runs use their own task/prompt identity and immutable run identity, so
they cannot resume, overwrite, or be aggregated with v1 run evidence.

The local-provider JSON Schema recursively mirrors the frozen v1.1 output
shape: nested fact references, statements, conditions, and hypotheses have exact
required fields, closed object properties, and the canonical enum vocabularies.
Dynamic fact-ID membership and all semantic/RDE checks remain Python-validator
responsibilities.

### Semantic hardening patch v1.1.1

`research_reasoning/1.1.1` is a new opt-in contract, not a reinterpretation of
frozen v1.1.0 evidence. It adds the machine-checkable invariant that every
`condition.fact_id` is also present in `supporting_fact_ids`. Conditions,
target asset/metric/horizon, expected relationship, comparison basis,
supporting fact IDs, and `UNVALIDATED` status are authoritative. The free-text
`falsification_condition` remains explanatory only.

The v1.1.1 validator preserves strict input identity: numeric strings are not
numbers, `"NULL"` and `"UNAVAILABLE"` are not null, and a fact whose actual value
is null may be conditioned on only with JSON `null`. It derives a structured
same-asset, matched-control falsification specification from the authoritative
target fields. It does not parse prose, benchmark IDs, or an executor mapping.

The contract distinguishes `STRUCTURALLY_VALID`, `RDE_QUERYABLE`, and future
`RDE_EXECUTABLE` assessment. A v1.1.1 queryable hypothesis is deliberately not
executable today and reports
`NO_CANONICAL_FACT_TO_RDE_PREDICATE_MAPPING` until a separately versioned,
typed `CanonicalResearchPredicateV1` mapping is implemented. No benchmark
result is changed by this patch. Run the hardened contract separately:

```bash
python -m app.tools.chappie_ai_benchmark \
  --provider ollama --model qwen3:8b --track reasoning-safety \
  --reasoning-contract 1.1.1 --timeout-seconds 120 --resume --verbose
```

## Deterministic research query compiler v1

`research_query_compiler/1.0.0` separates model-facing `AIResearchIntentV1`
from internal `CanonicalRDEQueryV1`:

```text
Facts → Chappie AI → Research intent → deterministic compiler → canonical RDE query
      → future RDE executor → ResearchEvidenceV1 → Chappie AI critique
```

The model supplies only an unvalidated question, target asset/horizon/metric,
bounded expected direction, selected supporting fact IDs, and unknowns. It is
not an authority for database columns, Python types, availability handling,
matched-control implementation, cache identities, SQL, or evidence status.

Facts may carry optional typed compiler metadata: asset, canonical field, value
type, availability, timestamp, unit, source type, and provenance digest. The
versioned registry covers `RETURN`, `OI_STATE`, `FUNDING_STATE`,
`POSITIVE_BREADTH`, `RELATIVE_STRENGTH`, `TAKER_PRESSURE`, `EVENT_TYPE`, and
`EVENT_PRESENT`. The compiler copies values from facts without LLM
reserialization. Unavailable/null facts are not predictive predicates under the
v1 availability policy; no neutral value is invented. Event predicates describe
occurrence only and never assert that an event caused a market move.

Compilation returns typed outcomes such as `COMPILED`, `AMBIGUOUS_INTENT`,
`UNKNOWN_FACT`, `UNSUPPORTED_PREDICATE`, `INVALID_TARGET`, and `INVALID_HORIZON`.
The compiler fixes comparison to same-asset `MATCHED_CONTROL`, so SOL targets
cannot accidentally compare against BTC controls. Query identity is a digest of
compiler policy, typed predicates, target fields, relationship, and source fact
IDs—not AI prose.

`RDEQueryExecutor.execute(query) -> ResearchEvidenceV1` is an interface only.
No historical query executor is implemented or invoked here. A compiled query
is therefore a candidate for future execution, not current evidence. Legacy
v1.1 hypotheses can receive a read-only compatibility assessment; their run
records, validators, scores, and identities remain immutable.

## Evidence critique closed loop v1

`research_evidence_critique/1.0.0` is a separate, research-only task contract.
It accepts an original `UNVALIDATED` hypothesis and immutable existing-cache RDE
evidence as stable supplied facts. The evidence status remains authoritative:
directional contradiction cannot become support, insufficient data cannot
become high-confidence support, and unknown fact IDs fail validation.

The authority chain is fixed: **AI proposes → compiler formalizes → capability
resolver decides executability → RDE tests → evidence is immutable → AI
critiques/revises → a new idea is a new UNVALIDATED hypothesis.** The critic has
no authority to reinterpret a deterministic value as an observed fact.

The output is an interpretation only: assessment, bounded confidence change,
fact references, unknowns, and optional inert next-research requests. It cannot
replace evidence fields or automatically execute another query. A changed claim
creates a new deterministic `UNVALIDATED` candidate bound to its parent
hypothesis ID, evidence digest, and revision reason; it never revises the
original experiment after seeing outcomes.

The run identity binds original hypothesis, evidence digest, task/prompt digest,
provider, and model. Its deterministic safety score measures assessment and
confidence consistency, fact reference and lineage validity, unsupported
evidence, and rationalization rate. It is not a directional forecast benchmark.
The separate `chappie_ai_rde_closed_loop_safety/1.0.0` contract fixes
contradiction, support, inconclusive, and insufficient-data safety cases without
changing the frozen market benchmark.

An opt-in local command exists but is never called by automated tests:

```bash
python -m app.tools.chappie_ai_rde_review \
  --provider ollama --model qwen3:8b \
  --evidence PATH/TO/EVIDENCE.json --hypothesis PATH/TO/HYPOTHESIS.json \
  --information-cutoff 2026-08-19T00:00:00Z --verbose
```

It invokes no RDE executor and has no trading, Risk, or execution authority.

## Closed-loop calibration benchmark v1

`chappie_ai_closed_loop_calibration/1.0.0` is a separate frozen safety benchmark
for evidence critique. Its predeclared synthetic cases cover strong, mild, and
mixed-slice contradiction; strong and mild descriptive support; inconclusive
evidence; and insufficient data. It measures assessment consistency, confidence
revision delta, zero-confidence rate by evidence family, fact fidelity, lineage,
rationalization, and trade-authority violations. It does not measure forecasting
skill or profitability.

Confidence is a model interpretation, never RDE authority. A contradiction may
reduce confidence to zero but is not required to do so; support may rise only
within bounds; inconclusive and insufficient evidence may not increase it. A
revised claim is classified descriptively as opposite direction, negation,
refinement, or no revision. Follow-up requests are inert and classified only as
market research, evidence diagnostic, unsupported, or none.

The two existing real BTC/ETH contradiction artifacts remain optional local
regressions; their evidence digests are not regenerated or rewritten by this
benchmark. Run a bounded local inspection first, then resume the full set:

```bash
python -m app.tools.chappie_ai_closed_loop_benchmark \
  --provider ollama --model qwen3:8b --limit 4 --verbose
python -m app.tools.chappie_ai_closed_loop_benchmark \
  --provider ollama --model qwen3:8b --resume --verbose
```

Project Knowledge/RAG and autonomous research recursion remain future work. The
benchmark stores only immutable, sanitized local-run evidence under ignored
research results; it does not execute follow-up RDE queries.

## Closed-loop authority separation v1.1

Calibration v1.0 exposed duplicated authority: the model was asked to echo an
RDE relationship that the deterministic evidence already owned. It remains
historical evidence (three valid contradiction runs, two rejected cautious
support runs, one valid inconclusive run, and one rejected insufficient-data
run); v1.1 does not rescore or rewrite it.

`research_evidence_critique/1.1.0` instead derives immutable
`EvidenceRelationV1` metadata before the provider is called:
`DESCRIPTIVE_DIRECTIONAL_SUPPORT → ALIGNED`,
`DESCRIPTIVE_DIRECTIONAL_CONTRADICTION → OPPOSED`, `INCONCLUSIVE →
INCONCLUSIVE`, and `INSUFFICIENT_DATA → INSUFFICIENT_DATA`. The model emits no
authoritative assessment. It can summarize evidence strength, retain or lower
confidence, state unknowns, and propose an independent `UNVALIDATED` successor.
Confidence is model interpretation only—not an RDE probability, calibrated
predictive probability, trade confidence, or sizing input.

`ALIGNED` does not mean proven, profitable, significant, or certain; cautious or
lower confidence is valid. `OPPOSED` means only that observed deterministic
evidence is directionally opposed, not that the logical opposite is validated.
For opposed, inconclusive, and insufficient evidence, confidence may not rise;
insufficient evidence also remains bounded at 0.5. A reversed successor remains
an `UNVALIDATED` candidate. Trace metadata presents the RDE relation separately
from the model confidence before/after.

The new frozen benchmark is `chappie_ai_closed_loop_calibration/1.1.0`. It
measures relation immutability, evidence fact fidelity, rationalization,
confidence-policy validity, lineage validity, and trade-authority violations.
Its cases reuse v1.0 conceptual evidence without tuning. Opt-in local execution
only (never automated) is:

```bash
python -m app.tools.chappie_ai_closed_loop_benchmark \
  --provider ollama --model qwen3:8b --contract 1.1.0 --limit 4 --verbose
python -m app.tools.chappie_ai_closed_loop_benchmark \
  --provider ollama --model qwen3:8b --contract 1.1.0 --resume --verbose
```

### Current local calibration evidence

The bounded local `qwen3:8b` v1.1 calibration has six valid cases out of seven:
three `OPPOSED`, two `ALIGNED`, and one `INCONCLUSIVE`. All accepted runs have
fact fidelity, confidence-policy validity, and lineage validity of `1.0`, with
zero rationalization and trade-authority violations. The remaining
`INSUFFICIENT_DATA` response retained model confidence `0.70`; it was correctly
rejected because the policy ceiling is `0.50`. This is retained useful evidence,
not a prompt- or model-tuning target. Model confidence is not a calibrated
probability and never carries execution authority.

The historical rejected attempt
`09b3caeec27f087fb65e9514086bf02f385c5582c1db0eb3b060b16f3e06ca13`
was written before failure persistence became contract-aware and is therefore
mislabelled as v1.0 metadata. It remains immutable historical evidence. Future
attempt artifacts bind the selected calibration identity/digest, critique task,
prompt identity/digest, case and evidence identities, provider/model, request
identity, and sanitized validation result.

Implemented research boundaries include provider abstraction, deterministic market
semantics, reasoning and query compilation, capability resolution, verified cache
identity, existing-cache RDE execution/cache tooling, immutable evidence, and the
closed loop. Not implemented: autonomous recursion, generic predicate methodology
for OI/funding/return/taker-pressure/breadth/relative-strength, Project Knowledge
or RAG, long-term memory, a Trading Agent, strategy authority, or live AI trading.
Chappie Knowledge v1 is the next planned retrieval-only layer for architecture,
authority, experiment history, negative results, and frozen contracts.

### Verified cache identity binding

`verified_research_cache_identity/1.0.0` is additive provenance metadata for
already-verified compact research caches. It binds the explicit frozen
methodology identity, aligned dependency, outcome manifest identity, and the
atomic event+match manifest identity. `bridge_schema` is a cache schema identity,
not methodology identity. The original manifests remain historical builder
evidence; a later identity binding must state that it was derived from existing
verified artifacts. Existing-cache execution must require a valid binding rather
than reconstructing provenance ad hoc.
## Existing-cache RDE evidence execution (research only)

The deterministic path is `query → capability → verified provenance → execution cache → executor → evidence artifact`.
`query_digest` expresses query semantics only. The reusable execution-cache identity additionally binds the executor digest, methodology, verified cache identity, event/match dependency, and outcome dependency. This is necessary because future-outcome mutation can leave a query digest unchanged while legitimately changing evidence.

Only VERIFIED, digest-checked successful evidence is reusable. Cache and result artifacts are ignored under `data/research/cache/ai/rde_queries/` and `data/research/results/ai/rde_queries/`; they are never runtime inputs. The CLI `python -m app.tools.rde_query_execute --query QUERY.json --dry-run` validates a typed artifact and reports capability/provenance without reading evidence rows or writing a cache. It accepts no SQL or expressions.
