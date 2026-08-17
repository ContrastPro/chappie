# Paper Trading

## Scope

This is the canonical current contract for Chappie's simulated Paper trading
runtime: deterministic execution, immutable fills, authoritative local Portfolio
accounting, durable Paper cycles, recovery, and safe observation. It describes
financial effects precisely; it does not define market ingestion, signal/intent
semantics, mandatory Risk policy, or Testnet exchange lifecycle.

Those boundaries are authoritative in [Market system](MARKET_SYSTEM.md),
[Trading decisions](TRADING_DECISIONS.md), [Decision runtime](DECISION_RUNTIME.md),
and [Persistence](PERSISTENCE.md) and [Exchange execution](EXCHANGE_EXECUTION.md).

Paper is a simulation. It creates no exchange order, makes no private exchange call,
and requires no trading credential to execute its simulated fills. It must not be
confused with Testnet or LIVE.

## Safety Boundary

The Paper execution adapter accepts only a complete immutable `RiskDecision`.
Strategy, planner, Shadow, monitor, and observer code cannot call it with an
unapproved intent or mutate Paper financial state.

`PaperExecutionProvider` is deterministic, stateless, immediate-fill, and local. It
uses injected `InstrumentRules`, a configured fee rate, and injected ID factories;
it has no network, cache, provider, credential, clock, persistence, Portfolio, or
real-order dependency. It never resizes an approved order, adds slippage, partially
fills, retries, or applies a fill to Portfolio.

`Portfolio` is the single-stream, single-writer authority for Paper balances,
position, cost basis, realised and unrealised PnL, fees, turnover, drawdown, and
trade/fill counters. It performs no I/O or market decision. `PaperTradingEngine`
coordinates existing components; it does not contain strategy, Risk, execution-fee,
or accounting formulas.

All financial values are finite `Decimal` values. Quantity, price, gross notional,
and fee use versioned `InstrumentRules` and local Decimal precision independent of
the process global Decimal context. No code silently replaces invalid financial input
with a no-op result.

## Runtime Composition

Normal Paper runtime composition is:

```text
market polling / committed CandleClosed
  -> TradingDecisionCoordinator (one shared decision)
  -> DecisionPipelineResult
  -> PaperTradingEngine
  -> Portfolio valuation at latest closed price
  -> RiskManager (when there is an intent)
  -> PaperExecutionProvider (only APPROVED or MODIFIED)
  -> immutable Fill
  -> durable Paper-cycle commit
  -> in-memory Portfolio synchronization
  -> PaperTradingCycleResult, observer, status/monitor
```

The coordinator owns upstream `CandleClosed` delivery and its bounded event-result
deduplication. It supplies the same immutable snapshot/result to Shadow and Paper;
Paper does not recalculate strategy or planner output. Paper is independently
configurable and does not require Shadow.

During active application Paper composition, Portfolio initialization/recovery and
configuration validation occur before the coordinator starts consuming decisions.
The runtime then creates one Portfolio, matching InstrumentRules/Risk/Execution
components, one Paper engine with a durable cycle store, and one observer. The
composition root—not the engine—owns their lifetime and closes resources.

## Paper Execution Contract

An executable `RiskDecision` is `APPROVED` or `MODIFIED` and must contain approved
side, quantity, reference price, gross notional, maximum fee rate, rule version,
Portfolio version, stream, intent, and correlation linkage. The provider rejects
misaligned quantity/price, stream/rule drift, missing approved parameters, fee rate
outside the Risk envelope, and arithmetic inconsistencies.

For an executable decision, the provider creates one immediate complete `Fill`:

- `order_id` equals the generated `execution_id` in this immediate-fill model;
- `fill_id` is distinct from `execution_id`;
- side and quantity exactly equal the Risk-approved values;
- price exactly equals the approved reference price; slippage is zero;
- `filled_at_ms = decision.decided_at_ms + configured_latency_ms` with no sleep or
  wall-clock read;
- metadata records only safe provider/rule/fee/latency facts.

A structurally valid `REJECTED` Risk decision produces `NOT_EXECUTED` with no Fill
and the complete ordered rejection-reason tuple. It is a defensive execution result,
not an exchange rejection. Programming, factory, configuration, rule, or arithmetic
defects raise typed exceptions rather than returning a fake failed Fill.

