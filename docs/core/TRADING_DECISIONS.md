# Trading Decisions

## Scope

This is the canonical current contract for turning a completed-market observation
into an advisory signal, a target-exposure intent, and (where active runtime uses
it) a mandatory risk decision. Market ingestion is specified in
[Market system](MARKET_SYSTEM.md); persistence, transport, fills, and settlement
have separate core boundaries.

```text
completed market snapshot
  -> Strategy.generate_signal
  -> IntentPlanner.plan
  -> DecisionPipelineResult
  -> RiskManager.evaluate (Portfolio + instrument rules)
  -> execution consumer / Paper consumer
```

Each is a separate responsibility. A signal, intent, risk decision, order, and fill
are different facts and must not be conflated.

## Domain Objects

`MarketSnapshot` is an immutable defensive copy of one market input: stable ID,
stream key, explicit `as_of_ms`, ordered closed candles, optional open candle,
event/correlation provenance, data version, and staleness state. Closed candles must
be strictly ordered, non-overlapping, and actually closed at `as_of_ms`; the latest
closed candle must agree with the history and an optional open candle must follow it.

`Signal` is an immutable strategy result: identity, exact input snapshot and stream,
explicit decision/effective time, direction, reason/explanation, strategy
name/version, immutable parameter snapshot, correlation ID, and safe metadata. It
contains no price, quantity, notional, time-in-force, adapter payload, credential,
or execution instruction.

`OrderIntent` is an immutable request to change target long exposure. It links to a
source signal and preserves stream, strategy identity, time, reason, and
correlation. It is not an order, fill, exchange request, or approval.

`RiskDecision` is the immutable mandatory-risk result. Enriched decisions retain
the market snapshot, portfolio identity/version, policy identity, sizing audit,
instrument-stream binding, and approved order parameters when applicable.

All financial values are finite `Decimal` values; implicit `float`, NaN, and
infinity are rejected. Timestamps are explicit non-negative milliseconds supplied by
the application; decision objects do not read wall-clock time. Structured mappings
are recursively restricted to immutable scalar and tuple values.

## Signal Contract

`SignalDirection` is exactly `BUY`, `SELL`, or `HOLD`. HOLD is a complete auditable
outcome, not `None`: insufficient history, stale input, and no qualifying setup are
normal non-actionable conclusions, not retries or failures.

Every signal binds to the snapshot ID and stream it analyzed. Current validation
allows approved entry reason codes only for BUY and approved exit reason codes only
for SELL; HOLD cannot reuse an actionable reason. `created_at_ms` and
`effective_at_ms` are currently equal. Parameters and metadata are frozen at decision
time for reproducibility.

Strategies use completed-candle history for decision logic. An open candle may be
present for separately defined analytics, but must not silently alter a
completed-candle decision.

## Strategy Boundary

The `Strategy` protocol exposes immutable identity/configuration, required history,
and `generate_signal(snapshot) -> Signal`. A strategy is synchronous, pure, and
deterministic for identical snapshot, configuration, and deterministic ID factory
sequence.

Strategies may analyze only their supplied snapshot and emit an explanatory signal.
They may not read cache/database, call a provider, access credentials, read a clock,
invoke risk/execution, mutate Portfolio, choose size, or construct exchange payloads.
Strategy selection is explicit in factory/composition and never creates a path around
the planner or RiskManager.

The versioned EMA crossover remains a baseline. Research candidates are also
versioned signal algorithms; they do not change this boundary or constitute execution
approval.

## Intent Contract

`IntentPlanner` is the only decision-layer translation from a signal to an
`OrderIntent`. Its typed result is one of:

- `CREATED`: BUY or SELL produced a valid intent.
- `NO_INTENT_FOR_HOLD`: HOLD deliberately produced no intent.
- `DECLINED`: an actionable signal deliberately produced no intent with an audit
  reason.

