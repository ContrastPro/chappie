# Exchange Execution

## Scope

This is the canonical execution contract for the active Chappie MVP. It
describes the normal continuous Binance Spot **Testnet** path from an approved
decision through a durable local order lifecycle, authoritative exchange
observations, settlement, and the managed Portfolio. It does not authorize an
order, guarantee exchange-side exactly-once execution, add a cancellation path,
or authorize LIVE trading.

The active path is deliberately separate from Paper and from the historical
M4.10 / Stage 4 one-shot work:

```text
strategy signal
  -> planner intent
  -> financial Risk approval
  -> durable command + PREPARED order
  -> final pre-submit gate
  -> durable SUBMITTING attempt
  -> at most one exchange POST
  -> reconciliation of authoritative exchange facts
  -> durable settlement
  -> managed Portfolio projection
```

Related contracts are deliberately not repeated here:

- [Trading decisions](TRADING_DECISIONS.md) owns strategy, Planner, and Risk
  decision semantics.
- [Risk](RISK.md) owns deterministic sizing and approval envelopes.
- [Decision runtime](DECISION_RUNTIME.md) owns shared delivery and process-local
  `SAFE_STOP`.
- [Persistence](PERSISTENCE.md) owns durable storage, recovery, SQLite
  ownership, and projection verification.
- [Paper trading](PAPER_TRADING.md) defines the local non-network provider.

## Safety boundary

Only the ready Testnet execution graph may cross the private order-submission
boundary. Strategy, Planner, Risk, Shadow, status publishing, and monitoring
cannot submit an order. They produce, assess, observe, or report facts only.

`TestnetExecutionApplicationService` is the active decision-to-order boundary.
It accepts an already validated shared decision and market snapshot, invokes the
ordinary financial and exchange Risk layers, persists a command/order through
`DurableExchangeOrderLifecycle`, and asks `TestnetPreSubmitGate` to perform the
last durable reread. The gate is the only path that can issue the one-use
pre-submit authorization consumed by the lifecycle immediately before the
network call.

The composition root owns the SQLite store, HTTP client, Binance adapter,
reconciler, settlement service, pre-submit gate, application service, and
consumer. Injected clients remain caller-owned; the production composition
closes resources it owns. A market-data endpoint, a strategy setting, or a
successful public market poll does not confer private execution authority.

## Active Testnet MVP contract

The normal continuous Testnet MVP has these fixed boundaries:

| Concern | Active contract |
| --- | --- |
| Environment | Binance Spot Testnet only; `ExecutionEnvironment` has only `TESTNET`. |
| Schema | Exchange schema **v4** (`ACTIVE_MVP_EXCHANGE_SCHEMA_VERSION`). Fresh normal startup applies/validates migrations 001–004 only. |
| Database | Disposable Testnet store, default `data/testnet_mvp.sqlite3`; the historical M4.10 database path is rejected. |
| Instrument | Current models constrain the initial exchange symbol to `BTCUSDT`, MARKET BUY/SELL with base quantity. |
| Startup | local recovery → reconciliation → settlement → readiness → submission graph only when `READY`. |
| Readiness | enabled mode, clean local recovery, SAFE reconciliation, synchronized settlement, CLEARED durable kill switch, active session, and no non-terminal order. |
| Submission | a fresh process-local capability bound to account/session/reconciliation/control generations plus current Risk and final-gate validation. |
| Failure | unsafe startup produces a non-submit-capable `RECOVERY_ONLY` graph or process-local `SAFE_STOP`; no automatic retry. |
| LIVE | unavailable: configuration rejects `live` before private-client construction. |

Schemas v5–v10, Phase 3 authorities, M4.10 proof tooling, managed-SELL
disposition, and successor experiments are historical. They neither migrate a
fresh MVP database nor contribute normal runtime readiness. See [historical
Stage 4 material](../historical/stage4/README.md).

## Durable intent identity

Before any private POST, the active path persists an immutable `ExchangeCommand`
and `ExchangeOrder`. Durable identity includes stable decision and decision-slot
keys, the command key, account and execution-root/session lineage, symbol/side,
exact approved base quantity, Risk authorization, rules and execution
configuration fingerprints, and a deterministic client order ID.

