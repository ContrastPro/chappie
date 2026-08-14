# Stage 4 Managed SELL Closure — Offline Safety Design

Date: 2026-08-14

## Verdict

**READY_FOR_STAGE4_SELL_SCHEMA_IMPLEMENTATION**

The remaining Stage 4 evidence gap can be closed by a distinct one-shot managed SELL
authority, additive schema v7, a dedicated operator composition and same-database
recovery. This is design readiness only. ADR-066 was accepted after final review on
2026-08-14; no SELL,
network access, production migration or execution is authorized by this document.

## Scope and immutable starting truth

The production lineage starts at schema v6 with Portfolio version `1`, managed
`0.00016 BTC`, managed `14.890736 USDT`, zero fees and turnover `10.109264 USDT`.
The sole managed BUY is FILLED and settled, reconciliation generation `2` is SAFE,
global settlement is SYNCHRONIZED, and the kill switch is ACTIVE at control generation
`0`. The account's pre-existing approximately `1 BTC` is external inventory. It is
reconciliation evidence only and never a source of financial Risk availability.

This closure adds no generic SELL permission, recurring trading, cancellation, signal
path, LIVE path or new exchange.

## Quantity policy

The reviewed intent is a **full managed-position exit**, not a hard-coded quantity.
At the future preflight the candidate is the current durable managed base balance from
the same Portfolio lineage. The candidate is accepted only if all of these hold:

1. it is a finite positive `Decimal` and exactly equals the open managed position;
2. it is exactly aligned to the fresh effective `MARKET_LOT_SIZE`, falling back to
   `LOT_SIZE` only according to the adapter's current Binance contract;
3. it is at least fresh `minQty` and no more than fresh market maximum quantity;
4. candidate × fresh public reference price satisfies fresh MARKET `minNotional` and
   any applicable maximum notional rule;
5. financial and exchange Risk approve the exact same quantity; and
6. the final in-transaction revalidation observes the same Portfolio version, state
   digest, managed quantity, rules/config fingerprints and fresh risk facts.

There is no upward rounding and no account-balance fallback. The implementation must
not silently round down either: a smaller order would leave managed dust and weaken the
clean provenance proof. If the exact full managed position is not currently tradable,
the operator stops and Stage 4 remains BLOCKED. Thus `0.00016 BTC` is the current
inventory fact, not an advance order authorization or stale-rules quantity decision.

The current `ExchangeInstrumentRules` port represents step, minimum quantity and
minimum notional but not the filter's maximum quantity. Schema implementation must
extend the typed model with `max_quantity: Decimal | None`: positive `maxQty` is the
inclusive maximum, while exact zero is canonical `None`/`UNBOUNDED` only for the
explicitly recognized Binance disabled-maximum contract. Missing, negative,
non-finite or otherwise ambiguous maximum semantics fail closed. The fingerprint
includes selected filter type, min, max-or-`UNBOUNDED`, step, minimum notional,
notional applicability and adapter rules-contract version.

`MARKET_LOT_SIZE` is selected as a whole only when its min and step are positive. If
either is exact zero, the whole quantity contract—including `maxQty`—falls back to
`LOT_SIZE`; fields must never be mixed across filters. A selected positive maximum
must be at least its minimum and step-compatible. `NOTIONAL` is preferred over
`MIN_NOTIONAL`; minimum applicability to MARKET must be true. A finite applicable
maximum notional must either be represented and enforced or cause fail-closed
normalization. Unknown filters or applicability semantics remain unsupported. Ignoring
`maxQty` is not acceptable, and no value may be inferred from old evidence.

## Managed ownership boundary

SELL eligibility is:

`eligible_base = managed Portfolio base − managed pending SELL reservations`

It is never `exchange free BTC`, `exchange total BTC`, or their difference from an
estimated external balance. The financial Risk input uses the recovered managed
Portfolio. The exchange Risk account view is a typed managed-scope projection equal to
that Portfolio plus its durable reservations. Separately, reconciliation proves:

`exchange total BTC = preserved external baseline + managed exchange effect`

This second equation detects account conflicts but grants no spending authority. An
exchange observation near `1.00016 BTC` therefore yields only `0.00016 BTC` eligible;
an order for any larger quantity is rejected before persistence even if Binance could
fund it. The authority records the external baseline, managed balance, Portfolio state
digest and ownership-policy digest so they cannot be exchanged at the final gate.

## Dedicated authority and schema v7

Schema v6 cannot safely reuse `exchange_smoke_authorities`: migration 005 fixes its
side to BUY, policy to `m4.10-phase3-smoke/v1`, unique first-BUY economic slot and a
version-zero/empty-order transaction. Those constraints are deliberate and remain
unchanged.

Migration 007 should add, transactionally:

- `exchange_managed_sell_authorities`;
- `exchange_managed_sell_authority_events`; and
- append-only UPDATE/DELETE rejection triggers for both.

The authority is terminal `CONSUMED` at its only committed state and binds:

- authority ID and unique operator run ID;
- TESTNET, `binance-spot`, `BTCUSDT`, MARKET and SELL only;
- existing execution root, session, account fingerprint and managed Portfolio ID;
- exact Portfolio version-before and state digest;
- exact managed base, full-exit quantity and source BUY/order/trade/settlement lineage;
- external-baseline and managed-balance-scope payload/digest;
- ACTIVE control generation and current SAFE reconciliation generation/run;
- exact `SYNCHRONIZED` settlement snapshot digest, unresolved/pending counts and last
  economic Portfolio settlement/resolution linkage;
- rules, configuration, risk-policy and ownership-policy fingerprints;
- deterministic economic slot and deterministic durable command/client identities;
- one-command, one-submission, one-non-terminal and one-in-flight ceilings;
- creation/consumption timestamps, canonical payload and record digest; and
- the unique consumed durable command key.

The deterministic slot is derived independently from root, account, Portfolio ID and
the fixed label `stage4-managed-full-exit-v1`. Unique constraints cover root/slot,
operator run, consumed command and authority ID. The authority event is ordinal one,
`CONSUMED`, and links the same command and operator run.

The existing session is deliberately reused. Its current durable counters are one
submission and `10.109264` submitted/executed notional. The SELL policy must bind these
exact starting counters and permit exactly one incremental submission: cumulative
submission-count ceiling `2`, with cumulative submitted-notional and turnover ceilings
computed as the exact starting values plus the one reviewed SELL bound. These are
closure-specific ceilings, not a reusable increase for ordinary Testnet execution.

One `BEGIN IMMEDIATE` transaction must revalidate all lineage, current Portfolio
version/digest, managed ownership, SAFE reconciliation/coverage, ACTIVE control,
SYNCHRONIZED settlement, terminal prior BUY, zero non-terminal/in-flight work and
session counters; insert the authority; create the sole SELL command/order in PREPARED;
append its PREPARED event and reservation truth; link and consume the authority; append
the authority audit event; and commit all-or-nothing. Migration 007 is additive;
migrations 001–006 remain byte-identical. Migration interruption must leave a valid v6
database and release the writer.

## Kill switch and process-local claim

The global kill switch remains ACTIVE. There is no durable CLEARED interval. After the
atomic commit, persistence may return one opaque, non-copyable, non-serializable
`Stage4ManagedSellProcessClaim` bound to every authority and command fact above. The
final gate accepts ACTIVE control only for that exact claim and consumes it once.
Restart, copying, serialization, stale generation, alternate command or changed rules
cannot recreate it. A recovered PREPARED or SUBMITTING SELL is recovery-only forever.

Phase 3 claims/tokens, Phase 4/remediation/proof tokens, credentials and ordinary
Testnet readiness are insufficient. The proposed future entrypoint is:

`python -m app.tools.testnet_stage4_sell`

