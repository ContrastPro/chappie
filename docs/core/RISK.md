# Risk

## Scope

This is the canonical current contract for Chappie's deterministic financial Risk
boundary. It covers the pure long-only Spot `RiskManager` used by Paper and the
additional pure exchange-risk/submission gates used by Testnet. It does not define
signals, intent construction, Portfolio accounting, persistence, or transport. See
[Trading decisions](TRADING_DECISIONS.md), [Paper trading](PAPER_TRADING.md),
[Persistence](PERSISTENCE.md), and [Exchange execution](EXCHANGE_EXECUTION.md).

```text
Strategy (advisory Signal)
  -> Planner (OrderIntent)
  -> RiskManager (financial APPROVED / MODIFIED / REJECTED decision)
  -> Paper execution, or Testnet exchange-risk and submission-safety gates
```

Strategy and Planner are not execution authorities and cannot bypass Risk. Risk
does not submit exchange orders, create Fills, mutate exchange state, own
persistence, reserve/mutate Portfolio, or fabricate Portfolio facts.

## Safety Authority and Inputs

`RiskManager.evaluate(intent, portfolio, market_snapshot)` is synchronous,
side-effect-free, and has no network, filesystem, database, cache, clock, retry,
random, or global mutable-state dependency. Composition owns its immutable
`PortfolioRiskPolicy`, `InstrumentRules`, and decision-ID factory.

Inputs are immutable current records:

- `OrderIntent`: stream, action, target exposure, time, strategy, and correlation;
- `PortfolioSnapshot`: authoritative identity/version, balances, optional position,
  equity, and accounting counters;
- `MarketSnapshot`: same-stream closed-candle history and `as_of_ms`;
- versioned `InstrumentRules` and policy supplied during construction.

The reference price is exactly `latest_closed_candle.close`; an open candle is never
used. The caller must mark Portfolio before Risk with that same price and timestamp.
Risk does not mark it.

Gates verify stale state (when policy requires it), closed candle, intent/market/
Portfolio/rules stream equality, Portfolio/rules asset equality, intent and
Portfolio timestamp equality, positive finite tick-aligned reference price, and
position/base-balance consistency. Equity must be exactly:

```text
quote_balance + (base_balance * reference_price)
```

For a position, `last_mark_price` must equal reference price. For cash-only state,
equity equals quote balance. Corrupt assets/positions are invariant errors;
ordinary unsafe market/snapshot conditions are typed rejections.

## Risk Decision Contract

| Outcome | Meaning |
| --- | --- |
| `APPROVED` | Executable without a policy, budget, or rounding modification. |
| `MODIFIED` | Still executable after recorded deterministic caps, rounding, budget reduction, or Paper dust liquidation. |
| `REJECTED` | No executable parameters exist; execution must not occur. |

`APPROVED` and `MODIFIED` require immutable `ApprovedOrderParameters` and
`RiskSizingAudit`. `REJECTED` has a nonempty duplicate-free ordered reason tuple,
no approved order, and no modifications. `APPROVED` has no modification reason;
`MODIFIED` has at least one.

Each decision binds the intent, market snapshot, Portfolio ID/version, stream,
policy name/version, applied limits, correlation, immutable sizing audit, and
decision time equal to `market_snapshot.as_of_ms`. Approved parameters bind side,
quantity, reference price, quote-rounded gross, target exposure, BUY worst-case
envelope, policy facts, and dust flag. They are not an exchange order and do not
guarantee a later execution context remains current.

## Portfolio Authority and Exposure Model

Portfolio—not Risk—owns balances, position, cost basis, fees, PnL, turnover,
equity, drawdown, versions, and fill application. Risk reads one immutable valued
snapshot only; it neither reconstructs, reserves, mutates, nor replaces it. See
[Paper trading](PAPER_TRADING.md) for accounting authority.

The current financial model is long-only Spot. `ENTER_LONG` needs positive target
exposure. `EXIT_LONG` needs target exposure zero and is a full exit only. There is
no shorting, leverage, margin, borrowing, or partial reduction action. An ENTER that
would reduce current base is rejected; it is never converted into a SELL.

Let `E` be current equity, `p` reference price, `x` requested exposure, and
`q_current` position/base quantity:

```text
x_capped                  = min(x, max_target_exposure)
n_requested               = E * x
n_exposure_capped          = E * x_capped
n_allowed                 = min(n_exposure_capped, max_position_notional)
q_target_raw              = n_allowed / p
q_target                  = round_quantity_down(q_target_raw)
delta_raw                 = q_target - q_current
q_order                   = round_quantity_down(delta_raw), then caps
gross                     = round_quote(q_order * p)
q_resulting               = q_current + q_order
approved_target_exposure  = (q_resulting * p) / E
```

Positive target with non-positive equity rejects. Zero delta is `ALREADY_AT_TARGET`.
Negative ENTER delta is `ACTION_DELTA_MISMATCH`, or
`POSITION_LIMIT_UNSATISFIABLE` if a position cap caused it. A target rounded to zero
is a rejection, not a modified no-op.

## BUY Sizing and Balance Constraints