The client ID is derived from Testnet environment, venue, non-secret account
fingerprint, and durable command key. It is persisted before the network
boundary and is reused for the first POST, order lookup, and trade
reconciliation. It is never regenerated because of timeout, restart, or a
different later market price.

The durable decision slot prevents a corrected or replayed decision from
creating a second economic command for the same decision slot. Exact durable
replay returns the existing identity; a conflicting identity or payload fails
closed. Runtime correlation IDs are audit links, not exchange identity.

## Pre-submit lifecycle

The active lifecycle state names are:

```text
PREPARED
  -> SUBMITTING | RECONCILIATION_REQUIRED
     -> ACKNOWLEDGED | REJECTED | RECONCILIATION_REQUIRED
ACKNOWLEDGED
  -> PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | RECONCILIATION_REQUIRED
PARTIALLY_FILLED
  -> PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | RECONCILIATION_REQUIRED
RECONCILIATION_REQUIRED
  -> ACKNOWLEDGED | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED | EXPIRED
```

`PREPARED` is a durable local request and reservation: it proves neither
transmission nor exchange acceptance. `SUBMITTING` and ordinal-one submission
attempt evidence commit before the gateway is called. `ACKNOWLEDGED` binds a
valid exchange order ID but does not create a Fill. `PARTIALLY_FILLED` and
`FILLED` are advanced only from normalized authoritative observations and trade
facts. `RECONCILIATION_REQUIRED` is non-terminal, fail-closed, and cannot submit
again.

The model also retains `DISPOSED_NO_EXECUTION`, but that state belongs to the
historical v9 disposition boundary and is not part of the active schema-v4
normal lifecycle. It is not a cancel/reject/exchange state.

Terminal exchange states (`FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`) never
return to `PREPARED`. Their remaining trade and settlement completeness is still
verified separately; a terminal order observation alone is not proof that the
managed Portfolio is settled.

## Submission capability and final gate

Readiness mints a non-copyable, non-serializable, process-local
`SubmissionCapability` only when every current durable gate is safe. It binds
the Testnet account fingerprint, active session, reconciliation generation, and
kill-switch control generation. Restart destroys it. A durable `CLEARED` switch
alone is not enough to submit.

Immediately before the side effect, `TestnetPreSubmitGate` rereads and checks:

- clean local recovery and settlement authority;
- TESTNET order environment, root/session/account/client identity and PREPARED
  status;
- SAFE reconciliation and matching durable control/reconciliation generations;
- synchronized/no-blocked settlement state;
- active session and no competing non-terminal/in-flight order;
- rules/configuration fingerprint and authorization age;
- current Risk assessment/context, pending exposure, and the process-local
  capability.

Any failed check returns typed block reasons and performs no network I/O. A
successful gate issues an internal one-use pre-submit authorization. The
lifecycle consumes it, commits `PREPARED → SUBMITTING` and the first attempt,
then constructs the exact MARKET request. Neither the final gate nor Risk may
invent a replacement command, client ID, quantity, reservation, or order.

## At-most-one POST guarantee

The active guarantee is **at most one local exchange POST per durable command**,
not an assertion of exactly-once exchange execution. Its boundary is:

```text
durable command + deterministic client ID committed
  -> final gate approves current facts
  -> SUBMITTING + submission attempt #1 commit
  -> one gateway POST may occur
```

Only `PREPARED` is eligible for a first submission. The persistence layer limits
the attempt to ordinal one and rejects a second `begin_submission`. A crash or
lost acknowledgement after that commit is conservatively treated as potentially
transmitted, even if the process may have stopped before a socket write. This
sacrifices availability rather than permitting a duplicate order.

The lifecycle performs no retry loop. A deterministic exchange rejection records
`REJECTED`; malformed acknowledgement, uncertain transport, lost response,
connection reset, timeout, HTTP 5xx, or unknown transmission records
`RECONCILIATION_REQUIRED`. A valid acknowledgement records `ACKNOWLEDGED` with
the exchange order ID. The only local no-transmission category is an adapter
observation that proves the request was definitely not transmitted; it does not
create a second command or client ID and is not permission for blind recovery.