with exact separate authorization `STAGE4_MANAGED_SELL_AUTHORIZED=YES`. It must not
import or call `app.main`, a strategy, pipeline, coordinator, continuous consumer or
background worker.

## Fresh preflight and operator flow

The one-shot operator has a fixed Testnet host, a strict request budget and no retries.
Before writable database acquisition or authority creation it performs fresh approved
reads for exchange rules/reference price, account balances, open orders, the exact
known managed order, and managed-prefix trade history. The precise GET budget is fixed
during implementation after endpoint composition review; it cannot grow dynamically.

Preflight requires the expected account fingerprint; no unexpected open order; exactly
the known BUY lineage and no conflicting managed-prefix history; fresh compatible
rules; Portfolio version/digest and `0.00016 BTC` current managed inventory; SAFE current
reconciliation with complete coverage; SYNCHRONIZED settlement; preserved external
baseline; and enough exchange free BTC to cover the managed quantity without treating
the remainder as managed. A second in-transaction validation closes the preflight-to-
PREPARED race. A final pre-POST validation closes the PREPARED-to-side-effect race.

The future operator takes a second bounded cleanliness snapshot after authority commit
and immediately before final claim consumption. It repeats the approved account/open-
order/managed-history observations and revalidates rules/config fingerprints and the
freshness deadline. Any change loses the process claim and leaves the sole PREPARED
command recovery-only; no replacement authority is issued. The final gate also
rechecks, transactionally, control/reconciliation generations, settlement snapshot
digest, Portfolio version/digest/managed base, session counters, rules/config/risk
fingerprints, non-terminal/in-flight counts and the second snapshot digest. A remote
change after the last bounded observation cannot expand quantity or ownership: the
request remains capped at the durable managed amount and may only be accepted or
rejected by Testnet.

The flow is:

1. exact authorization and evidence-collision checks;
2. fresh bounded preflight;
3. immutable/read validation of the same v6 production lineage;
4. financial Risk full-exit decision using managed Portfolio only;
5. exchange Risk using the managed-scope account projection only;
6. atomic schema-v7 authority + PREPARED command commit;
7. issue and consume the process-local exact-command claim at the final gate;
8. commit SUBMITTING and the sole submission attempt;
9. at most one Testnet POST; then stop and close all resources.

The request contract is `BTCUSDT`, `SELL`, `MARKET`, exact fresh managed Decimal
`quantity`, persistence-derived `newClientOrderId`, and `newOrderRespType=ACK`.
`quoteOrderQty`, caller-provided identity, fallback endpoint, replacement identity,
second economic slot and retry are forbidden.

The existing exchange-Risk invariant requires its account view to equal managed
Portfolio balances. Passing raw Binance totals would therefore fail (and must continue
to fail). The operator must construct the typed managed-scope account view only after
the separate raw-account/external-baseline reconciliation succeeds; the two objects
must be distinct types or otherwise impossible to substitute at the authority/final
gate boundary.

## ACK, reconciliation and accounting

ACK changes only durable exchange-order state. It does not create a Fill, change the
Portfolio, infer price/fee/proceeds/PnL or establish completion. Lost ACK, timeout,
connection reset, 5xx and process death are ambiguous reconciliation states and never
cause resubmission.

The existing side-aware settlement model supports SELL from authoritative trades:

- base change is `−trade.quantity`;
- quote change is `trade.quote_quantity − supported quote fee`;
- full exit removes the position and its remaining cost basis;
- realized PnL increment is net quote proceeds minus removed managed cost basis;
- fees and authoritative quote turnover increase exactly once;
- partial fills reduce managed base/cost basis proportionally and remain INCOMPLETE
  until authoritative order/trade totals are terminal and complete; and
- a terminal status without complete authoritative trades is insufficient.