The current provider does not model exchange order states, partial fills, spread,
liquidity, cancellation, timeout, exchange rejection, maker/taker fees, nonzero
slippage, or real transport.

## Fill Contract

`Fill` is the Paper fill model; there is no parallel `PaperFill`. It binds:

```text
fill_id
  -> order_id / execution_id
  -> risk_decision_id
  -> intent_id
  -> stream_key, side, quantity, price
  -> gross_notional, quote fee, net quote/base changes
  -> logical fill time and correlation
```

The fee asset must equal the Portfolio quote asset. Portfolio revalidates stream,
quantity step, price tick, minimum quantity/notional (except the explicit complete
paper-only dust-liquidation case), gross/fee quote precision, and the exact net
balance deltas. It accepts already-rounded external quote values and does not round
them again.

## Financial Invariants

Let `q` be approved quantity, `p` approved reference price, `g` the quote-rounded
`q × p`, and `f` the quote-rounded `g × fee_rate`. `quote_round` and all alignment
rules are defined by the versioned `InstrumentRules`.

| Operation | Quantity / price | Gross and fee | Quote / base delta | Cost basis and PnL | Counters |
| --- | --- | --- | --- | --- | --- |
| BUY / `FILLED` | `q` and `p` are Risk-approved and aligned; full immediate fill | `g = quote_round(q × p)`; `f = quote_round(g × fee_rate)` | quote decreases by `g + f`; base increases by `q` | add `g + f` to authoritative position cost basis; new average entry is new cost basis ÷ new base quantity; realised PnL unchanged | turnover increases by `g`; total fees increases by `f`; trade count and applied-fill count each increase by 1 |
| SELL / `FILLED` | `q` cannot exceed position/base balance; full or valid partial exit | same `g` and `f` calculation | quote increases by `g - f`; base decreases by `q` | remove authoritative cost: full exit removes the complete remaining cost basis, partial exit removes `average_entry_price × q`; realised increment is `g - f - removed_cost` | turnover increases by `g`; total fees increases by `f`; trade count and applied-fill count each increase by 1 |
| HOLD / `NO_INTENT` | no order quantity or execution price | no gross and no fee | no balance delta | no cost-basis or realised-PnL effect; mark-to-market may change unrealised PnL/equity/drawdown | no trade/fill/turnover/fee increment |
| Planner declined | no executable intent | no gross and no fee | no balance delta | same non-trade valuation behavior as HOLD, but distinct outcome | no trade/fill/turnover/fee increment |
| Risk rejected / `NOT_EXECUTED` | no applied fill | no gross and no fee | no balance delta | no cost-basis or realised-PnL effect; valuation remains committed | no trade/fill/turnover/fee increment |

There is no fee deduction from purchased base quantity. Fees are quote-currency
execution facts: a BUY acquires the full approved base quantity while paying gross
plus quote fee; a SELL receives net quote proceeds after quote fee. Risk sizes and
approves an envelope; the execution provider charges the configured fee only if it is
within that envelope; Portfolio verifies and accounts for the resulting Fill.

## Portfolio Authority and Valuation

`PortfolioSnapshot` is the immutable public view of the authoritative aggregate. It
contains portfolio/stream/assets identity, monotonic version/as-of time, base and
quote balances, optional long position, cumulative realised/unrealised PnL, total
fees, initial/current/peak equity, current/max drawdown, turnover, trade/fill counts,
and correlation.

Portfolio construction requires zero initial base balance in the current model; the
initial quote balance is configured by application composition, rather than being an
unconditional `10,000` constant. It is the initial equity. A position exists exactly
when base balance is positive, and its quantity must equal base balance.

For a mark price `m`:

```text
base_value       = base_balance × m
current_equity   = quote_balance + base_value
unrealised_pnl   = position.quantity × m - position.cost_basis
drawdown         = peak_equity - current_equity
```

`peak_equity` never falls; `max_drawdown` is the greatest observed absolute quote
drawdown. Unrealised PnL does not deduct a hypothetical future SELL fee. A mark uses
the latest closed candle price and can advance Portfolio version/time without a fill;
it does not create realised PnL, turnover, fees, or trade count.

`average_entry_price` is derived from authoritative `cost_basis / quantity` under
deterministic local Decimal precision and is not price-tick rounded. Reconciliation
uses cost basis exactly, including a possible finite Decimal division residual. A
full SELL removes the complete remaining cost basis, preserving this exactness.

