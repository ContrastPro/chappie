# Decision Runtime

## Scope

This is the canonical current description of runtime coordination after a
confirmed `CandleClosed` event. It explains when a decision is evaluated, how one
immutable result is delivered to Shadow, Paper, and (when separately composed and
ready) Testnet consumers, and how the runtime contains failures. It does not define
the meaning of `Signal`, `OrderIntent`, or `RiskDecision`; those domain contracts
are authoritative in [Trading decisions](TRADING_DECISIONS.md).

The active path is synchronous and in-process:

```text
CandleClosed
  -> TradingDecisionCoordinator
  -> committed cache snapshot and consistency check
  -> one MarketSnapshot
  -> one TradingDecisionPipeline result
  -> optional ShadowDecisionConsumer
  -> optional PaperTradingEngine
  -> optional TestnetExecutionConsumer
```

The coordinator is an application/runtime boundary, not a trading-domain service.
It does not calculate strategy indicators, translate signals to intents, evaluate
Risk, create exchange payloads, persist a fill, mutate a Portfolio, poll market
data, own credentials, or perform retries.

## Runtime Role and Ownership

`TradingDecisionCoordinator` owns only its `CandleClosed` subscription, bounded
process-local completed-result registry, lifecycle/status, and delivery ordering.
The composition root owns the dispatcher, market cache, decision pipeline,
consumers, provider, persistence adapters, and their resource lifecycle.

It starts only when at least one consumer is composed. When neither Shadow nor Paper
nor a ready Testnet consumer is present, normal market ingestion has no decision
subscriber. The legacy `ShadowTradingDecisionEngine` remains available for explicit
standalone compatibility/tests, but normal application composition must subscribe
the coordinator only; the legacy engine and coordinator must never both subscribe
to the same runtime dispatcher.

## Source Events and Shared Coordination

`process_event(event)` requires the `RUNNING` lifecycle and a valid
`CandleClosed`. The dispatcher adapter `handle_candle_closed(event)` delegates to
that same method so direct tests and subscribed operation use one implementation.

For a non-duplicate eligible event, the normative sequence is:

1. Read the defensive cache snapshot for the event stream with the configured
   history limit.
2. Check that the snapshot is non-empty; that it includes the exact closed event
   candle; that its OHLCV and close boundary agree with the immutable event; and
   that the event occurred after that candle closed.
3. Generate one coordination ID and one snapshot ID only after the cache check.
4. Create exactly one `MarketSnapshot`, preserving event and correlation identity.
5. Evaluate `TradingDecisionPipeline` exactly once and validate the resulting
   snapshot, stream, correlation, signal, and optional intent linkage.
6. Deliver the exact shared objects in order: Shadow, then Paper, then the optional
   Testnet execution consumer.
7. Build an immutable final coordination result, retain it when its delivery is
   terminal, and update status counters.

The cache may already include a later candle, but the event candle must still be in
the selected history and closed-candle decision logic cannot look ahead through a
newer open candle. A short but internally consistent history reaches the strategy as
an explainable HOLD; it is not a coordinator failure. A cache read/mismatch,
factory, pipeline, or identity failure creates no snapshot/result delivery and is
reported as a typed coordinator failure.

## Decision Identity and Correlation

One accepted source event creates at most one coordination ID, snapshot ID,
pipeline-run ID, signal ID, and optional intent ID. The coordinator verifies the
chain before delivery:

```text
source_event_id + correlation_cycle_id
  -> MarketSnapshot(snapshot_id)
  -> DecisionPipelineResult(pipeline_run_id)
  -> Signal(signal_id)
  -> OrderIntent(intent_id | None)
```

The shared result is not copied for consumers. With both Shadow and Paper enabled,
the following are identity (`is`) invariants, not merely value equality:

```text
shadow snapshot is paper snapshot
shadow decision result is paper decision result
shadow signal is paper signal
shadow intent is paper intent, when an intent exists
```

The delivered shadow record and paper cycle result are validated against the source
event, snapshot/pipeline/signal/intent IDs, stream, and correlation before the final
result is accepted. ID factories must return non-empty strings; their errors are
typed coordinator failures. All result/status mappings are frozen and durations use
an injected monotonic clock only; market time remains event/snapshot time.

## Duplicate Handling

The coordinator's duplicate key is the exact `source_event_id`. It keeps a bounded
FIFO mapping of terminal `TradingDecisionCoordinationResult` values.