## Ambiguous submission and reconciliation

An unknown remote outcome is not absence. The service therefore never treats a
timeout, disconnect, malformed/lost response, process interruption, or
`NOT_FOUND` result as authorization to POST again. It keeps the same durable
identity and asks `ExchangeReconciler` to query bounded authoritative facts.

Reconciliation reads the configured account's order by client ID, open orders,
order trades, account-wide/recent trade coverage, balances, and effective
instrument rules. It validates local/exchange identity before advancing a local
order, persists normalized exchange trade facts idempotently, and records a
durable reconciliation run. A generation advances only on `SAFE` evidence.

The following are fail-closed examples: incomplete history coverage, reader
failure, ambiguous/missing/conflicting order facts, unknown open order/trade,
balance-scope disagreement, and rules mismatch. `NOT_FOUND` keeps the result
unsafe; it is not a proof that an order was never received. Persistence never
fabricates an exchange order, rejection, cancellation, trade, or fill from a
local status or transport result.

## Settlement and Portfolio authority

Exchange-reported normalized trades are the sole Testnet financial authority.
Acknowledgement fields, local reference prices, cumulative quantities, and order
state never manufacture a Portfolio Fill. Exchange trade identity is durable and
unique; an exact duplicate is idempotent, while conflicting facts for the same
identity fail closed.

`SQLiteExchangeSettlement` accepts pending trade facts only under a current SAFE
reconciliation generation with complete coverage. It validates the exchange root,
session, reconciliation linkage, canonical payload/digests, trade identity,
Portfolio version chain, and session turnover linkage. A settleable trade is
applied through one atomic durable transaction that records settlement, advances
the managed Portfolio projection, and advances session economic counters before
in-memory synchronization.

Unsupported/incomplete/conflicting settlement facts remain durable evidence but
block submission readiness; no fee conversion, synthetic Fill, balance repair, or
manual Portfolio adjustment is performed. After a committed transaction, a
memory-sync failure degrades the runtime: reopening/recovery reads durable truth
and must not reapply the trade.

## Non-terminal state, restart, and SAFE_STOP

Normal Testnet startup is ordered and cannot skip unsafe work:

```text
open exclusive schema-v4 store
  -> local durable recovery validation
  -> read-only/private reconciliation
  -> settlement initialization and pending settlement
  -> readiness evaluation
  -> READY: mint fresh capability and compose consumer
     otherwise: RECOVERY_ONLY, with no consumer/capability
```

`PREPARED`, `SUBMITTING`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, and
`RECONCILIATION_REQUIRED` prevent normal readiness until their durable/exchange
truth is resolved. Local recovery and reconciliation are distinct: a consistent
SQLite graph does not prove exchange truth, and SAFE reconciliation does not by
itself prove settlement or grant a capability.

An unexpected ordinary exception from `TestnetExecutionConsumer` puts the shared
`TradingDecisionCoordinator` into process-local `SAFE_STOP`. It stores a
sanitized exception category, ignores later `CandleClosed` events, rejects
restart of that coordinator instance, and exposes submission disabled in server
status. It does not repair durable state, infer a remote outcome, retry a POST,
or clear the durable kill switch. A fresh process/operator action must rerun the
ordered startup checks. Paper and Shadow are separate from this Testnet-specific
containment rule.

Startup composition/readiness failure also publishes a sanitized `SAFE_STOP`
server state before polling/submission. Graceful shutdown requests stop, blocks
new work, closes owned resources, and publishes `STOPPED`; it does not promise a
cancel or settlement of unresolved exchange state.

## Failure taxonomy

