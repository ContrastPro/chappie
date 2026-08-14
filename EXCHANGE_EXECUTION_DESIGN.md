# Exchange Execution Architecture Design

## 1. Status and scope

- Milestone: M4.1 Exchange Execution Architecture Design.
- Status: **final design; M4.2 safety review passed**.
- Date: 2026-08-09.
- Initial implementation target: **Binance Spot Testnet only**.
- M4.2 review date: 2026-08-09.
- Basis: completed Stage 3 durable paper runtime and accepted ADR-055–ADR-062.
- M4.3 implementation status: **completed; final domain safety review passed**.

M4.1/M4.2 change documentation only. They do not add production code, migrations, private
API calls, credentials, testnet execution, live execution, or permission to trade.
Successful Stage 4 testnet work will not authorize live trading.

M4.3 implements only the infrastructure-neutral `app/exchange_execution` domain and
ports: immutable commands/orders/trades/events, pure lifecycle and aggregation rules,
deterministic client IDs, transport/query DTOs, pending exposure, execution sessions,
kill-switch state and an opaque process-local capability. It adds no adapter, signing,
private call, migration, coordinator, reconciliation service or Portfolio mutation.
The final review confirms that ACK cumulative fields remain order-state observations
only and never create a trade, Fill, commission, price or settlement. Exact duplicate
trade payloads are idempotent in aggregation; a conflicting payload under the same
scoped trade identity fails closed.

## 2. Safety objective

Stage 4 may add exchange execution only if it preserves or strengthens these Stage 3
guarantees:

- one shared Strategy result feeds all enabled consumers;
- every executable intent passes deterministic Risk;
- durable identity does not depend on runtime UUIDs;
- financial Decimal values round-trip exactly;
- a Fill is never applied to Portfolio twice;
- recovery and conflicts fail closed;
- business facts commit before in-memory publication and observation;
- one process owns the financial writer;
- secrets never enter domain state, persistence, logs, or observer payloads.

The execution modes are distinct capabilities:

| Mode | Execution authority | Network side effect | Stage 4 status |
|---|---|---|---|
| PAPER | local deterministic provider | none | existing, supported |
| TESTNET | Binance Spot Testnet | private testnet orders only | initial Stage 4 target |
| LIVE | real Binance account | real-capital orders | unsupported and fail-closed |

Changing one ordinary feature flag must never enable LIVE. Strategy and market-data
configuration grant no execution authority.

## 3. Existing boundary and required separation

The existing `PaperExecutionProvider.execute(RiskDecision)` calculates one immediate,
full local Fill. `PaperTradingEngine` can therefore preview valuation and Fill, commit
all facts in one SQLite transaction, then atomically publish Portfolio memory.

Exchange execution has different semantics: submission is a remote side effect,
acknowledgement may be lost, an order can remain open, and zero or many exchange trades
can arrive later. `PaperExecutionResult`, `PaperExecutionStatus`, and the assumption
`one command = at most one Fill` must not be reused for exchange orders.

Stage 4 adds a sibling application service, provisionally
`ExchangeExecutionCoordinator`, as the testnet consumer of the existing shared
`DecisionPipelineResult`. It owns Risk invocation, durable order orchestration and
reconciliation. It does not own Strategy, Portfolio arithmetic, HTTP/signing, SQLite,
or Binance payloads.

```text
CandleClosed
  -> TradingDecisionCoordinator
  -> one DecisionPipelineResult
     -> shadow consumer (optional)
     -> paper engine (PAPER only)
     -> ExchangeExecutionCoordinator (TESTNET only)
          -> RiskManager
          -> durable pre-submit command/order
          -> ExchangeOrderGateway
          -> exchange observations and trades
          -> durable Portfolio transitions
```

PAPER and TESTNET are mutually exclusive financial consumers for one runtime. Shadow
may accompany either. LIVE has no composition path in the initial implementation.

## 4. Ownership and dependency direction

Proposed packages and ownership:

```text
app/exchange/                 domain/application-neutral exchange models and ports
app/exchange/orchestration.py state machine and command orchestration
app/exchange/reconciliation.py startup/runtime reconciliation service
app/infrastructure/binance/   private testnet HTTP, signing, response translation
app/persistence/              generic durable records/ports and SQLite adapter
app/application.py            mode validation, construction and lifecycle ordering
```

Infrastructure adapters depend inward on ports and models. Domain/application code
must not import `httpx`, Binance SDK types, API error codes, signature concepts,
endpoint paths, or Pydantic Settings.

The existing public market-data adapter remains separately configured. Its base URL,
successful polling, or exchange name cannot authorize private execution.

## 5. Order domain model

### 5.1 Durable identity and facts

An `ExchangeOrder` is a durable local aggregate with at least:

- `durable_command_key` and stable `client_order_id`;
- execution environment (`TESTNET` only initially), venue and account fingerprint;
- symbol, side, order type, time-in-force and approved quantity/price parameters;
- Risk policy, instrument rules and execution configuration fingerprints;
- local lifecycle state and monotonic state version;
- optional exchange order ID;
- exchange-reported original/executed/cumulative quote quantities;
- created, submission-attempt, exchange-update and locally-observed timestamps;
- last authoritative event sequence/digest and reconciliation requirement.

Runtime `intent_id`, `risk_decision_id`, pipeline IDs and correlation IDs remain audit
links, not durable exchange identity.

### 5.2 Lifecycle

Proposed local states:

```text
PREPARED
  -> SUBMITTING
     -> ACKNOWLEDGED
        -> PARTIALLY_FILLED
           -> FILLED
        -> CANCELED
        -> REJECTED
        -> EXPIRED
     -> RECONCILIATION_REQUIRED

ACKNOWLEDGED | PARTIALLY_FILLED
  -> FILLED | EXPIRED | RECONCILIATION_REQUIRED
```

- `PREPARED`: command, client ID and exact request semantics are durably committed;
  no transmission attempt is recorded.
- `SUBMITTING`: a durable attempt marker is committed before network transmission.
- `ACKNOWLEDGED`: the exchange confirms the order identity; it may have zero fills.
- `PARTIALLY_FILLED`: at least one authoritative trade exists and leaves quantity open.
- `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`: exchange-confirmed terminal states, but
  become locally settled only after all reported trades and Portfolio transitions commit.
- `RECONCILIATION_REQUIRED`: local knowledge is ambiguous or conflicts with exchange
  observations. It is non-terminal and forbids submission/retry.

`UNKNOWN` is not used as a second vague state name; the explicit
`RECONCILIATION_REQUIRED` state carries the fail-closed meaning. All states and every
transition are durable. Terminal rows are immutable except for later append-only
evidence proving missing trades, which must reopen reconciliation rather than silently
editing history.

Initial implementation has no cancellation command because it submits MARKET orders
only. `CANCEL_PENDING` and `CANCELED` remain recognized exchange evidence/future states,
not callable M4.3–M4.10 operations. Exchange status regression, impossible cumulative quantities, or
conflicting terminal outcomes require reconciliation; they are not auto-normalized.

