# Deterministic Paper Execution Provider

## 1. Status and scope

- Status: **M3.5 design and M3.6 implementation completed; final review passed**.
- Date: 2026-08-05.
- Implementation milestone: M3.6.

The first execution adapter converts an executable `RiskDecision` into the existing
immutable `Fill` without network, exchange API, sleep, random values, persistence or
Portfolio mutation.

```text
RiskDecision
    -> PaperExecutionProvider
    -> PaperExecutionResult(fill: Fill)
    -> application orchestration
    -> Portfolio.apply_fill(fill, mark_price)
```

The provider creates an execution fact. Portfolio remains the only owner of balances,
positions, cost basis and realised PnL.

## 2. Responsibility boundary

The provider:

- accepts only an immutable `RiskDecision`;
- executes both `APPROVED` and `MODIFIED` decisions because both contain parameters
  approved by Risk;
- defensively does not execute `REJECTED` decisions;
- copies the approved side, quantity and reference price without resizing;
- applies zero slippage and an explicitly configured fee rate;
- rounds external quote values through immutable `InstrumentRules`;
- creates immutable `Fill` and `PaperExecutionResult` values.

It does not know Portfolio, Market Cache, MarketProvider, Binance, polling, market
events, Strategy, Shadow, Risk Policy or Risk Manager. It never submits a real order
and never calls `Portfolio.apply_fill()`.

## 3. Recommended public API

```text
PaperExecutionProvider(
    config: PaperExecutionConfig,
    instrument_rules: InstrumentRules,
    execution_id_factory: ExecutionIdFactory,
    fill_id_factory: FillIdFactory,
)

execute(decision: RiskDecision) -> PaperExecutionResult
```

`PaperExecutionConfig` is immutable and contains:

- `provider_name = "deterministic_paper"`;
- `provider_version`;
- `fee_rate` as finite Decimal in `[0, 1)`;
- `latency_ms = 0` as a non-negative integer.

`RiskDecision` additively carries `stream_key` and is the self-contained authorization
artifact. `InstrumentRules` remains required for quote asset, precision and explicit
rounding. The provider verifies stream equality, approved order alignment and rules
version equality with `sizing_audit.instrument_rules_version`. This is neutral domain
configuration, not Binance knowledge.

No clock belongs in the first constructor. Execution time is derived from the decision
as described below. A future clock is allowed only by a separate ADR if execution time
must model an external event.

## 4. Existing Fill is the PaperFill model

Do not introduce a parallel `PaperFill`. Reuse `app.execution.models.Fill`:

```text
fill_id
order_id              = execution_id
risk_decision_id
intent_id
stream_key
side                  = BUY | SELL
quantity              = approved quantity
price                 = approved reference price
gross_notional
fee
fee_asset             = InstrumentRules.quote_asset
net_quote_change
net_base_change
filled_at_ms
correlation_id
immutable metadata
```

All financial values are finite Decimal. `order_id` carries `execution_id` in the
minimal immediate-fill model, avoiding a third identity with no lifecycle consumer.
Metadata contains only safe immutable scalars such as provider/version, rules version,
configured fee rate and `slippage_bps=0`.

## 5. PaperExecutionResult

Recommended immutable result:

```text
PaperExecutionResult(
    execution_id: str,
    source_decision_id: str,
    intent_id: str,
    market_snapshot_id: str,
    portfolio_id: str,
    portfolio_version: int,
    correlation_id: str | None,
    started_at_ms: int,
    completed_at_ms: int,
    latency_ms: int,
    status: PaperExecutionStatus,
    fill: Fill | None,
    reason: PaperExecutionReason | None,
    metadata: ImmutableMapping,
)
```

Minimal statuses:

- `FILLED`: an executable decision produced exactly one complete fill;
- `NOT_EXECUTED`: a structurally valid `REJECTED` RiskDecision produced no fill.

`FAILED` is not returned for programming or dependency defects: those raise typed
exceptions. Paper execution has no exchange rejection, timeout or partial-fill
business states in the first version. `NOT_EXECUTED` uses the typed reason
`DECISION_REJECTED` and exists as a defensive, auditable boundary result.

Result invariants tie status, fill and reason together. All IDs and versions are copied
from the decision, metadata is deeply immutable, and a filled result requires
`fill.risk_decision_id == source_decision_id` and `fill.order_id == execution_id`.

## 6. Deterministic execution algorithm

For `APPROVED` or `MODIFIED`:

1. Validate the enriched RiskDecision and approved order.
2. Validate rules version, side, quantity and price alignment.
3. Obtain and validate `execution_id` for the auditable attempt.
4. Set `price = approved_order.reference_price`; slippage is exactly zero.
5. Set `quantity = approved_order.quantity`; never resize or partially fill.
6. Validate cross-model fields and calculate rounded external quote values.
7. Obtain `fill_id` only after validation/arithmetic succeeds.
8. Build one immutable Fill, then one immutable FILLED result.

Quote calculations:

```text
gross_notional = instrument_rules.round_quote(quantity * price)
fee            = instrument_rules.round_quote(gross_notional * config.fee_rate)

BUY:
    net_quote_change = -(gross_notional + fee)
    net_base_change  = quantity

SELL:
    net_quote_change = gross_notional - fee
    net_base_change  = -quantity
```