Only `Portfolio` may mutate financial state. Paper engine may request local mutation
in explicit non-durable/offline use or prepare pure transition previews in durable
application use. Risk sees a valued immutable snapshot. Execution creates a Fill.
Observers, status reporting, and `paper_monitor` are read-only consumers.

## Identity and Exact-Once

The relevant identity chain is:

```text
source event -> shared pipeline result -> intent
  -> Risk decision -> execution/order -> fill -> Portfolio transition -> durable command
```

Different layers protect different scopes:

- the decision runtime suppresses duplicate source events in its bounded process-local
  completed-result registry;
- the Paper engine suppresses a completed in-memory cycle by intent ID for executable
  cycles or pipeline-run ID for HOLD/planner-declined cycles;
- Portfolio treats an identical retained `fill_id` as a no-op and rejects a
  conflicting payload for the same ID;
- active durable Paper runtime derives stable decision/slot/command keys and records
  the terminal cycle, execution/Fill facts, and Portfolio transition atomically.

An in-memory registry is bounded and resets with a process. It is not financial
exactly-once authority. In durable runtime a terminal duplicate returns
`DURABLE_REPLAY` without running Risk or execution and without a second financial
mutation. A conflicting durable identity/recovery condition fails closed; it is never
repaired by re-executing a fill.

## Persistence Boundary

Persistence is a separate [canonical boundary](PERSISTENCE.md); this document records
only Paper's interaction with it. In active durable Paper application mode, valuation and any Fill
application are first calculated as pure Portfolio transitions. The durable cycle
store commits the terminal decision/cycle, Risk/execution facts, and transition(s)
before the in-memory aggregate synchronizes through
`apply_committed_transitions()`.

Thus a commit failure leaves in-memory Portfolio unchanged. A synchronization failure
after durable commit degrades the engine and requires restart recovery; it must not
trigger financial replay. Observer/checkpoint/shutdown failure likewise cannot request
another execution. The journal/checkpoint schemas, corruption handling, locking, and
storage-level recovery algorithm belong to the canonical Persistence document.

## Recovery and Restart

At application startup, Paper configuration is fingerprinted and the persisted
initial/current state is checked against the expected Portfolio/stream configuration.
The runtime recovers the validated Portfolio state before it creates a coordinator
subscription. A recovery-required or configuration-mismatched state blocks Paper
runtime startup instead of starting with a partial or invented Portfolio.

The restored aggregate preserves balances, open position/cost basis, cumulative PnL,
fees, turnover, versions, and reconciliation state. Recent fills and durable terminal
identities prevent a restart from treating an already committed command as a new
execution. A recovered terminal cycle is represented as `DURABLE_REPLAY`, not as a
new Fill, Risk decision, or execution attempt.

The legacy pre-durable engine was intentionally process-local and could not safely
recover an ambiguous Fill-to-Portfolio interval. That limitation is historical; it is
not a license to retry in the current durable application runtime.

## Failure Semantics

| Situation | Current Paper behavior |
| --- | --- |
| HOLD | Value Portfolio; return `NO_INTENT`; do not call Risk or execution. |
| Planner declined | Value Portfolio; return `PLANNER_DECLINED`; do not call Risk or execution. |
| Risk rejected | Value Portfolio; return `RISK_REJECTED`; do not call execution. |
| Provider returns valid rejected/no-fill result | Return `NOT_EXECUTED`; do not apply a Fill. |
| Paper execution exception or invalid execution result | Typed Paper execution failure; no fill application/retry. |
| Portfolio valuation failure | Typed failure before Risk/execution. |
| Portfolio transition failure after a Fill exists | Degrade engine; report safe identity context; never blind-retry. |
| Durable commit failure | Leave in-memory Portfolio unchanged; degrade engine; fail closed. |
| In-memory synchronization failure after durable commit | Degrade engine; restart/recovery is required; do not replay finance. |
| Ordinary observer failure | Preserve committed business result; record failed observation; no rerun. |
| Observer `BaseException` after commit | Do not suppress; preserve safety by degradation/recovery path. |
| Duplicate source/cycle/fill | Return retained result/no-op only for matching identity; conflicting identity is an integrity error. |

