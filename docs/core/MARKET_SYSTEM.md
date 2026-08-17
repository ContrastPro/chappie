# Market system

## Scope and authority

This is the canonical current contract for the in-process market cache,
synchronous REST polling, and market-event layer. It consolidates the former
Market Cache, Market Events, and Polling Engine design documents. Historical
implementation proposals are retained only where they explain a still-active
contract; this document describes implemented behavior verified by the current
`app.market` tests.

## Current contract

The active runtime polls completed Binance market candles through the market
provider port, validates them, stores them in the in-memory cache, and publishes
in-process market events. The cache is the process-local source of market
observations; trading decisions use confirmed closed candles only.

- Candle identity is symbol, interval, and exchange time; duplicates do not
  create a second decision.
- Open candles are observable but cannot drive a trading decision. A candle
  becomes decision-eligible only after confirmed closure.
- Exchange timestamps, not local wall-clock time, order candles and determine
  gap/duplicate checks. Clock skew is observed and reported.
- Polling validates chronology, expected interval cadence, and candle shape.
  A gap is evidence, not data to interpolate.
- Publication is synchronous and in process. Observer failure is recorded and
  isolated from already committed business work.
- Transient polling/provider failures follow the polling lifecycle policy; an
  unsafe Testnet execution failure is handled by application SAFE_STOP.

The market loop owns polling and provider shutdown. It does not own strategy,
risk, execution, or financial persistence.

## Cache contract

`InMemoryMarketCache` is bounded, per `MarketStreamKey(symbol, interval)`, and
uses a default capacity of 1,000 candles per stream. It is deliberately
single-writer, synchronous, non-thread-safe, in-memory, and I/O-free.

- Inputs are validated `Candle` values plus explicit `observed_at_ms`; cache
  behavior never reads hidden system time.
- A batch is staged and committed atomically. Validation/upsert failure leaves
  the prior cache unchanged.
- Streams are strictly ordered, have no duplicate open times or overlapping
  ranges, and do not silently accept gaps or historical middle insertion.
- Exact duplicates are idempotent. Only the newest unconfirmed open candle may
  be refreshed; conflicting closed-candle or out-of-order updates fail.
- Closure confirmation is explicit cache lifecycle metadata. A candle is not
  made immutable solely by wall time until the cache commits its observed state.
- Capacity eviction removes the oldest candle only from its own stream.
- Reads return defensive immutable-equivalent copies; absent streams return an
  empty snapshot/latest `None`, while invalid read limits are errors.
- The detailed batch result exposes ordered committed mutation facts, including
  prior/current snapshots, evictions, data change, and closure confirmation.

## Polling contract

`SynchronousRestPollingEngine` owns one stream and is injected with its
provider, cache, policy, clock, sleeper, logger, and optional event components.
It does not construct or close its provider; the composition root owns provider
shutdown.

- Lifecycle is `STOPPED → STARTING → RUNNING`, with `BACKING_OFF`, `STOPPING`,
  and terminal `FAILED` states. Status is immutable and sanitized.
- Bootstrap obtains a bounded validated window; regular polling reconciles the
  newest candles rather than assuming an update is new data.
- A single exchange-time observation is used for cache/validation decisions;
  local-versus-exchange skew is recorded for observability.
- Cadence uses monotonic deadlines, skips missed deadlines rather than bursting,
  and separates monotonic scheduling from wall/exchange timestamps.
- Stop is idempotent and interrupts waits. An in-flight synchronous request is
  bounded by provider timeout; no later request begins after stop.
- Provider failures may use bounded retry/backoff. Validation and cache
  integrity failures are not retried with the same payload.
- Gaps are never synthesized. Recovery uses a bounded validated window; an
  incompatible history fails rather than silently resetting cache state.
- Cycle results distinguish bootstrap/recovery, fetched/mutation counts,
  attempts, timing, exchange-time/skew, publication, and delivery degradation.

## Event contract

The cache records committed facts; `MarketEventProjector` converts those facts
after commit into immutable snapshots and ordered events. Events are not a
replacement for cache state and cannot reconstruct missed history.

- Cache-change events: inserted, updated, closed, and evicted.
- Diagnostic events: gap detected and stream failure; they describe ingestion
  problems and never pretend to be cache mutations.
- Event envelopes contain factory-provided event/correlation IDs, explicit
  occurrence time, stream identity, source, and typed reason. They do not read
  hidden time or generate hidden random IDs.
- Exact duplicates produce no mutation event. Closure uses a per-stream
  watermark; event projection is staged so projection failure does not advance
  its internal state.
- The dispatcher is synchronous, single-threaded, and resource-free. It
  snapshots subscriptions, preserves subscription order, makes unsubscribe
  idempotent, and rejects recursive publish.
- Handler failure is collect-and-continue: later handlers still receive the
  event, committed cache state is never rolled back, and an immutable delivery
  result reports ordered failures. Delivery failure does not retry ingestion.

## Crosswalk of preserved invariants

| Former source | Preserved current invariants |
|---|---|
| `MARKET_CACHE_DESIGN.md` | bounded per-stream cache; explicit observation time; atomic batch; open/closed lifecycle; idempotent duplicate/update rules; no gaps/out-of-order/middle insert; defensive reads; single-writer/no persistence |
| `MARKET_EVENTS_DESIGN.md` | post-commit projection; immutable envelope/snapshots; mutation versus diagnostic events; closure watermark; deterministic subscription ordering; subscription snapshot; recursive-publish prohibition; collect-and-continue delivery |
| `POLLING_ENGINE_DESIGN.md` | injected dependencies; lifecycle/status; bootstrap/regular/recovery distinction; exchange time and monotonic cadence; bounded retry/backoff; no synthetic gap repair; stop/resource ownership; deterministic fake-clock/provider testing |

## Safety boundary

Market ingestion supplies observations only. It cannot create orders, access
financial state, choose strategy behavior, bypass Risk, or perform execution.
Unexpected Testnet execution-consumer failure is an application-level SAFE_STOP
concern after market-event delivery, not a reason for cache/polling to invent or
retry data.