The current long-only planner maps BUY to `ENTER_LONG` with its configured positive
target exposure and SELL to `EXIT_LONG` with target exposure zero. HOLD allocates no
intent ID. BUY and SELL do not mean “submit now”; they express only desired
post-trade exposure.

An enter intent requires `0 < target_exposure <= 1`; an exit requires exactly zero.
Intent construction preserves signal ID, stream, strategy identity, effective time,
and correlation. The planner is pure and has no Portfolio, balance, instrument-rule,
clock, I/O, risk, or execution dependency.

## Planner Responsibilities

`TradingDecisionPipeline` owns synchronous strategy/planner orchestration. It
validates a ready snapshot, obtains a run ID, evaluates the strategy, validates the
signal, calls the planner once, validates the plan and any intent, then returns an
immutable `DecisionPipelineResult`.

The result is `HOLD`, `INTENT_CREATED`, or `PLANNER_DECLINED`, with input snapshot,
strategy/planner identities, signal and plan, optional intent, reason, correlation,
completed stages, and safe metadata. There is no `FAILED` domain outcome: invalid
contracts, factory failures, and unexpected collaborator failures are typed errors.
A strategy failure prevents planner invocation; a planner failure yields no partial
result.

The pipeline is stateless and reusable. It does not access cache, network, database,
Portfolio, or time; it cannot execute an order or retain a processed-ID cache. The
coordinator owns event delivery and bounded process deduplication; persistence and
execution own durable idempotency.

## Exposure Model

The model is long-only Spot target exposure in `[0, 1]`, not direct quantity or
notional. Current Portfolio state, completed-market reference price, and instrument
rules determine executable quantity at the risk boundary. This prevents a strategy
from owning capital sizing.

Risk evaluates an intent against `PortfolioSnapshot`, `MarketSnapshot`, and
`InstrumentRules`. It verifies time/stream/asset/valuation consistency and computes
a deterministic long-only order envelope. It may approve unchanged exposure, modify
it under policy/rules/caps, or reject it with structured reasons. Approval is
necessary before execution but is neither a fill nor a Portfolio mutation.

Existing-position behavior is risk-owned: entry can increase an existing long only
within policy/rules/capital limits; exit evaluates the available long and supports
the guarded full-exit/dust behavior of the risk model. Signals and intents introduce
no short, leverage, or arbitrary averaging-down capability.

## HOLD / BUY / SELL Semantics

| Signal | Planner result | Intent meaning | Execution meaning |
| --- | --- | --- | --- |
| HOLD | `NO_INTENT_FOR_HOLD` | No requested exposure change | No order path starts. |
| BUY | normally `CREATED` | `ENTER_LONG` to configured target | Requires separate Risk approval and execution controls. |
| SELL | normally `CREATED` | `EXIT_LONG` to zero target | Requires separate Risk approval and execution controls. |
| BUY or SELL | `DECLINED` | No intent, explicit reason | No order path starts. |

A repeated directional signal is not an implicit repeated trade. Strategy is
advisory, planner is position-unaware, and Risk plus durable execution controls own
whether a particular adjustment is permissible or already processed.

## Risk Boundary

Risk is mandatory for an intent that may reach execution. Strategies, planners, AI
or research components, and the pipeline cannot manufacture a RiskManager approval.
Risk owns policy evaluation, Portfolio/rules compatibility, quantity/notional,
rounding, caps, budget checks, approved parameters, and sizing audit.

`RiskDecisionOutcome` is `APPROVED`, `REJECTED`, or `MODIFIED`. APPROVED preserves
requested exposure with no rejection reasons; REJECTED has no approved exposure and
has structured reasons; MODIFIED preserves an approved exposure and modification
basis. These are immutable audit facts, not invitations to retry.

## Determinism and Idempotency

Equal immutable input/configuration with deterministic ID factories yields equal
decision semantics. Production UUID factories may change independent evaluation IDs,
not direction, target, reason, or version semantics.