### 5.3 Synchronous operations over asynchronous reality

Application calls may remain synchronous initially, but no call promises terminal
execution. `submit` returns one observation, not a Fill. Subsequent polling or startup
reconciliation advances the aggregate. Network timeout is an observation failure, not
proof that the order does not exist.

## 6. Minimal exchange ports

M4.1 proposes two infrastructure ports rather than one paper-like `execute()` method:

```text
ExchangeOrderGateway
  submit(prepared_order) -> OrderObservation

ExchangeAccountReader
  find_order(client_order_id, exchange_order_id?) -> OrderObservation | NotFound
  list_open_orders(symbol) -> tuple[OrderObservation, ...]
  list_orders(symbol, bounded_window) -> tuple[OrderObservation, ...]
  list_trades(symbol, cursor?) -> TradePage
  get_balances() -> BalanceSnapshot
  get_instrument_rules(symbol) -> ExchangeInstrumentRules
```

`ExchangeOrderGateway` initially owns the single submission side effect. Cancellation
is deliberately excluded from the first MARKET-only implementation; adding it requires
its own durable attempt identity and ambiguous-cancel review. `ExchangeAccountReader` is read-only and is
used by reconciliation. `ExecutionReconciler` is an application service, not an
infrastructure port. Splitting submit/query makes it impossible to mistake a failed
submit response for an absent order and permits read-only reconciliation while the kill
switch blocks new submissions.

Port inputs and outputs use project-owned immutable models with `Decimal` and typed
enums. The Binance adapter translates REST payloads and error codes at the boundary.

## 7. Stable client order identity

The client order ID is known and persisted before any network call:

```text
material = canonical("exchange-client-order/v1", {
  environment: "testnet",
  venue: "binance-spot",
  account_fingerprint: <non-secret provisioned identifier>,
  durable_command_key: <SHA-256 durable command key>
})
digest = SHA-256(material)
client_order_id = "ctt1t-" + lower_rfc4648_base32(digest, no_padding)[0:26]
```

The resulting 32-character ID uses the conservative ASCII subset `[a-z0-9-]`, is
restart-stable and contains 130 digest bits. Official Spot FIX documentation specifies
`^[a-zA-Z0-9-_]{1,36}$`; the REST new-order table defines the field as STRING but does
not separately publish a charset/maximum. This format is within the documented Spot
subset and remains subject to an explicit `POST /api/v3/order/test` adapter contract
test before any real Testnet submission. Database uniqueness plus payload-digest comparison detects
the already-improbable truncation collision and fails closed.
For `n` commands, the birthday approximation is `n(n-1) / 2^131`; even at one billion
commands it is about `3.7e-22` (roughly `2^-71`). The account fingerprint is a
provisioned non-secret random identifier, not a hash or substring of an API key.

The `durable_command_key` remains the semantic execution authorization. It must include
the stable decision key, approved Risk parameters, policy/rules fingerprints, TESTNET
execution environment, venue, account fingerprint, order type/time-in-force, and
adapter semantic version. Neither API credentials nor random runtime IDs participate.

The same persisted client ID is used for submission, timeout recovery, querying and
trade reconciliation. It is never regenerated after uncertainty.

## 8. Critical ambiguous-submit policy

Normative rule:

```text
commit SUBMITTING
-> transmit request
-> timeout / disconnect / response parse failure
-> commit RECONCILIATION_REQUIRED
-> query by the same client_order_id
-> ingest authoritative order/trades or remain blocked
```

There is no blind resubmission from `SUBMITTING` or `RECONCILIATION_REQUIRED`. HTTP
timeout, connection reset, 5xx, malformed response, and process death after transmission
are all ambiguous. Even a local `NotFound` response is not immediately proof of absence:
the documented API is asynchronous and query data may come from memory or database.
Binance exposes no strong proof-of-absence primitive. Repeated negative queries by the
same client ID, plus bounded `myTrades`, open-order and order-history coverage, improve
diagnosis but never authorize automatic resubmission or a new client ID. Exhaustion
requires audited operator resolution while submission remains blocked.

Only `PREPARED` with no durable attempt marker is eligible for first submission. The
transition to `SUBMITTING` and attempt record commits before invoking the gateway.

Exact outcome classification:

| Case | Evidence and durable result | Retry/reconciliation rule |
|---|---|---|
| A | Adapter proves the request was not transmitted; remain/recover `PREPARED` and append `ATTEMPT_NOT_TRANSMITTED` | Revalidate and retry with the **same** client ID; reconciliation is not required |
| B | Request may have been transmitted but no valid response exists; `RECONCILIATION_REQUIRED` | Never resubmit; query the same client ID and reconcile |
| C | Reviewed Binance response proves rejection before acceptance; `REJECTED` | No retry of this command; a later decision requires a new command |
| D | Valid acknowledgement; `ACKNOWLEDGED` | Never retry submission; order/trade reconciliation is mandatory |
| E | Query finds the order | Append normalized evidence, ingest trades and advance state; never resubmit |
| F | Query returns not found | Stay `RECONCILIATION_REQUIRED`; broaden bounded coverage, then require operator resolution |

Timeouts, socket resets, response parse failures, `-1007`, and HTTP 5xx are Case B.
HTTP 4xx is Case C only when a maintained, tested error classification proves non-
acceptance; uncertainty is Case B. A crash after the attempt marker is always Case B,
even if it may have occurred before the socket write.

Case A never creates a new durable command or client identity. M4.5/M4.6 may retry only
the exact persisted command with the same client ID after all current gates are
revalidated. Timeout, HTTP 5xx, reset, lost response and unknown transmission are typed
Case B categories and always require reconciliation.

## 9. Durable transaction phases

Network I/O is never performed inside a SQLite transaction.

### Phase A — authorize and prepare

One local transaction:

1. claim stable decision and verify no terminal/competing command;
2. persist Risk approval and exact immutable order request semantics;
3. derive and persist `durable_command_key` and `client_order_id`;
4. create order state `PREPARED` and append its event;
5. reserve pending order exposure used by runtime safety controls;
6. commit.

No exchange side effect exists before this commit.

### Phase B — mark attempt

One short local transaction changes `PREPARED -> SUBMITTING`, appends a numbered
submission-attempt event and commits. Only then may the gateway transmit.

### Network phase

The adapter submits the exact prepared request with the persisted client ID. It returns
a typed observation or typed definite/ambiguous failure. No adapter response mutates
Portfolio directly.

### Phase C — record transport outcome

One transaction appends the transport response/failure and either a normalized ACK,
reviewed definite rejection, or ambiguity transition. Transport attempts are diagnostic
facts; they are not authoritative order/trade truth.

### Phase D — reconcile and settle

One local transaction:

1. append the raw-normalized observation digest and validated lifecycle event;
2. bind the unique exchange order ID when present;
3. insert every new exchange trade under durable uniqueness constraints;
4. preview one Portfolio transition per newly accepted trade;
5. append journal transitions and update `portfolio_current`;
6. update order cumulative facts/status and release/adjust pending exposure;
7. commit.