Caps apply in order: target exposure, position notional, quantity rounding, order
notional, then quote budget. The audit/reasons record
`TARGET_EXPOSURE_CAPPED`, `POSITION_NOTIONAL_CAPPED`, `QUANTITY_ROUNDED`,
`ORDER_NOTIONAL_CAPPED`, and/or `QUOTE_BUDGET_CAPPED`.

```text
available_quote = quote_balance - min_cash_reserve
worst_price     = p * (1 + max_slippage_bps / 10000)
required_quote  = q_order * worst_price * (1 + max_fee_rate)
affordable      = round_quantity_down(
                    available_quote / (worst_price * (1 + max_fee_rate)))
```

Quantity above `affordable` is reduced to the step-aligned value. `required_quote`
is deliberately not quote-rounded: it is the conservative approval envelope. Exact
equality with available quote is allowed. Non-positive availability rejects as
`CASH_RESERVE_LIMIT` when caused by reserve, otherwise `INSUFFICIENT_QUOTE`.

After caps, quantity must be positive and at least `min_quantity`, and quote-rounded
gross must meet `min_notional`; otherwise Risk returns respectively
`ROUNDED_QUANTITY_ZERO`, `BELOW_MIN_QUANTITY`, or `BELOW_MIN_NOTIONAL`. Risk does
not add a fee reserve beyond configured cash reserve and this envelope.

## SELL Sizing

`EXIT_LONG` requires a nonempty position and positive quantity equal to Portfolio
base balance. That quantity must already align to the quantity step; corruption is a
`RiskInvariantError`. Risk approves exactly that entire quantity with `SELL`, zero
approved target exposure, zero BUY quote requirement, and gross
`round_quote(q_current * p)`.

Partial SELL sizing does not exist in this manager. A normal full exit is
`APPROVED`. If the complete existing position is below `min_quantity` or gross is
below `min_notional`, the current Paper contour approves the complete exit as
`MODIFIED` with `DUST_LIQUIDATION`; it never creates a partial dust order. This is
not a claim that a venue will accept below-filter dust. No position rejects as
`NO_POSITION`.

## Precision and Exchange Constraints

All money, price, and quantity values are finite `Decimal`; floats and tolerance
comparisons are excluded. Local Decimal precision is derived from operands, so
results do not depend on global Decimal context.

`InstrumentRules` are immutable/versioned and require positive tick, step, minimum
quantity, and minimum notional. Price and quantity are floored to their positive
tick/step. Quote values quantize down to `quote_asset_precision`. Risk rejects an
unaligned reference price rather than rounding it, rounds target/order quantities
down, quote-rounds approved gross, and does not round Portfolio accounting values.

The immutable audit retains requested/capped target figures, raw/rounded target,
raw delta, final quantity/gross, current position notional, approved exposure,
worst-case BUY facts, and rules version.

## Fee Boundary

Risk never charges, persists, or mutates an actual fee, slippage, spread, fill price,
or balance. BUY approval uses `max_slippage_bps` and `max_fee_rate` only as a
conservative gross-plus-fee envelope. SELL has no BUY quote requirement. Execution
owns actual fees/fills; Portfolio owns cost basis, fee, PnL, balances, turnover, and
equity. The current quote-fee accounting formulas live in
[Paper trading](PAPER_TRADING.md).

## Rejection and Failure Taxonomy

Expected unsafe state produces `REJECTED` with a current typed primary reason:

```text
STALE_MARKET, NO_CLOSED_CANDLE,
STREAM_MISMATCH, PORTFOLIO_STREAM_MISMATCH,
PORTFOLIO_TIME_MISMATCH, PORTFOLIO_VALUATION_MISMATCH, INTENT_TIME_MISMATCH,
INVALID_ACTION, INVALID_TARGET_EXPOSURE, INVALID_REFERENCE_PRICE,
NON_POSITIVE_EQUITY, ALREADY_AT_TARGET, ACTION_DELTA_MISMATCH,
ROUNDED_QUANTITY_ZERO, BELOW_MIN_QUANTITY, BELOW_MIN_NOTIONAL,
INSUFFICIENT_QUOTE, INSUFFICIENT_BASE, NO_POSITION,
POSITION_LIMIT_UNSATISFIABLE, CASH_RESERVE_LIMIT
```

Some retained enum members are compatibility vocabulary; the current long-only
full-exit path normally reaches stronger invariant/no-position gates first. Invalid
dependencies/configuration/model construction/arithmetic and decision-ID factory
failure raise typed `RiskError` subclasses, rather than becoming a fake rejection or
approval. Risk does not own process `SAFE_STOP`; application/coordinator failure
handling does. Callers must fail closed on a Risk exception.

## Determinism and Idempotency

Inputs/results are frozen and evaluation does not mutate inputs. Same semantic
intent, valued Portfolio, market snapshot, policy, rules, and ID-factory output give
the same sizing/audit/outcome. The ID factory is injected, so otherwise identical
evaluations may intentionally receive different decision IDs. Correlation, market,
Portfolio, rules, and policy lineage are retained.