| Class | Consequence |
| --- | --- |
| Deterministic Planner/Risk decline | No durable executable command or POST. |
| Pre-submit validation/gate failure | Typed rejection; no attempt marker or POST. |
| Persistence failure before commit | Transaction rolls back; reread/recovery decides durable truth. |
| Failure after `SUBMITTING` commit, before/during/after network boundary | Potentially transmitted; reconciliation only, no resubmit. |
| Definite exchange rejection | Durable `REJECTED`; this command is not retried. |
| Valid ACK | Durable `ACKNOWLEDGED`; query/settle authoritative trade facts. |
| Timeout, reset, 5xx, malformed/lost response, unknown transport | `RECONCILIATION_REQUIRED`; retain identity and reconcile. |
| Reconciliation incomplete/conflict/failure | No SAFE generation/capability; remain blocked or recovery-only. |
| Settlement incomplete/unsupported/conflict | Preserve trade evidence; block readiness and do not mutate Portfolio speculatively. |
| Unexpected Testnet consumer failure | Process-local `SAFE_STOP`; later market events do not reach execution. |

## Credentials, network, and mode boundary

Paper execution is local and requires no trading credentials or real exchange
POST. Testnet private calls are isolated in `BinanceSpotTestnetAdapter`, which
uses the exact `https://testnet.binance.vision` base URL, dedicated Testnet
credentials, signing, bounded time/receive-window handling, response validation,
and redaction at exception/logging boundaries. Credentials are `SecretStr`
configuration values and must not appear in durable payloads, logs, status,
diagnostics, fixtures, headers, signatures, or signed URLs.

Research and public market-data tooling are not execution authority and must not
use private execution endpoints. LIVE is unavailable by configuration and has no
composition path. Testnet and Paper are mutually exclusive financial consumers;
Shadow may observe either but cannot authorize execution.

## Observability and verification

Server status publishes only sanitized process facts: mode, lifecycle, schema,
polling state, reconciliation/settlement status, submission enabled/disabled,
and a sanitized safe-stop reason. It is an atomic status snapshot, not a
competing SQLite financial reader and not a source of execution authority.

The active test suites cover deterministic identity/lifecycle transitions,
persist-before-POST and one-attempt constraints, malformed/ambiguous outcomes,
reconciliation completeness and conflicts, exact-once trade settlement,
restart/crash boundaries, pre-submit stale capability/generation checks,
Testnet startup/readiness, SAFE_STOP, credential/endpoint restrictions, and
LIVE rejection. Default MVP readiness uses `pytest -m "not historical"`;
historical evidence suites remain explicitly runnable but do not define current
server readiness.

## Historical Stage 4 and M4.10 notes

The legacy design included a staged M4.1–M4.11 roadmap, schema-v2/v3 planning,
Phase-3 one-shot authorities, M4.10 smoke/proof evidence, settlement remediation,
managed SELL closure, recovery/disposition, and successor experiments. Those
materials are historical evidence, not active operations or startup obligations.

Their retained operator-forbidden context and evidence indexes are under
[historical Stage 4](../historical/stage4/README.md). In particular, no normal
MVP process may derive authority from historical authorization tokens, process
claims, v5–v10 tables, one-shot operators, protected databases, or a prior
M4.10 outcome. The active v4 contract above supersedes old claims about schema
v2 startup, Stage 4 completion criteria, one-shot SELL recovery, and a future
LIVE rollout.

## Source invariant crosswalk

The former `EXCHANGE_EXECUTION_DESIGN.md` was reviewed against the active code
and tests before removal. Each meaningful source section is accounted for here;
`HISTORICAL_ONLY` means preserved under the historical index/Git history rather
than copied into an operational MVP contract.

### Source invariant inventory