Only after commit are the complete Portfolio transition batch and order projection
published in memory. Observers run afterward. A failed commit leaves prior local state;
post-commit memory failure degrades exchange execution and requires restart recovery.

Valuation-only candle cycles may retain the Stage 3 transaction pattern, but must not
mark an outstanding exchange command terminal merely because the market decision ended.

## 10. Initial execution and accounting scope

The first implementation is Binance Spot Testnet, `BTCUSDT`, long-only, and MARKET BUY
or SELL only. Both sides use base-asset `quantity`; `quoteOrderQty`, price,
`timeInForce`, limit/order-list/SOR/cancel-replace and cancellation commands are out of
scope. The adapter explicitly requests `newOrderRespType=ACK`; ACK proves identity, not
execution or settlement. Dynamic `exchangeInfo` filters, including market quantity,
notional, position and order-count filters, are versioned Risk inputs.

## 11. Exchange Fill authority and Portfolio boundary

Binance-reported trades are the only authority for TESTNET execution economics. Local
reference price, acknowledgement fields and cumulative quantities cannot manufacture a
Fill. One order may have zero, one or many trades.

Each normalized `ExchangeTrade` contains:

- venue, account fingerprint, symbol and exchange order ID;
- stable client order ID and durable command key;
- exchange trade ID (the primary venue execution identity);
- side, exact quantity, price and quote quantity;
- commission amount and commission asset;
- exchange trade timestamp and maker/liquidity metadata when available;
- canonical source payload digest and adapter model version.

The local Portfolio Fill ID is deterministic from venue/account/symbol/exchange trade
ID, not random. `UNIQUE(venue, account_fingerprint, symbol, exchange_trade_id)` and
`UNIQUE(fill_id)` prevent duplicate ingestion and application. Cumulative order
quantity must equal the sum of accepted trade quantities at a defined reconciliation
cut; it is a cross-check, not a substitute for trades.

Existing Portfolio accounting accepts fees only in the quote asset and requires one
Fill per accounting mutation. Therefore initial testnet settlement supports only facts
that can be represented exactly:

- quote-asset commission: supported by existing formulas after model adaptation;
- base-asset commission: unsupported until net acquired/sold quantity and cost-basis
  semantics are explicitly extended;
- third-asset commission such as BNB: unsupported until multi-asset balances and fee
  valuation are designed.

An unsupported fee asset is persisted as exchange evidence, activates the kill switch,
sets reconciliation required, and blocks Portfolio application and new submissions. It
must never be silently converted to quote, rounded away, or omitted.

Partial fills are applied individually in exchange trade order. A restart between
trades recovers the last committed Portfolio journal head and resumes querying. A
duplicate trade produces no new Portfolio version; a conflicting duplicate fails closed.

Exchange records commit independently of Portfolio applicability. A base- or third-
asset commission therefore cannot erase exchange truth: the trade is durable, its
settlement record is blocked, and execution fails closed. Binance is authoritative for
account balances, orders and trades; Portfolio remains the deterministic managed
subledger/projection.

The initial lineage uses a dedicated Testnet account/key and only BTC/USDT. Manual
orders, transfers and account resets are forbidden during a lineage. Its root comes
from reconciled exchange `free + locked` balances, never arbitrary configuration.
Unexplained differences from settled balances plus reservations/in-flight effects block
execution. A Testnet reset starts a new explicitly audited lineage; it is not repaired
automatically.

## 12. Reconciliation model

### 12.1 Startup gate

Before the coordinator may subscribe for TESTNET submissions:

1. open storage and obtain exclusive writer ownership;
2. verify schema, digests and Portfolio journal exactly as Stage 3;
3. recover orders, controls, reconciliation cursors and pending exposure;
4. load all non-terminal and reconciliation-required local orders;
5. query each by stable client ID and exchange ID when known;
6. query open orders, recent trades and balances using bounded paginated cursors;
7. ingest missing order events and trades in durable settlement transactions;
8. compare exchange balances, local Portfolio and pending/open-order exposure;
9. require zero unexplained conflicts;
10. mark a durable reconciliation generation successful;
11. require an explicit operator submission capability for that generation;
12. only then construct/enable the side-effect gateway and subscribe the consumer.

Market ingestion and shadow analysis may start independently. TESTNET readiness remains
false until this gate completes. Failure does not fall back to an empty Portfolio.

### 12.2 Runtime reconciliation

Reconciliation runs synchronously immediately after ACK or ambiguity, while an order is
non-terminal under bounded polling/backoff, before a conflicting order, periodically on
a monotonic cadence with durable cursor, after rate-limit recovery, and before shutdown.
It is serialized with order settlement through the single-writer application boundary.
Polling cursors and the coverage window are durable so restart cannot silently skip
trades.

### 12.3 Fail-closed conflicts

The following require `RECONCILIATION_REQUIRED`, block new submissions and normally
activate the kill switch:

- exchange order exists but local acknowledgement is missing (ingest only after exact
  client ID/request matching);
- local acknowledged order cannot be found by all valid query paths;
- exchange cumulative quantity differs from accepted trade totals beyond a documented
  observation window;
- unknown exchange order or trade for the configured account/symbol;
- duplicate client ID or exchange ID with conflicting semantics;
- unsupported commission asset or impossible arithmetic;
- local/exchange status regression or conflicting terminal state;
- exchange rules changed after Risk approval;
- balances do not reconcile with local settled state plus explicitly modeled holds;
- pagination/cursor coverage is incomplete.

Automatic ingestion is allowed only for exact, explainable, idempotent missing facts.
Unexplained financial conflicts are never deleted, rewritten or heuristically repaired.

## 13. Risk hardening

M4.4 status: **completed; final safety review passed** (2026-08-10). Реализация
добавлена как отдельный pure `app.risk.exchange` слой поверх неизменённого Stage 3
`RiskManager`; network, persistence, command preparation и runtime wiring не входят в
milestone.

### 13.1 Deterministic pre-trade Risk

Keep the existing pure `RiskManager` responsibilities and extend its immutable inputs
and policy only where the result remains deterministic:

- existing max order/position notional and target exposure;
- existing minimum cash reserve, fee/slippage envelope and stale-market rejection;
- exchange-sourced versioned filters (step/tick/minimums and applicable maximums);
- available settled balances minus reserved pending/open-order exposure;
- maximum cumulative submitted notional, executed turnover and submission count;
- maximum realised loss, drawdown and consecutive execution failures;
- explicit permitted symbol, side, order type and time-in-force;
- maximum aggregate pending order and position notional.

Limits belong to a durable operator-started execution session anchored to Binance server
time in UTC; they never reset automatically at a calendar boundary. A new session is
allowed only with no unresolved/non-terminal conflict. Numeric thresholds are mandatory
configuration before Testnet use. `max_open_orders=1` and
`max_inflight_submissions=1` are fixed initially.