Calculations use fixed local Decimal precision and never float. Rounding mode and quote
precision are explicit in versioned `InstrumentRules`; Portfolio accepts these external
values and does not re-round them. Execution must reject by typed exception if its fee
rate exceeds the Risk-approved `max_fee_rate` or if the produced BUY quote debit exceeds
`approved_order.required_quote`.

For `REJECTED`, return `NOT_EXECUTED` with no Fill. The result preserves the complete
ordered `risk_rejection_reasons`; it never chooses a primary reason. No fill identity
is requested. A malformed decision is a typed exception, not `NOT_EXECUTED`.

## 7. Clock and latency policy

Use decision-derived logical time:

```text
started_at_ms   = decision.decided_at_ms
latency_ms      = config.latency_ms
completed_at_ms = started_at_ms + latency_ms
fill.filled_at_ms = completed_at_ms
```

Default latency is zero. There is no sleep and no monotonic or wall-clock read.
Exchange time already entered the pipeline through the market snapshot and decision.
This makes replay, tests and repeated execution semantically reproducible. Monotonic
time remains suitable only for future runtime duration metrics, never fill chronology.

## 8. Factories, atomicity and idempotency

`FillIdFactory` and `ExecutionIdFactory` are runtime-checkable injected protocols.
Outputs must be non-empty strings; collaborator failures are wrapped with `__cause__`.
No Fill or result escapes if either factory fails.

Exact ordering is: execution ID -> decision/config validation and arithmetic -> fill
ID -> Fill -> result. Invalid executable input consumes an audit execution ID but does
not consume a fill ID.

The provider is stateless and therefore cannot prove global ID uniqueness or suppress
repeated decisions. Duplicate IDs are detected by result construction, Portfolio fill
idempotency and the future execution journal/orchestrator. Adding an in-memory dedup
registry would make the provider stateful and is outside M3.6.

## 9. Failure boundaries

Business result:

- valid `REJECTED` decision -> `NOT_EXECUTED / DECISION_REJECTED`.

Typed exceptions:

- invalid configuration or rules;
- incomplete or corrupted RiskDecision;
- rules-version/stream-side/quantity/price mismatch;
- fee rate outside the approved envelope;
- Decimal arithmetic or reconciliation failure;
- invalid/duplicate-within-result factory values;
- unexpected factory failure, preserving `__cause__`.

There are no simulated exchange rejects, retries, timeouts or fallback fills.

## 10. Ownership and lifecycle

The application composition root owns the provider and factories. The provider has no
`start`, `stop` or `close`: it owns no resources. Application orchestration separately
calls `execute()`, then atomically hands the returned Fill to Portfolio. Execution
success does not mean Portfolio commit success; the future orchestration result must
represent that boundary explicitly.

## 11. M3.6 test plan

- exact BUY and SELL fills, direction of balance deltas and full quantity;
- zero slippage and reference-price equality;
- fee/gross/net quote rounding at exact boundaries;
- fee rate zero, maximum approved rate and incompatible rate;
- low/high global Decimal context, scientific notation and large/small operands;
- `APPROVED`, `MODIFIED` and defensive `REJECTED` decisions;
- invalid decision/order/rules-version combinations;
- deterministic decision-derived timestamps and configured latency;
- fill/execution factory failure, invalid output and preserved causes;
- equal fill/execution IDs rejected; repeated IDs documented at ownership boundary;
- input immutability, result deep immutability and provider statelessness;
- architecture AST boundaries: no Binance/httpx/market cache/polling/shadow/strategy/
  risk manager/Portfolio aggregate/database/filesystem/random/time/sleep;
- offline integration: RiskDecision -> Fill -> `Portfolio.apply_fill()` for BUY/SELL;
- integration failure leaves Portfolio atomic and provider reusable;
- no runtime wiring in M3.6 implementation.

## 12. Future extensions

Separate ADRs are required for partial fills, maker/taker fees, non-zero slippage,
spread, IOC/FOK, cancellation, timeout, exchange rejection, durable execution journal,
stateful idempotency and live/testnet adapters. They must extend the port without
changing the meaning of existing immediate `FILLED` results.

## 13. Open questions

1. **Resolved in M3.6:** `RiskDecision` carries `stream_key`; quote asset and rounding
   remain in injected versioned `InstrumentRules`.
2. Should a rejected decision be passed to execution at all? Recommendation: the
   orchestrator skips it, while the provider retains defensive `NOT_EXECUTED` behavior.
3. Is `order_id = execution_id` sufficient once an order lifecycle exists?
   Recommendation: yes for immediate-fill M3.6; split identities with lifecycle ADR.
4. Where is decision-level idempotency enforced before persistence exists?
   Recommendation: future orchestration, not the stateless provider.
5. Which quote rounding mode will a future live adapter require if it differs from the
   current `InstrumentRules.round_quote()` semantics? Resolve before adapter parity.

## 14. Final implementation guarantees

The implementation and final safety review are complete. Confirmed guarantees:

- each executable decision creates one immediate complete Fill with zero slippage;
- timestamps derive only from decision time plus deterministic configured latency;
- execution explicitly rounds gross and fee quote facts through InstrumentRules;
- valid REJECTED decisions produce NOT_EXECUTED and preserve all rejection reasons;
- full paper-only dust liquidation is compatible with Portfolio accounting;
- the provider is stateless, does not mutate Portfolio and has no decision-level
  idempotency;
- runtime, network, persistence, private API and real capital remain absent.
