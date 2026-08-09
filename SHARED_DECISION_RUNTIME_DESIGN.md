# Shared Decision Runtime Coordination Design

## 1. Status and scope

- Milestone: M3.10 Shared Decision Runtime Coordination Design.
- Status: design completed; design review completed.
- Implementation: completed; final safety review passed (2026-08-08).
- Design date: 2026-08-08.
- Implementation milestone: M3.11.
- Basis: ADR-039 and accepted ADR-043–ADR-047.

This document specifies the synchronous application/runtime coordination layer that
turns one confirmed `CandleClosed` into one shared trading decision and delivers the
same immutable objects to independently enabled shadow and paper consumers.

```text
CandleClosed
    -> TradingDecisionCoordinator
        -> Market Cache read
        -> one MarketSnapshotFactory.create()
        -> one TradingDecisionPipeline.evaluate()
        -> one DecisionPipelineResult
            -> optional ShadowDecisionConsumer
            -> optional PaperTradingEngine
```

M3.10 changes documentation only. It does not connect runtime, modify Settings,
remove the existing shadow engine, add persistence, or enable testnet/live execution.

## 2. Architectural role and ownership

`TradingDecisionCoordinator` belongs in `app/coordination`, not in the trading
domain. It decides when existing domain/application components run and isolates
delivery failures; it does not calculate EMA, plan an intent, size risk, execute,
or mutate Portfolio directly.

Recommended minimal package for M3.11:

```text
app/coordination/
    __init__.py
    trading.py
    models.py
    exceptions.py
```

`factories.py` is added only if ID-factory implementations cannot reuse an existing
factory module without reversing dependencies. The coordinator owns event-level
dedup, its subscription token and operational status. Composition root owns the
dispatcher, cache, pipeline, consumers and all resources.

## 3. Responsibilities and exclusions

The coordinator receives `CandleClosed`, performs event-level dedup, reads a
committed defensive cache snapshot, verifies event/cache consistency, creates one
`MarketSnapshot`, evaluates the pipeline once, delivers the same snapshot/result
instances to configured consumers, isolates ordinary consumer failures, retains a
stage-aware final result and maintains safe status/logging.

It is not responsible for strategy formulas, intent planning, Risk, execution,
Portfolio accounting, Binance, polling, persistence, networking, retries, live or
testnet trading.

## 4. Public contract

```text
TradingDecisionCoordinator(
    cache_reader: MarketCacheReader,
    snapshot_factory: MarketSnapshotFactory,
    decision_pipeline: TradingDecisionPipeline,
    snapshot_id_factory: SnapshotIdFactory,
    coordination_id_factory: CoordinationIdFactory,
    *,
    shadow_consumer: ShadowDecisionConsumer | None = None,
    paper_engine: PaperTradingEngine | None = None,
    history_limit: int = 1000,
    dedup_capacity: int = 10000,
    monotonic_clock: MonotonicClock | None = None,
)

start(dispatcher: MarketEventDispatcher) -> None
stop() -> None
process_event(event: CandleClosed) -> TradingDecisionCoordinationResult
status -> TradingDecisionCoordinatorStatus
```

`handle_candle_closed(event) -> None` is the subscribed adapter and delegates to
the same `process_event()` implementation. The direct API keeps orchestration
testable without duplicating dispatcher logic. Construction requires at least one
consumer. Limits and factories are validated eagerly.

## 5. Subscription lifecycle

Lifecycle is `STOPPED | RUNNING`.

- `start(dispatcher)` subscribes exactly one handler to `CandleClosed`, stores only
  the returned token, and enters `RUNNING` after successful subscription.
- Repeated `start` raises a typed state error.
- Dispatcher lifecycle remains owned by composition root.
- `stop()` unsubscribes the owned token and is idempotent when already stopped.
- State becomes `STOPPED` only after confirmed unsubscribe.
- On unsubscribe failure state remains `RUNNING`, the token is retained, and a typed
  error is exposed for safe retry/termination.