`PaperTradingEngine` is synchronous and single-writer. It rejects re-entrant calls;
its activity always returns to `IDLE`. `READY` allows a later ordinary call;
`DEGRADED` rejects additional processing until a safe reconstruction/restart path is
used. No failure category is an automatic retry instruction.

## Paper, Testnet, and LIVE

| Mode | Execution authority | Financial authority | Operational boundary |
| --- | --- | --- | --- |
| Paper | deterministic local immediate Fill only; no exchange order | local Portfolio plus durable Paper persistence | no execution credential or private endpoint required |
| Testnet | separate exchange execution lifecycle | reconciliation, settlement, exchange facts, and Testnet controls | readiness and unexpected execution failure use SAFE_STOP; see future Exchange Execution canonical doc |
| LIVE | unavailable | none | no MVP runtime path |

Paper market observation may use a market-data provider, but Paper *execution* does
not call the network and must not inherit Testnet credentials. Testnet cannot be
selected by a Paper flag, and Paper cannot simulate its exchange order/reconciliation
states.

## Observability

The Paper cycle result and compact `PaperTradingRecord` provide stable IDs, outcome,
ordered Risk reasons, execution/fill fields, post-cycle balances/position, realised
and unrealised PnL, equity, drawdown, and safe failure category. Engine status tracks
handled and business-outcome counters, failures, observation failures, duplicates,
last cycle/intent/fill IDs, last Risk/execution state, Portfolio version, and safe
diagnostics.

Application status projects compact Paper Portfolio metrics and persistence/recovery
health. The read-only Paper monitor uses the sanitized status snapshot and its own
read-only persisted state access; it creates no order, Fill, Portfolio mutation, or
SQLite write. Logs and status never contain credentials, private exchange responses,
full mutable models, or unbounded candle payloads.

## Tests and Verification

The current contracts are exercised by `tests/test_paper_execution.py`,
`tests/test_portfolio.py`, `tests/test_paper_engine.py`, `tests/test_persistence.py`,
`tests/test_application.py`, `tests/test_trading_coordination.py`, and
`tests/test_paper_monitor.py`. Coverage includes:

- exact BUY/SELL gross, quote-fee, base/quote delta, and Decimal-rounding behavior;
- weighted average cost, partial/full exits, realised/unrealised PnL, fees, turnover,
  equity, drawdown, and fill idempotency/conflict rejection;
- HOLD/decline/rejection/no-fill behavior, stage order, observer isolation, and
  re-entrant/degraded engine handling;
- one shared decision across consumers, duplicate suppression, and Paper-unavailable
  coordinator behavior;
- atomic durable commit/restart replay, open-position recovery, checkpoint recovery,
  corruption/config mismatch fail-closed behavior, and no second Fill after restart;
- read-only monitor/status behavior and no Paper execution network dependency.

## Historical Notes and Code/Document Conflicts

| Old claim | Current implementation/evidence | Disposition |
| --- | --- | --- |
| Early core design treated `USDT=10000` as the fixed first state. | `PortfolioConfig` requires zero initial base but accepts configured initial quote balance; application supplies the configured value. | SUPERSEDED |
| Early configuration proposed `PAPER_SLIPPAGE_BPS=5`. | `PaperExecutionConfig` has fee rate and fixed latency; execution price is the approved reference price with zero slippage. | SUPERSEDED |
| Early core/orchestration documents described process-local fill/cycle dedup and no restart restoration. | Active application uses durable Paper cycles, atomic committed transitions, startup recovery before coordinator subscription, and `DURABLE_REPLAY`. | SUPERSEDED |
| Early orchestration called durable persistence future scope and listed only five cycle outcomes. | Current model includes durable store integration and `DURABLE_REPLAY`. | SUPERSEDED |
| Provider design ordered full decision validation before allocating an execution ID. | `PaperExecutionProvider.execute()` allocates an execution ID immediately after RiskDecision type validation, then validates executable structure/arithmetic before requesting a fill ID. | HISTORICAL_ONLY; current code wins |
| Older documents said Paper runtime/wiring remained future scope. | `build_application()` composes Paper persistence, Portfolio, Risk, execution, engine, coordinator, status, and recovery today. | SUPERSEDED |
| Historical M3.x milestone/smoke plans and future extension lists. | Retained as historical rationale only; they do not alter current Paper financial semantics. | HISTORICAL_ONLY |