- A duplicate returns the exact retained result, increments only
  `ignored_duplicates`, and does not read cache, allocate IDs, re-evaluate strategy,
  or call any consumer.
- A duplicate does not move an entry in FIFO order; capacity eviction explicitly
  removes the oldest completed event.
- The same candle with another event ID is a distinct source event.
- Failures before any delivery attempt are not retained, so a later delivery may
  retry the shared computation. After a consumer attempt, terminal `DELIVERED`,
  `FAILED`, and Paper `UNAVAILABLE` outcomes are retained and never automatically
  redelivered for that source event.

This is bounded, process-local delivery suppression—not durable execution
idempotency. Restart/recovery and one-POST guarantees belong to the active
persistence/execution boundary. A `BaseException` after an observable side effect
has no process-local replay guarantee and must not be treated as a retry signal.

## Shadow Consumer

`ShadowDecisionConsumer` observes a decision that the coordinator has already
computed. It receives `(event, snapshot, decision_result, coordination_id)` and
must not read the cache, create a snapshot, run the pipeline, subscribe to events,
or own event deduplication.

It creates an immutable `ShadowDecisionRecord` with source/correlation/snapshot/
pipeline/signal/intent identity, strategy/planner identity, outcome, and a shadow
run ID. Its observer is read-only from the decision runtime's perspective: no
observer is permitted to grant execution authority, invoke Risk, mutate Portfolio,
or use credentials. The logging observer emits compact, sanitized fields only; it
does not log candle history, arbitrary metadata, private exchange payloads, or
secrets.

An ordinary observer `Exception` preserves the semantic decision and changes the
record observation to `OBSERVATION_FAILED`; it does not cause a second decision or
block Paper. `BaseException` is not swallowed.

## Paper Consumer

Paper receives the same `DecisionPipelineResult` and `MarketSnapshot` after Shadow.
Paper owns its own Risk, durable cycle, Paper execution, fill application, and
Portfolio work; the coordinator does not duplicate that business logic.

If Paper is already `DEGRADED`, it is deliberately not called. The result records
`UNAVAILABLE`, increments the corresponding counter once for the unique decision,
and Shadow may still observe the shared decision. A successful Shadow delivery is
not undone by a Paper exception. Conversely, a Shadow failure does not block Paper.
Ordinary Paper exceptions are represented by a terminal `FAILED` delivery result;
they do not increment shared-decision computation failures and are not retried on a
duplicate event.

Paper recovery/configuration is performed before the coordinator is started. A
failed Paper recovery prevents normal Paper decision delivery rather than allowing a
partially restored Portfolio to process an event.

## Testnet Consumer and SAFE_STOP

The optional Testnet execution consumer receives the same already-validated shared
decision and snapshot. It is composed only through the Testnet runtime's separate
readiness/capability controls; the coordinator does not create Testnet authority or
bypass Risk, reconciliation, settlement, or execution gates.

An unexpected ordinary exception from that consumer is different from isolated
Shadow/Paper observation/delivery failure: it enters process-local `SAFE_STOP`.
The coordinator stores only a sanitized error category and the fixed
restart-required message, rejects `start()` in that instance, and ignores later
`CandleClosed` deliveries. No automatic submission retry occurs. A fresh
process/operator action is required before any new Testnet delivery path exists.

This containment applies only to unexpected Testnet execution-consumer failure. A
normal HOLD does not call the execution path and does not enter `SAFE_STOP`. Paper
operation remains isolated from that Testnet-specific containment behavior. LIVE is
unavailable in the MVP runtime.

## Delivery Ordering and Failure Isolation

The delivery order is deterministic:

```text
shared computation -> Shadow -> Paper -> Testnet (if separately composed)
```

`DecisionConsumerStatus` for the two recorded consumer slots is one of:

- `DISABLED`: that consumer was not configured;
- `DELIVERED`: it returned a identity-consistent result;
- `FAILED`: it was invoked and raised an ordinary `Exception`;
- `UNAVAILABLE`: configured Paper was deliberately not invoked because it was
  degraded;
- `NOT_ATTEMPTED`: shared computation did not reach a configured delivery.

The immutable result records ordered stages from event receipt through cache read,
snapshot creation, decision evaluation, consumer delivery/failure/unavailability,
and `COMPLETED`. Model validation rejects missing prerequisites, duplicate stages,
out-of-order stages, incompatible result/error combinations, and identity drift.