- Coordinator never closes cache, dispatcher, pipeline, consumers or provider.
- `process_event()` requires `RUNNING`, including direct test use.

Startup order: logging, market components, decision components, optional shadow,
optional paper, coordinator, subscription, polling. Shutdown order: polling stop,
coordinator stop, provider close, logging flush.

### 5.1 Event processing sequence

The final synchronous sequence is normative:

1. Verify lifecycle is `RUNNING`.
2. Receive and type-check `CandleClosed`.
3. Look up `source_event_id` in coordinator dedup.
4. Validate the event.
5. Read the committed cache snapshot.
6. Validate event/cache consistency.
7. Generate coordination ID.
8. Generate snapshot ID.
9. Build exactly one `MarketSnapshot`.
10. Evaluate `TradingDecisionPipeline` exactly once.
11. Validate `DecisionPipelineResult` and its identity chain.
12. Deliver the same snapshot/result objects to optional shadow consumer.
13. Deliver the same snapshot/result objects to optional paper consumer.
14. Build the final immutable coordination result.
15. Store the completed result in bounded dedup.
16. Update immutable status/counters.

If paper is already `DEGRADED`, step 13 records `UNAVAILABLE` without calling
`paper_engine.process()`. Coordinator performs no retry at any step.

## 6. Event/cache consistency

Before allocating IDs, the coordinator reads
`cache_reader.get_snapshot(event.stream_key, limit=history_limit)` and verifies:

- non-empty defensive result and matching stream;
- event candle is present by `open_time_ms` and exactly equals the cached candle;
- history through the event candle is used for the decision;
- a newer open candle may exist but is excluded to prevent look-ahead;
- `MarketSnapshot.as_of_ms == event.occurred_at_ms`;
- source event ID and correlation cycle ID are preserved.

The verified checks must be extracted/reused from the existing shadow engine during
M3.11, not independently reinterpreted. Cache/mismatch failure creates no snapshot,
pipeline result or consumer call.

## 7. Snapshot, decision and object identity

After validation coordinator obtains one snapshot ID, calls
`MarketSnapshotFactory.create()` once, and calls
`TradingDecisionPipeline.evaluate(snapshot)` once. For one source event there is
exactly one snapshot ID, pipeline run ID, signal ID and at most one intent ID.

The pipeline result must reference the created snapshot, stream and correlation.
Disagreement is a typed invariant failure and prevents delivery.

With both consumers enabled these runtime guarantees are mandatory:

```text
shadow_snapshot is paper_snapshot
shadow_decision_result is paper_decision_result
shadow_decision_result.signal is paper_decision_result.signal
if an intent exists:
shadow_decision_result.order_intent is paper_decision_result.order_intent
```

No equal-but-copied model is allowed and coordinator creates no defensive copies
between consumer calls. Immutability makes shared identity safe.

## 8. Feature matrix

| Shadow | Paper | Behavior |
|---|---|---|
| false | false | Do not build Strategy, pipeline or coordinator; ingestion only. |
| true | false | Compute once and deliver only to shadow consumer. |
| false | true | Compute once and deliver only to paper engine. |
| true | true | Compute once and deliver the same instances to both. |

Paper does not require the shadow flag. Both flags remain false by default and
paper-only configuration is validated only when paper is enabled.

## 9. Shadow migration

M3.11 introduces:

```text
ShadowDecisionConsumer.consume(
    event: CandleClosed,
    snapshot: MarketSnapshot,
    decision_result: DecisionPipelineResult,
    coordination_id: str,
) -> ShadowDecisionRecord
```

It builds the existing record and invokes `ShadowDecisionObserver`. It does not read
cache, create snapshots, evaluate pipeline, subscribe to events or own event dedup.
Its status contains `handled_decisions`, `successful_records`,
`observation_failures`, `last_record` and `last_error`.

The existing `ShadowTradingDecisionEngine` remains in source during design. It may
become a compatibility wrapper, but composition root must never subscribe it together
with coordinator. There is exactly one active runtime decision path.