Positive USDT commission remains supported. Positive BTC, BNB or other commission
remains `BLOCKED_UNSUPPORTED_FEE`; no conversion, external-balance deduction or price
reconstruction is allowed. ADR-065 exact finite Decimal zero is side-neutral: the
schema-v6 resolver builds its Fill from the persisted trade side, so zero BTC/USDT/BNB
labels can resolve a SELL without economic fee. Implementation must add explicit SELL
coverage for normal quote fee, exact-zero labels, partial fills, unsupported positive
fees, duplicate/reordered delivery and recovery before any production authorization.

If the full managed position settles under accepted fee semantics, managed BTC becomes
zero. Managed USDT, turnover and realized PnL are derived only from authoritative SELL
trades and are not precomputed. Exchange BTC may remain near the external `1 BTC`; the
fresh proof must show that it remains outside managed Portfolio truth.

## Crash, restart and exactly-once matrix

Fault injection must cover `RuntimeError`, `KeyboardInterrupt`, `SystemExit` and
`GeneratorExit` at least:

- before authority transaction and after each authority/PREPARED/link/audit insert;
- before and after authority commit/claim issue;
- before and after final claim consumption;
- before/after SUBMITTING and attempt commit;
- immediately before POST and after transport side effect;
- ACK parsing/persistence and lost ACK;
- reconciliation order/trade persistence;
- each SELL settlement record, Portfolio current, session turnover and commit boundary;
- exact-zero block/resolution boundaries; and
- evidence publication before/after fsync/rename/PASS marker.

Pre-commit state rolls back fully. Post-authority-commit crashes retain one PREPARED
command but lose submission eligibility. Post-SUBMITTING ambiguity recovers by the same
client identity only. Duplicate operator delivery, restart, reordered history and lost
settlement acknowledgement converge on one authority, command, attempt, exchange
order, each exchange trade and each Portfolio version transition. Quote proceeds,
turnover, fees and realized PnL apply once.

## Same-database recovery and evidence

The closure upgrades and extends `data/m4_10_testnet_smoke.sqlite3`. A fresh Portfolio
or database is forbidden because only the existing lineage proves ownership of the
managed BTC. Recovery must validate the original BUY authority/order/trade/block/
resolution, then the SELL authority/order/trades/settlements as one version chain.

Reserve collision-protected sanitized paths without overwriting M4.10 evidence:

- `logs/stage4_managed_sell_submission.log`;
- `logs/stage4_managed_sell_recovery.log`; and
- `logs/stage4_managed_sell_zero_fee_resolution.log` only if an exact-zero block
  requires a distinct persistence-only resolution run; and
- `logs/stage4_managed_sell_closure_proof.log`.

Evidence contains IDs, counts, normalized states, request budgets and checksums only;
never credentials, signatures, signed URLs, headers or raw private responses. PASS is
published last through exclusive atomic creation.

The evidence chronology is fixed: bounded preflight/submission; fresh-process
reconciliation and authoritative trade settlement; optional exact-zero resolution
without network or trade rewrite; a new immutable fresh-process recovery proof; then
the final Stage 4 completion report. Each phase has a distinct exact authorization and
collision-protected path. Authorization never carries forward.

## Stage 4 closure predicate

Stage 4 may become COMPLETE only after separate implementation review and separately
authorized production phases prove at least one controlled managed BUY and one managed
SELL, both authoritative trade histories, exactly two total durable submission attempts
(the existing BUY plus one SELL), zero duplicate submissions/settlements,
managed-only inventory use, final recovered Portfolio truth, preserved external
inventory, safe crash/restart behavior, ACTIVE final kill switch, no LIVE behavior and
no credential leakage. The recovered evidence records authoritative BUY cost basis,
SELL proceeds/fee, removed cost basis and realized PnL without interpreting its sign as
profitability. Until that proof, Stage 4 remains BLOCKED and Stage 5 remains not started.

## ADR status and next safe action

The new authority and ACTIVE-switch exception are architecture-significant. ADR-066 is
**accepted** after final design review on 2026-08-14. The next safe action is offline
schema-v7/domain/persistence implementation and adversarial tests. Production migration,
credentials, network and SELL execution remain separately unauthorized; no production
authorization follows from implementation.