Ordinary Shadow and Paper failures are isolated from market ingestion. The handler
logs a safe category and returns, allowing the dispatcher to continue. A
`BaseException` is deliberately not suppressed; remaining delivery and retention are
not guaranteed. Testnet execution failure instead follows the SAFE_STOP rule above.

## Lifecycle

The coordinator lifecycle is `STOPPED`, `RUNNING`, or `SAFE_STOP`.

- `start(dispatcher)` validates the dispatcher, creates exactly one subscription,
  and transitions to `RUNNING` only after subscription succeeds.
- Calling `start` twice raises a typed state error. Calling it after `SAFE_STOP`
  raises: a fresh process is required.
- `stop()` is idempotent from `STOPPED`. From `RUNNING` or `SAFE_STOP`, it requires
  confirmed unsubscription; on unsubscribe failure it retains the subscription and
  does not claim a successful shutdown.
- The coordinator never closes the dispatcher, cache, pipeline, consumers, provider,
  persistence, or Testnet resources. The application composition root owns those.

Normal startup constructs market components, required decision consumers, recovers
Paper state where enabled, starts the coordinator subscription, and only then starts
polling. Normal shutdown stops polling/decision delivery before closing owned
resources. Bootstrap does not publish historical `CandleClosed` events; decisions
start from a new confirmed close.

## Disabled and Unavailable Paths

Shadow and Paper are independently configurable. Paper does not depend on Shadow;
when both are disabled, there is no decision coordinator. Disabled consumers have no
delivery result/error data. A Paper engine that is `DEGRADED` is `UNAVAILABLE`, not
silently reset. Testnet startup/readiness failures are surfaced as startup SAFE_STOP
before polling/submission, not as a best-effort execution mode.

No path in this runtime makes a strategy advisory result an order by itself:
strategy/planner/Risk semantics remain in [Trading decisions](TRADING_DECISIONS.md).
Shadow specifically has no execution authority, and the coordinator cannot bypass
Risk or durable execution controls.

## Observability

`TradingDecisionCoordinatorStatus` exposes lifecycle, unique handled events,
successful/HOLD/intent/planner-declined/failed computation totals; Shadow/Paper
delivery, failure, and Paper-unavailable totals; duplicate count; last identity
chain; last strategy reason/parameters/indicators; last consumer outcomes; and
sanitized last-error/SAFE_STOP information.

`ChappieTraderApplication` projects this compact status alongside market, Paper,
persistence/recovery, and Testnet readiness/reconciliation/settlement values. The
server status snapshot consumes this operational data. Status and logs are not audit
stores and must never expose credentials, signatures, private responses, full candle
history, mutable model objects, or unbounded payloads.

## Safety Boundaries

- One eligible source event is evaluated once per process-local completed-result
  entry and is shared by consumers rather than recomputed per consumer.
- Shared coordination is deterministic for the same immutable input/configuration
  and deterministic injected IDs; production UUIDs change identity, not decision
  semantics.
- Coordinator deduplication is not financial exactly-once authority; durable
  execution/persistence owns one-intent/one-POST, ambiguity, reconciliation, fill,
  settlement, and Portfolio correctness.
- Shadow is observability only. Strategy and planner remain advisory; neither has
  Risk, execution, persistence, credential, or Portfolio authority.
- No ordinary delivery failure creates an internal retry. An unexpected Testnet
  execution failure tightens this further to SAFE_STOP for the process.

## Tests and Verification

Primary executable coverage is in `tests/test_trading_coordination.py` and
`tests/test_application.py`, with legacy standalone Shadow behavior covered in
`tests/test_shadow_engine.py`, `tests/test_shadow_models.py`, and
`tests/test_shadow_observer.py`. The suite verifies:

- start/stop guards, confirmed unsubscribe behavior, and feature combinations;
- one cache/snapshot/pipeline evaluation and exact object identity across consumers;
- event/cache mismatches, typed failures, ID failure handling, and stage validation;
- bounded FIFO duplicate handling and no redelivery after terminal partial delivery;
- Shadow failure isolation, Paper failure/degraded behavior, and `BaseException`
  propagation;
- Testnet SAFE_STOP, no later consumer/submission action, and fresh-process-only
  restart semantics;
- Paper recovery before coordinator start plus compact application status counters.