| Classification | Inventory disposition |
| --- | --- |
| `CURRENT_EXECUTION_AUTHORITY` | Only the ready Testnet application service, final gate, and lifecycle may reach the submit gateway; strategy/Planner/Risk/Shadow cannot. |
| `CURRENT_TESTNET_BOUNDARY` | Binance Spot Testnet, explicit mode/configuration, isolated adapter, v4 schema, and no LIVE path. |
| `CURRENT_SUBMISSION_CAPABILITY` | Fresh process-local capability plus one-use final authorization, both bound to current durable generations. |
| `CURRENT_DURABLE_INTENT` | Stable decision/slot/command/client identities, exact quantities/fingerprints, durable replay validation, no secret identity material. |
| `CURRENT_PRE_SUBMIT_LIFECYCLE` | Prepare and reserve before any side effect; final reread before the attempt transaction. |
| `CURRENT_PREPARED_STATE` | Durable local intent only; no transmission or exchange outcome is implied. |
| `CURRENT_SUBMITTING_STATE` | Attempt evidence commits before the gateway call and permanently removes local first-submit eligibility. |
| `CURRENT_POST_SEMANTICS` | One exact MARKET request may cross the gateway only after the preceding commits. |
| `CURRENT_AT_MOST_ONCE` | One durable command/client ID/attempt and at most one local POST; no claim of exactly-once remote execution. |
| `CURRENT_AMBIGUITY_HANDLING` | Unknown transport or `NOT_FOUND` is reconciliation-only and never a blind resubmission signal. |
| `CURRENT_RECONCILIATION` | Read authoritative order/trade/balance/rules facts, retain bounded coverage evidence, and advance SAFE generation only on complete consistent truth. |
| `CURRENT_SETTLEMENT` | Exchange trades are authoritative; exact-once durable settlement precedes managed Portfolio publication. |
| `CURRENT_NON_TERMINAL_STATE` | PREPARED/SUBMITTING/acknowledged/partial/reconciliation-required orders block normal readiness. |
| `CURRENT_SAFE_STOP` | Unexpected Testnet consumer/startup failure disables later submission in the process and preserves diagnostics. |
| `CURRENT_RESTART_REQUIREMENT` | Fresh process repeats local recovery, reconciliation, settlement, readiness, and capability issuance; capabilities are never reconstructed. |
| `CURRENT_IDEMPOTENCY` | Durable uniqueness and digest/identity checks make exact replays no-ops and conflicts fail closed. |
| `CURRENT_FAILURE_SEMANTICS` | Deterministic decline, pre-submit failure, durable rollback, ambiguity, reconciliation, settlement, and consumer failure remain distinct. |
| `CURRENT_SCHEMA_CONTRACT` | Active continuous Testnet requires v4 only; historical v5–v10 are excluded. |
| `CURRENT_CREDENTIAL_BOUNDARY` | Secrets are adapter-only/Testnet-only and are redacted from durable, logging, status, and research paths. |
| `CURRENT_LIVE_BOUNDARY` | `live` is rejected before private-client construction. |
| `CURRENT_OBSERVABILITY` | Sanitized atomic status reports readiness/submission state but never becomes financial/execution authority. |
| `HISTORICAL_STAGE4`, `HISTORICAL_M4_10` | One-shot authority, proof, recovery/disposition/successor, v5–v10, and completion narratives stay under the historical index. |
| `DUPLICATED` | Strategy/Planner/Risk, runtime, persistence, and Paper details are cross-linked to their canonical documents rather than duplicated. |
| `SUPERSEDED` | Proposed package layout, schema-v2 readiness, automated Case-A retry language, and a future LIVE rollout yield to current implemented behavior. |