## 10. Consumer order and isolation

Order is shared compute, shadow consumer, PaperTradingEngine, final result/status.
Shadow comes first because it is observational and does not mutate Portfolio.

- Shadow `Exception` does not prevent paper.
- Paper `Exception` cannot undo delivered shadow output.
- Either/both failures produce a completed delivery-degraded result.
- Ingestion continues.
- `BaseException` is never suppressed.
- After `BaseException`, further consumer delivery is not guaranteed.

Trading outcome and consumer delivery outcome are separate concepts.

## 11. Paper degradation

If PaperTradingEngine health is already `DEGRADED`, coordinator continues shared
decision and shadow processing, does not call paper, records `UNAVAILABLE`, and
increments `paper_unavailable` once for the unique decision. It never resets paper.
Paper stays unavailable until external inspection, reconciliation and reconstruction.

If paper becomes degraded during a call, the current result records the explicit
failure/health and subsequent decisions use `UNAVAILABLE`.

## 12. Dedup and partial delivery

Dedup key is `source_event_id`. A bounded FIFO registry stores final immutable
`TradingDecisionCoordinationResult` objects. A hit returns the exact stored result,
does not read cache or allocate IDs, does not evaluate/call consumers, increments
`ignored_duplicates`, and does not move FIFO order. Oldest completed entry is
explicitly evicted at capacity.

Insertion is stage-aware:

- pre-decision failures before consumer attempts are not cached, allowing a later
  delivery to recover from a transient cache/snapshot/pipeline failure;
- after any consumer attempt, all configured statuses are completed and the result
  is cached, including `FAILED` and `UNAVAILABLE`;
- a valid shared decision with configured consumers unavailable is also cached;
- failed consumers are not retried for duplicate events;
- no internal retry exists.

Thus shadow-success/paper-failure never repeats shadow delivery. Protection is
process-local, capacity-bounded and not durable exactly-once. A `BaseException`
after an observable side effect remains a persistence/reconciliation concern.

The symmetric partial-delivery outcomes are explicit:

- shadow `DELIVERED`, paper `FAILED`: the shared decision remains successful, final
  result is cached, and neither consumer is called again for a duplicate;
- shadow `FAILED`, paper `DELIVERED`: paper delivery remains committed, final result
  is cached, and neither consumer is called again for a duplicate.

`DELIVERED`, `FAILED` and `UNAVAILABLE` are terminal delivery statuses for the
current process lifecycle.

## 13. Result model and delivery status

Immutable `TradingDecisionCoordinationResult` contains:

- coordination/source/correlation IDs and stream key;
- snapshot, pipeline run, signal/reason and optional intent IDs;
- immutable `market_snapshot` and `decision_result` references;
- enabled/status/result/safe error fields for each consumer;
- ordered completed stages;
- non-negative snapshot/decision/shadow/paper/total monotonic durations;
- bounded immutable metadata.

`DecisionConsumerStatus`:

- `DISABLED`: feature not configured;
- `DELIVERED`: consumer returned normally;
- `FAILED`: consumer was called and raised expected `Exception`;
- `UNAVAILABLE`: configured consumer deliberately not called, e.g. degraded paper;
- `NOT_ATTEMPTED`: shared decision was not available.

Ordered stages are `EVENT_RECEIVED`, `EVENT_VALIDATED`, `CACHE_READ`,
`SNAPSHOT_CREATED`, `DECISION_EVALUATED`, `SHADOW_DELIVERED`/`SHADOW_FAILED`,
`PAPER_DELIVERED`/`PAPER_FAILED`/`PAPER_UNAVAILABLE`, and `COMPLETED`. Model
validation rejects missing prerequisites and contradictory combinations.

Required invariants include:

- disabled shadow implies `shadow_status=DISABLED`, no record and no error;
- disabled paper implies `paper_status=DISABLED`, no cycle and no error;
- delivered shadow requires `shadow_record` and forbids shadow error fields;
- failed shadow requires bounded error category/message and no successful record;
- delivered paper requires `paper_cycle_result` and forbids paper error fields;
- failed paper requires bounded error category/message;
- unavailable paper has no cycle result and was not invoked;
- `NOT_ATTEMPTED` is valid only when shared computation did not reach delivery;
- snapshot/result IDs, stream and correlation must agree throughout the chain;
- completed stages contain no duplicates and follow the normative order;
- consumer status and completed-stage variant must agree exactly.

## 14. Coordinator status and counter semantics

Immutable `TradingDecisionCoordinatorStatus` includes lifecycle and:

- handled, successful, HOLD, intent, planner-declined and failed decisions;
- shadow deliveries/failures;
- paper deliveries/failures/unavailable;
- ignored duplicates;
- last event/coordination/snapshot/pipeline/signal/intent IDs;
- last signal, consumer statuses, paper outcome and safe error.

- `handled_events`: unique valid processing attempts accepted after dedup boundary;
- `ignored_duplicates`: only `source_event_id` dedup hits;
- `successful_decisions`: valid pipeline results, independent of delivery failures;
- `failed_decisions`: cache, snapshot, ID/invariant or pipeline computation failures;
- `shadow_deliveries`: successful shadow consumer returns;
- `shadow_failures`: ordinary shadow consumer exceptions;
- `paper_deliveries`: successful paper process returns;
- `paper_failures`: ordinary paper consumer exceptions;
- `paper_unavailable`: paper skipped because its configured path is unavailable,
  including pre-call `DEGRADED`.

Snapshot failures use `failed_decisions` with a precise typed error category; no
separate snapshot counter is introduced. HOLD, intent and planner-declined are
mutually exclusive subsets of successful decisions. Consumer failures never increase
`failed_decisions`. Consumer counters increment once per unique completed
coordination; duplicate hits increment only `ignored_duplicates`.

## 15. Errors and failure boundaries

Proposed hierarchy:

- `TradingCoordinationError`;
- `TradingCoordinationConfigurationError`;
- `TradingCoordinationStateError`;
- `TradingCoordinationInvariantError`;
- `TradingCoordinationCacheError`;
- `TradingCoordinationSnapshotError`;
- `TradingCoordinationPipelineError`;
- `TradingCoordinationShadowError`;
- `TradingCoordinationPaperError`;
- `TradingCoordinationIdFactoryError`.

Expected handler `Exception` is classified into safe status/result data and does not
break `MarketEventDispatcher`. Causes remain chained while public messages are
bounded. `BaseException` propagates after lifecycle/status cleanup.

| Failure | Pipeline | Shadow | Paper | Dedup result |
|---|---|---|---|---|
| Cache/event mismatch | no | no | no | no |
| Snapshot/ID factory | no | no | no | no |
| Pipeline/invariant | failed | no | no | no |
| Shadow Exception | computed | failed | still called | cached |
| Paper Exception | computed | retained | failed | cached |
| Both consumers | computed | failed | failed | cached |
| Paper pre-call degraded | computed | called if enabled | unavailable | cached |
| BaseException | re-raised | not suppressed | not suppressed | no durable guarantee |

## 16. Timing and logging

Market chronology comes only from event/snapshot. Monotonic time measures operational
durations; wall clock never decides candles, signals, fills or valuation.

Startup log: decision/shadow/paper enabled flags and safe component versions; paper
Portfolio ID/initial quote if explicitly formatted. Per decision: source event,
coordination, pipeline run, signal, intent, consumer statuses, paper outcome and
correlation. Never log full candle history, models, metadata, secrets or credentials.
Logs are not an audit store.

## 17. Composition and configuration proposal

When either feature is enabled, composition root creates one snapshot factory, EMA
Strategy, Intent Planner, pipeline, optional ShadowDecisionConsumer, optional paper
graph, and one coordinator. Coordinator is the sole decision subscriber.

```text
SHADOW_DECISIONS_ENABLED=false
PAPER_TRADING_ENABLED=false
DECISION_COORDINATOR_HISTORY_LIMIT=1000
DECISION_COORDINATOR_DEDUP_CAPACITY=10000
```