## Source Invariant Crosswalk

| Source file | Source section / unique invariant | Destination | Status |
| --- | --- | --- | --- |
| `PAPER_EXECUTION_PROVIDER_DESIGN.md` | Provider creates immutable execution fact only; Portfolio owns accounting | Safety boundary; Portfolio authority | PRESERVED |
| Same | APPROVED/MODIFIED execute; REJECTED yields defensive no-fill | Paper execution; Failure semantics | PRESERVED |
| Same | Exact Fill identity/fields; order ID equals execution ID | Paper execution; Fill contract | PRESERVED |
| Same | Quote-rounded gross/fee and BUY/SELL delta formulas | Financial invariants | PRESERVED |
| Same | Decision-derived logical time, fixed latency, no clock/sleep | Paper execution | PRESERVED |
| Same | Stateless provider does not give global dedup | Identity and exact-once | PRESERVED |
| Same | Typed validation/factory/arithmetic failures and no simulated exchange states | Paper execution; Failure semantics | PRESERVED |
| Same | No network/Portfolio mutation/persistence in provider | Scope; Safety boundary | PRESERVED |
| Same | Partial fills, slippage, order lifecycle and exchange behavior require distinct design | Paper execution | HISTORICAL_ONLY |
| `PAPER_TRADING_CORE_DESIGN.md` | Risk gate, single Portfolio authority, finite Decimal inputs | Safety boundary; Portfolio authority | PRESERVED |
| Same | Position/snapshot identity, valuation, exact cost basis/average and BUY/SELL accounting | Financial invariants; Portfolio authority | PRESERVED |
| Same | Quote-owned fee and no hypothetical exit fee in unrealised PnL | Financial invariants; Portfolio authority | PRESERVED |
| Same | Instrument rules align/round external facts; Portfolio never re-rounds | Fill contract | PRESERVED |
| Same | Dust full SELL exception remains narrow and validated | Fill contract | PRESERVED |
| Same | Target-exposure/Risk sizing formulas and policy limits | Trading decisions / future Risk doc | MERGED by cross-link |
| Same | Early stable paper idempotency key and non-durable transaction gap | Identity and exact-once; Historical notes | SUPERSEDED |
| Same | Performance metrics identity and non-financial ownership | Portfolio authority; Observability | PRESERVED |
| Same | Future paper events/schema/module/config proposals | Historical notes | HISTORICAL_ONLY |
| Same | Initial fixed 10,000 quote/default slippage proposal | Historical notes and conflicts | SUPERSEDED |
| `PAPER_TRADING_ORCHESTRATION_DESIGN.md` | Engine is synchronous coordinator, no Strategy/Risk/accounting formulas | Runtime composition; Safety boundary | PRESERVED |
| Same | Single-writer process/activity/health and dependency ownership | Failure semantics | PRESERVED |
| Same | Validate -> dedup -> value -> Risk -> execute -> fill -> observer sequence | Runtime composition | PRESERVED |
| Same | Valuation every new cycle, including non-trades; duplicate before valuation | Portfolio authority; Failure semantics | PRESERVED |
| Same | HOLD, planner decline, Risk reject, no-fill and executed outcomes/stages | Failure semantics | PRESERVED |
| Same | Cross-model identity validation and no mismatch repair | Identity and exact-once; Failure semantics | PRESERVED |
| Same | Observer records compact facts; observer failure cannot rerun business work | Observability; Failure semantics | PRESERVED |
| Same | In-memory dedup FIFO and no blind retry | Identity and exact-once | MERGED |
| Same | Status/counter/duration rules and safe logging | Observability | PRESERVED |
| Same | M3.9 no-transaction/no-restart gap | Historical notes and conflicts | SUPERSEDED |
| Same | Shared coordinator/Paper-versus-Shadow delivery ordering | Runtime composition; Decision runtime cross-link | PRESERVED |
| Same | Durable Paper store, commit-before-memory-sync, terminal replay | Persistence boundary; Recovery and restart | PRESERVED |
| Same | M3.15 crash boundary and milestone evidence | Historical notes | HISTORICAL_ONLY |

The three source documents are retired as root canonical documents. Their historical
milestone reasoning is retained in the crosswalk and repository historical evidence;
current Paper accounting and recovery behavior is defined here.