Risk receives an immutable `ExecutionRiskContext`: Portfolio version, reconciled balance
snapshot/version, active reservations, pending BUY quote, pending SELL base, counters,
reconciliation generation and control generation. It performs no network I/O. Phase A
atomically reserves worst-case quote for BUY or base quantity for SELL using compare-
and-set versions; any non-terminal order rejects another decision.

Pending exposure explicitly carries original, executed and remaining base quantity in
addition to remaining quote/base reservations and notional. The invariant is
`original = executed + remaining`; after a partial fill M4.4 therefore consumes only
the remaining reservation rather than ambiguously retaining the full original amount.

Implemented M4.4 resource formulas are deliberately conservative:

- BUY effective quote = Portfolio quote - reserved quote - configured cash reserve;
- SELL effective base = Portfolio base - reserved base;
- projected BUY position includes remaining pending BUY plus the candidate;
- projected SELL position subtracts remaining pending SELL plus the candidate and may
  never become short;
- projected submitted notional adds candidate gross notional to session submitted;
- projected turnover adds candidate gross notional to executed turnover.

Upper notional/count boundaries permit exact equality. Failure and drawdown limits block
at equality. Session limits never imply a calendar/day reset. Realised-loss enforcement
is intentionally deferred because M4.3 has no unambiguous immutable session-loss
baseline; inventing one would weaken restart determinism. Portfolio drawdown is enforced
now. Normal financial blocks and submission-safety blocks return distinct immutable,
ordered typed results.

Final review clarified that aggregation is mathematical, not authorization: the M4.3
helper sums any number of immutable pending records, while M4.4 blocks a new candidate
when the aggregate order or in-flight count reaches the fixed limit. Mixed-side remaining
quantities are explicit in the risk context. Submission also verifies the durable kill-
switch generation itself; an old capability cannot survive a control transition. The
financial audit embeds the exact immutable applied policy.

### 13.2 Runtime/exchange safety controls

These remain outside the pure RiskManager:

- execution mode and endpoint allowlisting;
- credential availability/scope validation without persisting secrets;
- startup reconciliation readiness;
- kill-switch state;
- writer ownership and order-operation serialization;
- rate limits, recvWindow, clock synchronization and retry classification;
- balance/open-order reconciliation;
- ambiguous-submit classification and reconciliation handling;
- adapter health and circuit breaking.

Risk approval expires if market freshness, rules version, Portfolio version, reservation
version, reconciliation generation or control generation changes before preparation.
Preparation must compare-and-set these versions rather than submitting stale approval.

## 14. Kill switch

The execution kill switch is a durable application control with state, generation,
reason code, actor/source and timestamp. Safe default for a new TESTNET lineage is
`ACTIVE`; enabling submissions requires an explicit operator action after successful
reconciliation. Restart preserves `ACTIVE` and never clears it from configuration alone.

When active:

- new Risk-to-command preparation and order submission are forbidden;
- reconciliation and read-only queries remain allowed;
- ingestion and settlement of already executed trades remain mandatory;
- recognized orders/trades continue to be queried, ingested and settled;
- shadow continues;
- PAPER behavior is separate and is not implicitly stopped unless a distinct global
  analytical/paper control says so.

Automatic activation occurs on reconciliation conflict, ambiguous unsupported state,
unsupported fee asset, balance mismatch, stale private-account view, writer loss,
repeated adapter safety failure, or breached hard loss/drawdown limit. Clearing requires
an audited operator command naming the exact Testnet account/database fingerprint,
expected generation and explicit Testnet acknowledgement, after successful current
reconciliation and zero conflicts. An environment value cannot clear it.

Disarming also issues only a process-local submission capability bound to the current
reconciliation/control generations. Restart destroys that capability and requires fresh
operator startup authorization even when the durable switch is disarmed. LIVE cannot
obtain this capability.
The capability is non-serializable and non-copyable. Submission additionally requires
TESTNET, matching reconciliation/control generations, Risk approval and a safe pending-
order control; clearing the kill switch alone is insufficient.

## 15. Binance Testnet adapter boundary

The future adapter owns only infrastructure concerns:

- Testnet REST base URL and strict endpoint allowlist;
- API-key header and HMAC signing;
- server-time offset, timestamp and bounded `recvWindow`;
- request serialization and Binance error/rate-limit classification;
- response schema validation and translation to project-owned models;
- pagination and request identifiers;
- redaction at exception and logging boundaries.

Credentials use dedicated names such as `BINANCE_TESTNET_API_KEY` and
`BINANCE_TESTNET_SECRET_KEY`. They are wrapped/redacted configuration values and passed
only to the adapter. Secret values, signatures and full signed URLs are forbidden in
logs, exceptions, persistence, metadata, observers, diagnostics and test fixtures.

API keys should have testnet Spot trading permissions only and no withdrawal permission.
The public-data adapter and its URL remain independent.

## 16. Configuration safety

Proposed conceptual configuration:

```text
EXECUTION_MODE=paper | testnet | live
EXCHANGE_EXECUTION_ENABLED=false
BINANCE_TESTNET_BASE_URL=<compile-time/default allowlisted URL>
BINANCE_TESTNET_API_KEY=<secret>
BINANCE_TESTNET_SECRET_KEY=<secret>
EXECUTION_KILL_SWITCH_DEFAULT_ACTIVE=true
```

Validation rules:

- default mode is PAPER and exchange execution is disabled;
- TESTNET requires `EXECUTION_MODE=testnet`, the separate enable flag, dedicated
  credentials, persistence, exact testnet endpoint allowlist and durable reconciliation;
- paper and testnet financial consumers cannot both be enabled;
- public market URL/keys never satisfy private execution requirements;
- LIVE is represented as a distinct value but initial Stage 4 code must reject it
  unconditionally as unsupported before constructing any private client;
- no generic `BINANCE_API_KEY` fallback is allowed for testnet credentials;
- environment label (`development`, `production`) does not grant trading authority;
- disabling the kill switch in environment cannot clear its durable active state.

A future LIVE implementation requires a separate build capability, credential namespace,
endpoint allowlist, configuration ceremony and explicit live-readiness ADR/review. It
must not be unlocked by changing `EXECUTION_MODE` alone.

## 17. Persistence schema v2 concept

M4.6 implements additive migration v2 with generic exchange records while leaving Stage 3
paper tables readable:

| Table | Authority and key constraints |
|---|---|
| `exchange_execution_roots` | immutable environment/venue/account/database lineage, config digest and reconciled genesis balances |
| `exchange_decisions` | one TESTNET decision claim; PK stable decision key, UNIQUE semantic slot, payload digest |
| `exchange_commands` | exact Risk-approved command; PK 64-char durable command key, UNIQUE decision key, immutable payload digest |
| `exchange_orders_current` | rebuildable projection; PK durable command key, UNIQUE client order ID, UNIQUE nullable `(venue, account_fingerprint, exchange_order_id)`, state version/digest |
| `exchange_order_events` | append-only lifecycle authority; PK event sequence, UNIQUE `(durable_command_key, state_version)`, source/event digest uniqueness |
| `exchange_submission_attempts` | append-only transport evidence; PK attempt ID, UNIQUE `(durable_command_key, attempt_number)`, transmission classification |
| `exchange_trades` | authoritative normalized trades; PK deterministic fill ID, UNIQUE venue/account/symbol/trade ID, FK order/command, immutable economics and source digest |
| `exchange_trade_settlements` | separate Portfolio applicability/result per trade; UNIQUE trade, nullable journal transition, blocked reason |
| `exchange_reconciliation_runs` | generation/coverage/result records; PK run ID and immutable input/output digests |
| `exchange_reconciliation_conflicts` | append-only unexplained conflicts linked to a run |
| `exchange_reconciliation_cursors` | durable account/symbol/query coverage cursor and watermark |
| `execution_control_events` / `execution_control_current` | append-only kill-switch/operator audit plus rebuildable projection |
| `execution_sessions` / `execution_session_counters` | explicit server-time session and durable limit counters |
| `order_reservations` | pending quote/base/notional exposure by command; UNIQUE active reservation per command |

Exchange trades reference Portfolio journal entries, but unlike v1 `fills`, multiple
trades may reference one command. Journal uniqueness remains
`(portfolio_id, portfolio_version_after)`, and each accepted trade produces at most one
Fill transition. All financial values remain canonical Decimal TEXT; payloads carry
versioned digests; append-only event/trade/control rows reject UPDATE/DELETE.

Migration v2 is additive: Stage 3 `paper_*`, Portfolio journal/checkpoint and migration
v1 lineage remain byte-for-byte authoritative and readable. Exchange tables reference
shared Portfolio identities without renaming or rewriting paper history.

Authoritative order history is append-only. Each event has a monotonic local sequence,
aggregate state version, raw exchange status, normalized safety state, canonical digest
and unique source identity/digest. `exchange_orders_current` is rebuilt/verified from
events; transport responses never overwrite exchange truth.

Persistence recovery and exchange reconciliation are independent axes:
`CLEAN | RECOVERED | RECOVERY_REQUIRED` for local integrity and
`NOT_RUN | RUNNING | CLEAN | REQUIRED` for exchange truth. Local recovery success does
not grant exchange submission readiness.

## 18. Crash/restart matrix

| Boundary | Durable truth after crash | Permitted recovery |
|---|---|---|
| Before command persistence | no command/order | recompute only from a valid decision |
| After Phase A, before attempt marker | `PREPARED` | revalidate controls/versions, then perform first attempt |
| After `SUBMITTING` commit, before network call | attempt is potentially ambiguous | query/reconcile; never infer safe resubmit |
| Request transmitted, response lost | `SUBMITTING` | set/recover reconciliation-required; query stable client ID |
| Acknowledgement received, before persistence | `SUBMITTING` | query and persist authoritative observation |
| Exchange partial fill, before local ingestion | local order behind exchange | query trades; idempotently settle missing trade |
| Trade persisted and committed, before memory sync | DB is authoritative | restart and restore; do not reapply/re-submit |
| Portfolio preview before settlement commit | no local financial commit | rollback; re-read exchange trade and retry same deterministic Fill |
| Terminal exchange state before all trades ingested | unsettled terminal observation | keep reconciliation-required until trade totals settle |
| Terminal state fully committed | immutable settled order | duplicate observations are no-op; conflicts fail closed |
| During reconciliation | incomplete durable session | resume from durable cursor/coverage; submissions remain blocked |

The conservative rule treats an attempt marker as possible transmission even if the
process may have died before the actual socket write. Availability is sacrificed to
prevent duplicate orders.

## 19. Lifecycle and readiness

TESTNET startup order:

```text
persistence and exclusive writer lock
-> schema v2 validation/migration and independent local recovery
-> validate exact endpoint/credentials without private side effects
-> restore controls/sessions/orders/cursors/reservations
-> create read-only account client
-> reconcile rules/server time/account/orders/trades/balances
-> persist successful reconciliation generation
-> explicit operator capability (only if durable switch is disarmed)
-> construct/enable side-effect gateway
-> subscribe exchange consumer
```

Shutdown first blocks new preparation, stops decision delivery, reconciles known
in-flight operations within a bounded policy, persists status/cursors, closes the
private adapter, then follows existing market/persistence cleanup. Failure to complete
reconciliation is reported as not-ready, never as successful cancellation or settlement.

Readiness has separate dimensions: market ingestion, shadow, persistence integrity,
exchange connectivity, reconciliation, controls, and submission permission. Health does
not imply permission to submit.

Safe status exposes only `execution_mode`, process-local submission enabled/disabled,
kill-switch state/generation, reconciliation status/generation/time, non-terminal order
count, last client ID, normalized exchange status and failure counters. It never exposes
keys, signatures, secret headers, signed URLs, or unnecessary raw private responses.

## 20. Test strategy and Stage 4 completion criteria

Before Stage 4 completion, all of the following are required:

- deterministic models, client IDs and adapter translation unit tests;
- contract tests for fake gateway/account readers and Binance Testnet adapter;
- fake-exchange lifecycle tests for acknowledgement, rejection, partial fill and expiry;
- multiple partial fills, duplicate/out-of-order observations and conflicting duplicates;
- every ambiguous-submit and crash-matrix boundary;
- restart recovery with prepared, submitting, acknowledged and partially filled orders;
- startup/runtime reconciliation, cursor pagination and balance conflicts;
- writer exclusion and concurrent operation serialization;
- exact Portfolio application once per exchange trade;
- quote-fee accounting and fail-closed base/third-asset fee tests;
- stale market/rules, pending exposure, turnover/loss/drawdown and kill-switch tests;
- secret-redaction and endpoint-allowlist tests;
- proof that LIVE always fails before private client construction;
- controlled Testnet BUY and SELL with persisted exchange trades;
- all ambiguous cases A–F, including repeated not-found without resubmission;
- account reset/manual activity/balance mismatch and unsupported-fee settlement blocks;
- explicit session/counter and restart-lost submission-capability tests;
- final crash/restart/duplicate-order safety review;
- controlled testnet smoke with no live endpoint usage or secret leakage.

No default automated test may contact Binance. Real Testnet tests require explicit
operator invocation, bounded notional, preflight kill-switch/reconciliation checks and
artifact review.

Acceptance layers are fixed before implementation:

1. pure models prove legal transitions, canonical digests, client IDs and Risk context;
2. fake ports prove A–F classification, same-ID behavior and transport/truth separation;
3. SQLite fault injection proves every transaction/crash boundary and projection rebuild;
4. ambiguity suites prove timeout/5xx/reset/crash/NotFound never duplicate submission;
5. partial-fill suites prove durable trade identity, exactly-once Portfolio settlement,
   unsupported fees, balances, cursors and restart reconciliation;
6. separately authorized Testnet smoke proves bounded BUY/SELL/restart behavior and
   artifact redaction. Layers 1–5 are offline; layer 6 cannot be a default test.

## 21. Live trading boundary