Future paper config covers initial balance, fee, latency, risk limits and instrument
rules. M3.10 does not add Settings code.

## 18. Application status proposal

Final application status exposes compact coordinator lifecycle/counters, last signal,
consumer statuses and paper health. Paper summary may include quote/base/equity,
realised/unrealised PnL, drawdown and last fill ID. Full history is excluded.

## 19. Offline integration plan

```text
FakeMarketProvider -> Polling -> Cache -> CandleClosed
    -> TradingDecisionCoordinator
    -> one MarketSnapshot -> one DecisionPipelineResult
        -> ShadowDecisionConsumer
        -> PaperTradingEngine -> Risk -> PaperExecution -> Portfolio
```

Assert factory/pipeline exactly once, `is` identity at both consumers, shadow record,
paper cycle, Portfolio state, no network, and no repeated work on duplicate event.
Also test every feature row, lifecycle, mismatch, stage-aware partial delivery and
degraded paper.

## 20. Real smoke plan

Only after M3.11 implementation/final review, run Binance public-data runtime with
both flags enabled. Verify bootstrap, `CandleClosed`, one shared signal, shadow and
paper outcomes, compact Portfolio status, no private API/order, and clean shutdown.
HOLD plus mark-to-market is sufficient evidence; BUY/SELL is not guaranteed.

## 21. Persistence and recovery boundary

M3.10/M3.11 provide no journal, durable replay, restart recovery or durable
exactly-once. Dedup and Portfolio are lost on restart. Consumer retry is absent
because partial delivery may already have side effects. M3.12 now defines the
append-only journal, checkpoint, stable-key, transaction and fail-closed recovery
contract in `PERSISTENCE_RECOVERY_DESIGN.md`; those guarantees begin only after
M3.13–M3.14 implementation and review.

M3.14 implementation закрывает runtime boundary: coordinator подписывается только
после successful Portfolio recovery. Он по-прежнему вычисляет один shared
`DecisionPipelineResult`; paper consumer строит durable identity из фактической closed
candle history, а shadow consumer не зависит от persistence. Serious paper persistence
failure переводит paper engine в `DEGRADED`; consumer isolation позволяет shadow path
продолжить без повторного запуска Strategy.

## 22. Design-review resolutions and deferred implementation details

Approved in M3.10 review:

1. Direct `process_event()` requires `RUNNING`; tests use `start()` and a fake
   dispatcher rather than bypassing lifecycle.
2. Snapshot failures increment `failed_decisions` with a typed category; no separate
   counter is added.
3. M3.11 application composition has exactly one runtime decision subscriber:
   `TradingDecisionCoordinator`. Legacy ShadowTradingDecisionEngine may remain only
   for compatibility/standalone use.
4. `DEGRADED` paper never stops coordinator, shared pipeline or shadow, and is not
   auto-reset.
5. Consumer `Exception` is isolated; `BaseException` propagates and further delivery
   is not guaranteed.
6. Dedup insertion is stage-aware and terminal consumer outcomes are never retried
   for a duplicate event during the process lifecycle.

Deferred to implementation/persistence milestones:

- BaseException reconstruction after an observable consumer side effect belongs to
  M3.12 persistence/reconciliation; current process terminates safely.
- Final Settings names and validation placement require M3.11 implementation review.
- Legacy wrapper retention is decided after mapping its standalone tests in M3.11.

## 23. M3.11 implementation status

Implemented immutable models/errors, coordinator lifecycle/direct processing,
reusable event/cache consistency validation, ShadowDecisionConsumer, feature-matrix
composition, deterministic paper graph and unit/boundary integration coverage.
Legacy ShadowTradingDecisionEngine remains available but is no longer constructed by
application runtime when coordinator is enabled. Persistence, internal retry, threads,
network trading and live/testnet execution remain excluded. The final safety review
passed on 2026-08-08; bounded process-local dedup remains explicitly non-durable.