| Source section / invariant | Destination | Status |
| --- | --- | --- |
| 1 Status, scope, Testnet-only intent, no LIVE authority | Scope; Active Testnet MVP contract | PRESERVED; milestone chronology historical only |
| 2 Existing safety objectives and mode separation | Safety boundary; Credentials, network, and mode boundary | MERGED |
| 3–4 coordinator/dependency ownership | Safety boundary; Decision runtime cross-link | MERGED; old package proposal superseded by current modules |
| 5 order facts, identity, states, async reality | Durable intent identity; Pre-submit lifecycle | PRESERVED; historical disposition state identified separately |
| 6 gateway/account-reader split | Safety boundary; Ambiguous submission and reconciliation | PRESERVED |
| 7 deterministic client identity/no secret derivation | Durable intent identity | PRESERVED |
| 8 ambiguous cases and no blind resubmission | At-most-one POST guarantee; Ambiguous submission and reconciliation | PRESERVED; old Case-A retry language is not an active automated retry path |
| 9 transaction phases and commit-before-memory | Pre-submit lifecycle; Settlement and Portfolio authority | PRESERVED |
| 10 initial MARKET/BTCUSDT scope | Active Testnet MVP contract | PRESERVED |
| 11 exchange-trade authority, fee limits, managed subledger | Settlement and Portfolio authority | PRESERVED; historical account facts not active configuration |
| 12 startup/runtime reconciliation and conflicts | Ambiguous submission and reconciliation; Non-terminal state, restart, and SAFE_STOP | PRESERVED |
| 13 Risk hardening and stale approval controls | Submission capability and final gate; Risk cross-link | MERGED |
| 14 durable kill switch and generation-bound capability | Active Testnet MVP contract; Submission capability and final gate | PRESERVED |
| 15–16 Testnet adapter, signing, configuration, credential redaction, LIVE rejection | Credentials, network, and mode boundary | PRESERVED; proposed names/details superseded by current Settings/adapter |
| 17 v2 table proposal and persistence recovery axes | Persistence cross-link; Active Testnet MVP contract | SUPERSEDED by implemented active v4 schema |
| 18 crash/restart matrix | At-most-one POST guarantee; Non-terminal state, restart, and SAFE_STOP; Failure taxonomy | MERGED |
| 19 readiness/status/shutdown | Active Testnet MVP contract; Observability and verification | PRESERVED |
| 20 test strategy/completion criteria | Observability and verification | MERGED; Stage 4 completion criteria historical only |
| 21 LIVE future boundary | Credentials, network, and mode boundary | PRESERVED as current unavailability |
| 22 ADR-055–ADR-062 acceptance list | Historical Stage 4 and M4.10 notes | HISTORICAL_ONLY |
| 23 verified Binance assumptions and M4.5–M4.8 implementation chronology | Adapter boundary; lifecycle/reconciliation/settlement sections | MERGED for current behavior; chronology HISTORICAL_ONLY |
| 24 M4.7–M4.11, Phase 1–4, remediation, SELL/recovery/successor narratives | Historical Stage 4 and M4.10 notes | HISTORICAL_ONLY |

### Code/doc conflict record

| Old claim | Current implementation/test evidence | Disposition |
| --- | --- | --- |
| Startup requires schema v2 (later v5/v6/etc. milestones) | `ACTIVE_MVP_EXCHANGE_SCHEMA_VERSION = 4`; composition passes it to `SQLiteExchangePersistence`; active persistence tests and MVP boundary tests use v4. | Superseded: v4 is the only active MVP contract. |
| Stage 4 completion and one-shot BUY/SELL work define Testnet readiness | `build_production_testnet_components` builds only the normal v4 graph; archived exports are omitted from normal runtime; `tests/test_mvp_runtime_boundary.py` covers the boundary. | Historical only. |
| A capability may be cleared through Stage 4 exception claims | Normal `evaluate_testnet_readiness` issues capability only with clean recovery, SAFE reconciliation, synchronized settlement, CLEARED control, active session, and zero non-terminal orders; historical claim compatibility is not supplied by normal composition. | Historical compatibility only; not active contract. |
| Transport Case A permits retry | `DurableExchangeOrderLifecycle` has no retry loop; `begin_submission` is one-attempt evidence and ambiguity routes to reconciliation. | Do not describe an automatic retry. |
| Legacy completion plan includes a future LIVE rollout | `Settings.validate_market_data_configuration` rejects `live`; no LIVE composition path exists. | Superseded by current structural unavailability. |

## Verification pointers

The active behavior described here is exercised principally by:

- `tests/test_exchange_persistence.py` — durable lifecycle, one attempt,
  transaction/crash behavior, and immutable terminal transitions;
- `tests/test_exchange_runtime.py` — startup/readiness, final gate,
  reconciliation, settlement, and restart safety;
- `tests/test_exchange_reconciliation.py` and adapter tests — authoritative
  observations, query coverage, identity validation, and ambiguity;
- `tests/test_trading_coordination.py`, `tests/test_application.py`, and
  `tests/test_server_status.py` — Testnet consumer `SAFE_STOP`, status, and
  process lifecycle;
- `tests/test_mvp_runtime_boundary.py` — active runtime separation from
  historical Stage 4 modules.