Risk is stateless. It provides neither submission/Fill idempotency nor durable
deduplication, reservation, or stale-decision revalidation; those belong to runtime,
persistence, and execution.

## Testnet Additive Exchange Risk

Testnet begins with the same financial decision, then runs pure
`evaluate_exchange_risk(decision, portfolio, context, policy)`. It validates account
and Portfolio stream/assets/reconciled balances, then checks active session; the
initial hard limit of one non-terminal and one in-flight order; effective resources
after remaining reservations/cash reserve; no-short projected BUY/SELL position;
order/position/notional/quantity limits; session submitted notional, turnover,
submission count, execution failures, and drawdown.

Upper resource/count limits allow exact equality. Failure-count and drawdown block
at equality. The result is immutable `ExchangeRiskAssessment` (`APPROVED` or
`BLOCKED`) with ordered typed reasons and a projection/policy audit. A financial
rejection remains `RISK_NOT_APPROVED`; it is never reinterpreted.

Final `evaluate_exchange_submission_safety` is separate and does not POST. It
requires Testnet, approved financial assessment, cleared current-generation kill
switch, SAFE reconciliation/current generation, safe pending control, and a genuine
matching process-local capability (environment/account/session lineage). LIVE is
unavailable. Historical Stage 4 recovery is not active MVP Risk behavior.

## Mode Boundary and Observability

Paper and Testnet share the pure financial manager. Paper consumes an approved
decision locally; Testnet adds exchange financial and final submission-safety gates.
In every mode strategy stays advisory, Planner creates intent, and execution remains
the separate consumer.

Decisions/audits retain safe IDs, policy/rules versions, quantities, notional,
reasons, Portfolio/market lineage, and projections. They must never expose
credentials, private account payloads, or transport headers.

## Tests and Verification

`tests/test_risk_manager.py` covers Decimal sizing/caps/boundaries, valuation gates,
full exits/dust, typed errors, immutability, and global-context independence.
`tests/test_exchange_risk.py` covers pending exposure, exact thresholds, control and
capability lineage, and separation of financial from submission approval.
`tests/test_paper_execution.py`, `tests/test_paper_engine.py`,
`tests/test_offline_decision_flow.py`, and `tests/test_trading_coordination.py`
cover the Planner-to-Risk-to-consumer boundary and rejected-decision non-execution.

## Historical Notes and Code/Document Conflicts

| Old claim | Current code / evidence | Disposition |
| --- | --- | --- |
| Original source used reason names such as `MARKET_PRICE_UNAVAILABLE`, `TARGET_EXCEEDS_LIMIT`, and `DUST_REMAINDER`. | `app/risk/models.py` and `tests/test_risk_manager.py` use the current enum above. | SUPERSEDED |
| Some source passages described order/position caps as future policy. | Current `PortfolioRiskPolicy` requires and implements target, position, order, and quote-budget limits. | SUPERSEDED |
| Partial SELL/remainder rules were proposed. | Current manager permits only full `EXIT_LONG`; complete Paper dust exit is explicit. | SUPERSEDED |
| M3 design called execution/persistence future work. | Paper now has durable execution/accounting and Testnet has distinct exchange gates. | SUPERSEDED |
| M4.4 introduced pure exchange-risk/submission separation. | That separation is current; historical one-shot Stage 4 recovery is not active MVP Risk. | MERGED / HISTORICAL_ONLY |
| Daily loss, multi-symbol limits, financial-manager reservations, volatility/frequency policy, futures/margin/LIVE. | Not current financial-manager behavior. | HISTORICAL_ONLY |

## Source Invariant Crosswalk

| Source section / unique invariant | Destination | Status |
| --- | --- | --- |
| 1–2: sole financial authority; no execution/Fill/Portfolio/I/O | Scope; Safety authority | PRESERVED |
| 3: immutable policy and injected deterministic factory | Safety authority; Determinism | PRESERVED |
| 4: closed-candle reference and exact valuation alignment | Safety authority and inputs | PRESERVED |
| 5: ordered gates; ordinary rejection vs invariant failure | Rejection and failure taxonomy | MERGED |
| 6: Decimal target/delta formula and long-only actions | Portfolio authority; Exposure model | PRESERVED |
| 7: down-rounding; APPROVED/MODIFIED/REJECTED | Decision contract; Precision | PRESERVED |
| 8: immutable audit/approved-order/lineage | Decision contract | PRESERVED |
| 9: typed rejection/error taxonomy | Rejection and failure taxonomy | PRESERVED; obsolete names superseded |
| 10 BUY envelope, cash reserve, affordability | BUY sizing; Fee boundary | PRESERVED |
| 10 SELL full exit and dust policy | SELL sizing | PRESERVED |
| 11: no mutation/I/O; statelessness; execution owns stale version | Safety authority; Determinism | PRESERVED |
| 12–14: extensions, future tests, M3 open questions | Historical notes; Tests | HISTORICAL_ONLY where not current |
| 15 M4.4: exchange financial gate and submission safety | Testnet additive exchange risk | MERGED |
| 15 M3.4 final guarantees | Entire current contract and test references | MERGED |