## Historical Notes

The predecessor documents described M2.5/M2.6 standalone Shadow design and
M3.10/M3.11 shared coordination design. Their useful reasoning is preserved below.
Several proposals are historical only: EMA-only composition, both features disabled
by default, the old no-persistence/restart claim, future smoke plans, and the former
assertion that all consumer exceptions merely allow continued ingestion. The current
MVP supports strategy selection, durable Paper recovery, and Testnet SAFE_STOP; it
does not reinstate historical Stage 4 recovery as part of this runtime contract.

## Source Invariant Crosswalk

| Source file | Source section / unique invariant | Destination | Status |
| --- | --- | --- | --- |
| `SHARED_DECISION_RUNTIME_DESIGN.md` | Status/scope; one confirmed close creates one shared decision | Scope; Source events | PRESERVED |
| Same | Coordinator is application ownership, not strategy/Risk/execution/Portfolio | Runtime role | PRESERVED |
| Same | Public constructor/dependency validation and direct subscribed API | Runtime role; Lifecycle | MERGED |
| Same | One subscription; start/stop ownership and confirmed unsubscribe | Lifecycle | PRESERVED |
| Same | Normative event/cache/snapshot/pipeline/delivery sequence | Shared coordination | PRESERVED |
| Same | Defensive cache, exact event candle, no look-ahead, preserved correlation | Shared coordination | PRESERVED |
| Same | One IDs chain and exact object identity for shared consumers | Decision identity | PRESERVED |
| Same | Shadow/Paper independent feature matrix | Disabled and unavailable paths | PRESERVED |
| Same | Shadow migration to consumer; no concurrent legacy subscriber | Runtime role; Shadow consumer | PRESERVED |
| Same | Shadow-before-Paper ordering and ordinary consumer isolation | Delivery ordering | PRESERVED |
| Same | Degraded Paper is deliberately not called and is not auto-reset | Paper consumer | PRESERVED |
| Same | FIFO dedup, stage-aware insertion, no duplicate consumer retry | Duplicate handling | PRESERVED |
| Same | Immutable result, terminal statuses, ordered stage validation | Delivery ordering | PRESERVED |
| Same | Counter/last-result meanings and monotonic duration rules | Observability; Decision identity | PRESERVED |
| Same | Typed errors, bounded public errors, BaseException propagation | Delivery ordering | PRESERVED |
| Same | Safe structured logging, no secrets/history, logs not audit | Observability | PRESERVED |
| Same | EMA-only/old flags/future composition proposal | Historical notes | SUPERSEDED |
| Same | No journal/restart recovery and process-local Portfolio claim | Safety boundaries; Historical notes | SUPERSEDED |
| Same | M3.15 durable replay result | Historical notes and persistence boundary cross-link | HISTORICAL_ONLY |
| `SHADOW_DECISION_ENGINE_DESIGN.md` | Shadow scope: observation only, no Risk/execution/Portfolio/private API | Shadow consumer; Safety boundaries | PRESERVED |
| Same | Legacy engine ownership and standalone lifecycle | Runtime role; Historical notes | MERGED |
| Same | `MarketCacheReader` structural read port and bounded defensive history | Shared coordination | PRESERVED |
| Same | Event close/cache equality and short-history HOLD behavior | Shared coordination | PRESERVED |
| Same | Immutable shadow record and source-to-intent audit chain | Shadow consumer; Decision identity | PRESERVED |
| Same | Observer protocol, sanitized compact logging, HOLD observability | Shadow consumer; Observability | PRESERVED |
| Same | Observer exception isolation; BaseException propagation | Shadow consumer; Delivery ordering | PRESERVED |
| Same | Original engine reserves event IDs before cache read | Historical notes | SUPERSEDED |
| Same | Original engine status/counters/durations | Observability | MERGED |
| Same | Single-thread synchronous operation; bootstrap has no historical replay | Lifecycle | PRESERVED |
| Same | Original EMA/default-false configuration proposal | Historical notes | SUPERSEDED |
| Same | M2.6 deterministic test plan and boundary checks | Tests and verification | MERGED |
| Same | Open questions/implementation plan | Historical notes | HISTORICAL_ONLY |

The two source files are retired as root canonical documents. Their historical
milestone context is retained here and in the repository's historical documentation;
the active contract is this document plus the linked market, trading-decision,
Paper, Risk, persistence, and execution boundaries.