Decision determinism is not execution idempotency. The coordinator retains bounded
in-memory event-result deduplication and returns the recorded result for a duplicate
event ID. Durable one-intent/one-post and ambiguity/no-retry guarantees belong to
persistence/execution, not a hidden pipeline cache. A restart must rely on those
durable controls, never strategy memory.

## Declines / Rejections / Errors

- `HOLD` is a normal non-actionable strategy conclusion.
- Planner `DECLINED` is a normal deliberate refusal to form an intent.
- Risk `REJECTED` is a normal mandatory policy/rules/Portfolio refusal.
- Validation, invariant, configuration, factory, and unexpected collaborator errors
  are exceptions, never substitutes for HOLD, decline, or rejection.
- Execution ambiguity/failure is owned by execution safety and must not trigger an
  automatic retry from this layer.

In active Testnet runtime, unexpected execution-consumer failure activates
process-local SAFE_STOP. Later candle events cannot create another execution attempt;
a fresh process is required. SAFE_STOP is containment, not a decision retry/recovery
state machine.

## Data Ownership

| Data / action | Owner |
| --- | --- |
| Completed history and staleness policy | Market snapshot factory / market system |
| Algorithmic interpretation and signal reason | Strategy |
| Signal-to-target translation | Intent planner |
| Portfolio, rules, quantity, notional, limits | RiskManager |
| Durable idempotency, order lifecycle, transport, reconciliation | Persistence / execution runtime |
| Fills, settlement, Portfolio mutation | Authoritative execution/settlement path |
| Event delivery and bounded process dedup | Trading decision coordinator |

The coordinator creates one shared snapshot and pipeline result for an accepted
`CandleClosed` event, then delivers that decision to consumers. It does not ask each
consumer to re-run strategy reasoning. Paper and shadow consume the result; Testnet
failure uses SAFE_STOP instead of automatic retry.

## Observability

The audit chain joins event/correlation provenance, snapshot ID, signal ID, intent
ID, pipeline run ID, strategy/planner versions, reason codes, stages, and risk audit
where risk is evaluated. Human explanations do not replace structured identities,
versions, inputs, or outcomes.

Logs/status may expose sanitized decision facts and safe error categories. They must
not expose credentials, signatures, private responses, mutable application objects,
or unbounded payloads.

## Safety Invariants

- Strategies are advisory-only: they cannot execute, size, approve, or settle.
- Planner cannot bypass RiskManager and has no Portfolio or exchange access.
- Signal, intent, risk decision, order, and fill remain distinct immutable facts.
- Only completed-candle data informs a completed-candle decision.
- Signal/intents retain snapshot/stream/strategy/correlation provenance; pipeline
  validation rejects mismatches.
- HOLD and deliberate declines create no intent/order capability.
- Long-only exposure stays in `[0, 1]`; quantity and price belong to Risk/execution.
- Pure pipeline has no hidden dedup, retry, I/O, or stateful execution shortcut.
- Testnet execution failure is fail-closed through SAFE_STOP.

## Tests / Verification

- `tests/test_trading_domain.py`: immutable snapshots/signals/intents/risk decisions,
  Decimal validation, completed/open candle separation, timestamps and relationships.
- `tests/test_intent_planner.py`: BUY/SELL/HOLD mapping, frozen configuration,
  deterministic IDs, no-I/O/no-Portfolio boundary, decline semantics, typed failures.
- `tests/test_trading_pipeline.py`: ordering, cross-model validation, immutable
  results, stateless repeats, deterministic semantics, no infrastructure dependency.
- `tests/test_risk_manager.py`: sizing, caps/rules/valuation, existing-position and
  exit behavior, rejection/modification, and input immutability.
- `tests/test_trading_coordination.py`: shared evaluation, event dedup, consumer
  isolation, Testnet SAFE_STOP/no retry, and Paper/shadow delivery.

## Historical Notes