Stage 4 completion means only that the isolated TESTNET path met its criteria. LIVE
remains structurally unsupported. A later live-readiness review must independently
approve operational ownership, capital/loss limits, credentials and permissions,
alerts, reconciliation evidence, incident/cancel procedures, backup/restore, dependency
and secret security, testnet observation sample, and an explicit bounded rollout.

No testnet configuration, credential, database lineage, endpoint override or ordinary
feature flag may be reused to opt into LIVE.

Future tests must prove LIVE mode and production private hosts are rejected before
private-client construction, no LIVE adapter/capability is registered in the Stage 4
composition root, Testnet credentials cannot satisfy LIVE configuration, and neither
kill-switch disarm nor endpoint override creates LIVE authority.

## 22. Accepted ADRs

- ADR-055: Exchange execution uses a durable multi-state order lifecycle.
- ADR-056: Client order identity derives deterministically from durable command identity.
- ADR-057: Ambiguous submission requires reconciliation and forbids blind resubmission.
- ADR-058: Exchange-reported trades are authoritative execution facts.
- ADR-059: Exchange command and client ID commit before network submission.
- ADR-060: Startup reconciliation gates new exchange submissions.
- ADR-061: A durable kill switch blocks submission but permits reconciliation.
- ADR-062: TESTNET and LIVE use structurally separate configuration and credentials.

ADR-055–ADR-062 passed final safety review and were accepted on 2026-08-09. Their
normative detail is this document; M4.3 must not broaden it implicitly.

## 23. Verified Binance contracts and residual preconditions

Reviewed on 2026-08-11 against official Binance Spot documentation:

- [REST API, signing, timing, errors, order/query/trade/account contracts](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)
- [Official order-status enums](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md)
- [Official symbol/exchange filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md)
- [Spot Testnet scope and endpoints](https://github.com/binance/binance-spot-api-docs)
- [Client-order-ID character/length contract published for Spot FIX](https://github.com/binance/binance-spot-api-docs/blob/master/fix-api.md)
- [Spot API change log](https://github.com/binance/binance-spot-api-docs/blob/master/CHANGELOG.md)

The REST order table does not independently state a client-ID length/charset and the API
provides no atomic absence proof. M4.5 must validate the conservative 32-character ID
with authenticated `POST /api/v3/order/test` before any Testnet order. Numeric risk
thresholds, reconciliation windows/rate budget and non-secret account-fingerprint
provisioning are mandatory operator configuration, not architecture guesses.

The only private REST base allowed is exact scheme/host `https://testnet.binance.vision`
with `/api` routes; redirects, ports, userinfo and arbitrary path/base overrides fail
closed. `/sapi` is unavailable on Spot Testnet. Demo Mode is distinct and is forbidden.
Signed calls use dedicated Testnet keys, `X-MBX-APIKEY`, HMAC-SHA256 parameter signing,
server time, and a bounded `recvWindow` (Binance maximum 60,000 ms). Rate-limit 429/418
responses honor `Retry-After` and cannot be classified as order rejection without
authoritative order evidence.

Production composition creates that private client only in explicit Testnet mode. The composition
root owns its SQLite adapter, private HTTP client, Binance adapter, reconciliation, settlement,
authoritative application service and consumer; injected resources remain caller-owned. Startup
fails closed unless reconciliation, settlement and the operator-controlled kill switch produce a
typed READY result before capability minting and coordinator subscription.

Schema v4 also persists append-only reconciliation coverage evidence. Settlement requires a
complete lower bound at the execution-root creation time and a complete upper bound through the
reconciliation start, with the evidence payload/digest, root, account, session and generation all
matching. A SAFE label by itself is not settlement authority. Trade order is deterministic by
`(traded_at_ms, exchange_trade_id)`; incomplete chronology stays PENDING.

The safety-matrix pass additionally enforces one durable command per decision-slot key, so a
corrected historical fingerprint conflicts instead of creating a second order. Identical economic
decisions return the existing command/client ID before exchange Risk or submission. Financial Risk
uses a non-mutating current-market valuation preview; durable Portfolio authority remains the last
settled exchange state until an atomic settlement commits.

### M4.5 implementation status

Status: **completed; final adapter safety review passed** (2026-08-11). Official Spot REST
contracts were reverified on 2026-08-11. The isolated
`app.infrastructure.binance.BinanceSpotTestnetAdapter` implements the M4.3 gateway and
account-reader ports for the exact Testnet base URL only. It uses injected time,
`httpx.Client` and a read-only local identity resolver; the caller owns the client.

Signed parameters use ordered URL/form encoding and lowercase HMAC-SHA256. MARKET submit
uses base `quantity`, the durable `newClientOrderId`, and ACK response type. Submit has
no retry: timeout, connection uncertainty, HTTP 5xx, malformed/lost ACK and Binance
`-1007` become ambiguous observations. Explicit maintained request rejections, auth,
timestamp and rate-limit responses remain separate typed errors. Reads normalize orders,
trades, balances, open orders and the currently representable MARKET filters without
retaining private raw payloads. Unknown status, missing durable identity or unsupported
filter semantics fail closed.

All M4.5 verification is deterministic `httpx.MockTransport`; no DNS, authenticated
smoke or Testnet order was attempted. Runtime settings/wiring, credentials, migration v2,
durable lifecycle persistence, reconciliation and LIVE remain out of scope. The required
authenticated `/api/v3/order/test` client-ID preflight remains a future controlled smoke
precondition, not a default test.

The final review additionally proved exact sign/send byte equivalence, duplicate and
reserved-parameter rejection, no redirect forwarding, conservative ambiguity for transport,
5xx, `-1007`, 3xx and malformed acknowledgements, and read-only adapter reuse after ambiguity.
The caller owns client lifecycle plus proxy/TLS policy; the adapter requires the exact HTTPS
Testnet base URL, never exposes a `verify=False` option and explicitly disables redirects on
every call. The synchronous adapter has no thread-safety guarantee; sequential reuse is the
reviewed contract.

### M4.6 implementation status

Status: **completed; final crash/restart safety review passed** (2026-08-11). Migration
`002_exchange_execution.sql` adds immutable Testnet execution roots, sessions, durable
control state, order projections, append-only order events, pre-transport submission attempts,
authoritative exchange trades and reconciliation-record foundations without changing v1 paper
tables. Financial values and external numeric IDs are stored as canonical TEXT.

`DurableExchangeOrderLifecycle` depends only on gateway and persistence protocols. It commits
PREPARED before any possible side effect and atomically commits the first attempt, SUBMITTING
event/projection and session submitted counters before calling the fake gateway. Only PREPARED
is locally eligible for first submission. SUBMITTING, ACKNOWLEDGED, PARTIALLY_FILLED and
RECONCILIATION_REQUIRED require future M4.7 reconciliation after restart; terminal states never
return to PREPARED. ACK does not create a trade or Portfolio Fill. Exchange trades, including
base/BNB commission, are retained with PENDING settlement; Portfolio settlement is deliberately
outside M4.6.

Schema v2 and the lifecycle remain absent from runtime composition. Production polling stays
paper-only, no private adapter is instantiated, and M4.6 performs no network request.

The final review made preparation replay independent of the caller's later timestamp, restricts
submission attempts to ordinal 1 at SQL level, makes exact ACK/status replay a no-op, validates
malformed gateway observations as reconciliation-required, and revalidates root/event/order/
session authority during local recovery. Deterministic failpoints prove rollback after each
attempt/status write boundary. An exception after a successful SQLite COMMIT is deliberately not
interpreted as rollback: a fresh read observes SUBMITTING or the committed terminal/result state,
so blind resubmission remains impossible. Durable financial addition and trade summation are
independent of the process-global Decimal context.

## 24. M4.2 decision

### M4.7 implementation status

Status: **completed; final reconciliation safety review passed** (2026-08-11).
`ExchangeReconciler` зависит только от lifecycle persistence и read-only account-reader ports.
Он queries original client ID, validates immutable lineage, uses the M4.3 lifecycle validator,
persists normalized trades, and checks open orders, recent trade ownership, BTC/USDT balance
scope and configured rules. NOT_FOUND never authorizes resubmission. Unknown/conflicting facts
block SAFE; reader failures do not advance generation. Empty/PREPARED-only roots may be SAFE;
terminal-complete orders are not repeatedly queried.

Migration 003 adds digested current state and append-only cycle records without editing migration
002. Truthful facts may commit before the final cycle; SAFE evidence and generation advance in
one transaction. Kill switch, submission counters and turnover remain unchanged. Settlement,
Portfolio synchronization, scheduler/runtime wiring, credentials, authenticated calls,
cancellation and LIVE are absent. M4.8 is next; Stage 4 remains in progress and ADR-055–062
remain accepted.

An M4.7 `SAFE` generation proves only normalized exchange truth for one explicit cycle: every
required attempted-order query completed, fetched trade totals were complete, managed open-order
facts agreed, account-wide trade history was complete from execution-root creation through cycle
start, requested BTC/USDT balances were valid, and effective normalized rules matched. Each new
successful invocation is a new audited cycle and advances exactly once; exact replay of an already
persisted run ID/payload is a no-op. Unsafe cycles append evidence but retain generation.

`SAFE` does **not** mean Portfolio settlement, submission readiness, cleared kill switch, Risk
approval, process capability, control-generation stability or LIVE authorization.
`ExchangeReconciliationResult.submission_ready` is therefore always false in M4.7. Newly found
trades remain PENDING settlement even when exchange truth is SAFE. M4.8 must first synchronize
settlement, recheck local recovery and balances, bind the current reconciliation and control
generations, confirm kill-switch/Risk gates, and only then may issue a process-local capability.
The final M4.7 record does not freeze control generation; M4.8 must re-read it.

Binance trade-history reads request the root-start horizon with the maximum bounded page. A page
at the limit is explicitly incomplete and blocks SAFE; it is not described as full account
history. Dedicated Testnet lineage must therefore remain within provable history or later add
pagination/checkpoint policy. Balance validation in M4.7 proves normalized non-negative requested
assets only: it does not prove Portfolio equality or explain locked funds.

M4.1 architecture design and M4.2 final safety review are complete. The design is
approved for M4.3 domain/port implementation only. This review implemented no code,
migration, credential, private API call, Testnet order or LIVE capability.

### M4.8 implementation status

Status: **completed; final safety review passed** (2026-08-12). Migration 004
adds a durable exchange Portfolio root/current projection and append-only per-trade settlement
records. USDT-fee trades settle exactly once through Portfolio preview, atomic journal/current/
session-turnover commit, then memory synchronization. Other fee assets fail closed without
inventing conversion. Runtime startup executes recovery -> reconciliation -> settlement ->
readiness and issues authority only after re-reading current durable generations. The authority
is TESTNET-only and binds account and session lineage. LIVE, cancellation and authenticated smoke
remain absent; M4.9 is completed and its final crash/restart safety review passed. M4.10 security
preflight safety review passed, Phase 1 and Phase 2 PASS, and Phase 3 schema v5 and its final
adversarial review are complete but execution is unauthorized; Stage 4 remains in progress.

The completion pass makes the network boundary accept only a single-use `PreSubmitAuthorization`
minted by `TestnetPreSubmitGate`. The gate performs a final durable reread and rejects stale Risk,
capability, generations, controls, settlement, rules, session, pending exposure or order identity.
Startup readiness is deliberately insufficient by itself. Settlement recovery now verifies the
trade/settlement digest and identity chain, consecutive Portfolio versions/current head and exact
session turnover before synchronizing memory. These additions passed the M4.8 final acceptance
review.

Settlement rows are now bound by foreign key to the exact durable SAFE reconciliation run and
generation that established complete account trade-history coverage. A REQUIRED/FAILED/CONFLICT
generation leaves all newly discovered trades PENDING; deterministic timestamp/trade-ID ordering
is applied only after that coverage barrier is durable.

Production composition treats runtime availability and submission readiness as separate axes.
Once local durable authority is valid, an ordinary readiness blocker produces `RECOVERY_ONLY`:

```text
TESTNET runtime
├── RECOVERY_ONLY: persistence + reconciliation + settlement + status; no capability/submit path
└── READY:         fresh process-local capability + application service + Testnet consumer
```

Kill-switch, unresolved/non-terminal order, incomplete reconciliation and settlement blockers do
not tear down the recovery graph. Corruption, incompatible schema/root identity, writer conflict,
invalid Testnet configuration and LIVE remain fatal. A Testnet-only recovery graph omits the
decision coordinator, so executable intents cannot accumulate PREPARED orders. There is no
same-process promotion in M4.8: resolving the blocker requires a fresh startup, which repeats the
ordered recovery checks and may mint a new generation-bound capability. ADR-063 is accepted;
M4.9 is completed and its final crash/restart safety review passed. M4.10 security preflight
review passed, Phase 1 and Phase 2 PASS, while Phase 3 schema v5 and its final adversarial review
are complete but execution is unauthorized; Stage 4 remains in progress.

Full-graph acceptance uses fresh `build_application()` objects for every simulated process while
sharing only the SQLite file and deterministic fake exchange. The suite proves lost ACK A/B/C,
FILLED and PARTIALLY_FILLED settlement recovery, five consecutive ambiguous NOT_FOUND restarts,
and a lost ambiguity-status persistence result. In every case the durable command, client ID,
attempt and submission counters remain singular and no startup performs a replacement submit.

### M4.9 implementation/review status

Status: **completed; final crash/restart safety review passed** (2026-08-13). M4.9 adds only
adversarial evidence. Twenty NOT_FOUND production restarts, later FOUND resolution, repeated
same-process and cross-process decisions, recovery-only/READY restart stress and deterministic
fixed-seed schedules preserve one durable command/client ID/attempt and at most one gateway call.
Existing transaction failpoint matrices cover all four exception families around submission,
status, reconciliation and settlement commits. No new ADR or exchange capability was required.
See `M4_9_CRASH_RESTART_REPORT.md` for the preceding offline review. M4.10 subsequently completed
its bounded BUY, settlement and fresh-process recovery proof. M4.11 is complete with
`STAGE_4_BLOCKED` because the accepted controlled SELL evidence remains absent.

### M4.10 controlled smoke design status

Status: **M4.10 COMPLETED — PASS; M4.11 COMPLETED — STAGE_4_BLOCKED** (2026-08-14). The authorization ladder in
`M4_10_TESTNET_SMOKE_PLAN.md` keeps authenticated reads, `/api/v3/order/test`, one real Testnet
order and restart reconciliation under distinct approvals. Central dependency-log suppression and
an explicit no-fallback order-test adapter contract are implemented. The Phase 0 read-harness gap
is resolved by the separate `app.tools.testnet_phase1` entrypoint: its four-operation facade and
fixed-host transport enforce the GET whitelist and five-request ceiling. AST/import and
MockTransport proofs exclude trader composition, persistence, submission capability/gate/lifecycle,
real submit and order-test calls. It writes sanitized evidence and exits. Audited kill-switch/smoke
commands remain later prerequisites. Phase 1 used exactly four GETs and produced no order-capable
call or durable/financial mutation. Its authorization is consumed. Phase 2 later passed under its
own separate authorization.
The separate `python -m app.tools.testnet_phase2` entrypoint requires the exact
`M4_10_PHASE2_AUTHORIZED=YES` token. Its capability-narrow facade exposes only
`test_market_order`, while its transport allows exactly one fixed-host
`POST /api/v3/order/test`; it rejects real submission, cancellation, alternate endpoints/hosts
and a second request before transport. The reviewed Phase 2-only BUY quantity `0.00016000` BTC is
validated with `Decimal` against the public reference price `63849.22000000` and preserved Phase 1
rules. No persistence, Portfolio, lifecycle, reconciliation, settlement or normal trading graph is
composed. Phase 2 passed with exactly one test-endpoint POST, accepted signing/request-contract
semantics and no real order or financial mutation.

Phase 3 completed its one-shot ACK-level submission. The implemented and finally reviewed offline
one-shot authority graph passes through Risk, durable
identity, PREPARED, final gate and SUBMITTING and a first-order risk envelope capped at one
submission/non-terminal/in-flight order. The Testnet
account's pre-existing 1 BTC is not managed history: production Portfolio composition starts at
zero base, and current balance inspection does not establish ownership. ADR-064's accepted
quote-funded subledger records the external baseline without importing it. The pre-existing BTC
must not be sold automatically.

ADR-064 is accepted by the final Phase 3 design review. It specifies a zero-base, `25 USDT` managed allocation and a
durable Phase 3 authority tied to the exact root/account/session, control/reconciliation
generations, operator run, economic slot, command, rules/configuration and canonical envelope and
balance-scope digests. One `BEGIN IMMEDIATE` transaction inserts and consumes the authority, creates
the only PREPARED command/event/reservation and commits their one-to-one audit link. The global
switch remains ACTIVE; only the returned in-memory exact-command claim can satisfy the final gate.
Crash loses it, so restart omits capability and retains reconciliation authority for the same
identity. Schema-v5 persistence and the Phase 3 operator are implemented. Ordinary
CLEARED state is forbidden for Phase 3.

Phase 4 is a separate GET-only recovery and settlement graph over the same database and identity. Its
offline harness and final adversarial safety review are complete; execution authorization remains
pending. It cannot submit, replace, or cancel. Authoritative fill/settlement proof remains pending.

The first production Phase 4 observation reached SAFE reconciliation but remained INCOMPLETE because
the authoritative fill reported `commission=0` with `commission_asset=BTC`. ADR-065 accepts only exact
finite Decimal zero as asset-neutral. It preserves that BTC label, applies no fee or conversion, and
requires a schema-v6 append-only resolution of the existing blocked record. Positive USDT fees retain
existing accounting; positive BTC/BNB/other fees remain unsupported. Completion is a durable global
predicate—terminal order, complete base and quote trade truth, SAFE complete coverage, no unresolved
pending/block, and synchronized Portfolio/session—not a test of newly blocked rows in one invocation.
Schema-v6 and the offline resolution service now implement this predicate and append-only policy; the
final offline safety review passed; the separately authorized production remediation then completed
with one resolution and globally synchronized Portfolio/session truth.
The dedicated remediation composition imports no exchange adapter or HTTP client. Authorization and
evidence collision checks precede writable database acquisition; after opening schema v6 it validates
the sole Phase 3 authority/order/trade/block, exact zero-BTC fee, SAFE complete reconciliation and the
zero-BTC/25-USDT version-zero managed root before calling the existing settlement service. An exact
already-resolved state returns idempotent PASS, while any conflicting state fails closed.
The harness final offline safety review passed, including migration interruption, concurrent writer,
lost-commit-acknowledgement and partial-evidence-write proofs.

The final fresh-process proof is a separate observation-only composition. It accepts only its own exact
authorization, fixes the production database/evidence paths, opens SQLite with `mode=ro`, `immutable=1`
and `query_only`, validates the authority/audit/order/event/attempt/trade/block/resolution/reconciliation/
Portfolio/session digest chain and requires an identical database checksum after close. It has no
transport, credential, submission, cancellation or settlement mutation dependency. Its implementation
and final offline adversarial safety review are complete. Its separately authorized production run
passed and completed M4.10. M4.11 aggregation issued `STAGE_4_BLOCKED`: the durable BUY path satisfies
the reviewed safety and recovery properties, but the accepted controlled BUY-and-SELL criterion still
lacks SELL evidence. The managed-balance SELL closure and Stage 5 have not started.

The final closure schema boundary implements the accepted architecture: use the same durable
root/session/Portfolio, exact full managed inventory only when fresh effective MARKET rules accept it,
and a new append-only schema-v7 authority consumed atomically with one PREPARED SELL command. The
exchange account total is reconciliation evidence and cannot satisfy Risk. ACTIVE control remains
durable; a distinct process-local claim permits only that exact command and is lost on restart.
ADR-066 was accepted on 2026-08-14, authorizing offline schema/domain/persistence implementation and
tests only; production migration and SELL execution remain separately unauthorized. Migration 007,
typed SELL authority/request/claim models, atomic PREPARED creation, recovery-only restart behavior,
and the narrow final-gate exception are implemented offline; their final adversarial schema review
passed. No network operator is included, so Stage 4 remains BLOCKED on controlled SELL evidence.
See `STAGE_4_MANAGED_SELL_CLOSURE_DESIGN.md`.

The committed lifecycle has one terminal state, CONSUMED. Transaction-local PREPARED cannot become
visible without the linked command; stale authorization fails before insert, so EXPIRED/CANCELED
states are unnecessary. UNIQUE authority ID, operator run, economic slot and command linkage prove
one authority/command. BUY/BTCUSDT/MARKET, count ceilings, `12 USDT` caps, `25 USDT` allocation,
`13 USDT` reserve, fee/slippage assumptions and rules fingerprint are canonical bound facts. SELL
fails before persistence. Authority consumption occurs at PREPARED commit, never transmission.