The former `TRADING_DOMAIN.md` and `TRADING_PIPELINE_DESIGN.md` were Stage-2 design
records. Their useful contracts are consolidated here. Milestone labels,
single-EMA-only recommendations, proposed module layouts, implementation plans, and
open questions overtaken by current strategy/risk/coordinator/MVP boundaries are
historical context, not current requirements.

The early position-unaware planner intentionally remains target-exposure-only.
Portfolio state, price, rules, and executable quantity are risk-owned; the historical
idea that a future planner might own Portfolio state is not an active contract.

## Source Invariant Crosswalk

| Source file | Source section / unique invariant | Destination | Status |
| --- | --- | --- | --- |
| `TRADING_DOMAIN.md` | 1: Stage-2 scope and infrastructure independence | Scope; Historical Notes | MERGED / HISTORICAL_ONLY |
| `TRADING_DOMAIN.md` | 2: snapshot → signal → intent → risk vocabulary and identity chain | Scope; Domain Objects; Observability | PRESERVED |
| `TRADING_DOMAIN.md` | 3: frozen models, Decimal/time/version/metadata rules | Domain Objects | PRESERVED |
| `TRADING_DOMAIN.md` | 4: defensive completed-candle snapshot/open-candle separation | Domain Objects; Signal Contract | PRESERVED |
| `TRADING_DOMAIN.md` | 5: advisory BUY/SELL/HOLD signal, no sizing/order instruction | Signal Contract; Strategy Boundary | PRESERVED |
| `TRADING_DOMAIN.md` | 6: target exposure and ENTER_LONG/EXIT_LONG constraints | Intent Contract; Exposure Model | PRESERVED |
| `TRADING_DOMAIN.md` | 7: immutable mandatory risk outcomes/audit values | Domain Objects; Risk Boundary | PRESERVED |
| `TRADING_DOMAIN.md` | 8: pure deterministic strategy and normal HOLDs | Strategy Boundary; Declines / Rejections / Errors | PRESERVED |
| `TRADING_DOMAIN.md` | 9: EMA baseline/crossover and baseline-only recommendation | Strategy Boundary; Historical Notes | MERGED / SUPERSEDED as sole-strategy recommendation |
| `TRADING_DOMAIN.md` | 10: typed validation, safe errors, deep immutability | Domain Objects; Declines / Rejections / Errors | PRESERVED |
| `TRADING_DOMAIN.md` | 11: proposed M2.2 module layout | Historical Notes | HISTORICAL_ONLY |
| `TRADING_DOMAIN.md` | 12: market/strategy/planner/risk/execution boundaries | Data Ownership; Safety Invariants | PRESERVED |
| `TRADING_DOMAIN.md` | 13–15: questions, ADR references, implementation plan | Historical Notes | HISTORICAL_ONLY |
| `TRADING_DOMAIN.md` | 16: later order/fill/Portfolio extension boundary | Scope; Exposure Model | MERGED |
| `TRADING_PIPELINE_DESIGN.md` | 1–3: synchronous stateless pipeline/API/no I/O or Risk | Planner Responsibilities | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 4: result outcomes, stages, no FAILED result | Planner Responsibilities; Declines / Rejections / Errors | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 5–7: typed planner API, long-only mapping, plan invariants | Intent Contract; HOLD / BUY / SELL Semantics | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 8: evaluation order and cross-model validation | Planner Responsibilities; Safety Invariants | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 9: factory ownership and audit chain | Determinism and Idempotency; Observability | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 10: deterministic semantics vs execution idempotency | Determinism and Idempotency | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 11: error policy/no retry/typed causes | Declines / Rejections / Errors | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 12: proposed M2.4 module layout | Historical Notes | HISTORICAL_ONLY |
| `TRADING_PIPELINE_DESIGN.md` | 13: contract testing strategy | Tests / Verification | PRESERVED |
| `TRADING_PIPELINE_DESIGN.md` | 14–15: questions and implementation plan | Historical Notes | HISTORICAL_ONLY |
