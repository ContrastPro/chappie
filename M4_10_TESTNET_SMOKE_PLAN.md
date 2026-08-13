# M4.10 Controlled Binance Spot Testnet Smoke Plan

Status: **Phase 1 PASS; Phase 2 PASS; Phase 3 one-shot submission PASS; Phase 4 real reconciliation SAFE but settlement INCOMPLETE for authoritative `0 BTC` commission; ADR-065 exact-zero resolution design accepted; implementation/remediation authorization pending** (2026-08-13).

Phase 4 uses `python -m app.tools.testnet_phase4` to reopen the same smoke database. It is GET-only,
cannot reconstruct submission authority, and settles only normalized authoritative trades through the
existing exactly-once machinery. Production Phase 4 execution remains unauthorized.

The first authorized Phase 4 pass preserved one complete FILLED trade and correctly appended
`BLOCKED_UNSUPPORTED_FEE`: Binance reported exact zero commission with asset label BTC. ADR-065 accepts
exact finite Decimal zero as asset-neutral, but no production state is resolved by that decision alone.
Schema-v6 implementation, offline review, commit/push and separate same-database remediation authority
are required. No additional order, cancellation or fee conversion is permitted.

Stage 4 remains in progress. This document authorizes no network access, credential use,
additional `/api/v3/order/test` request or real Testnet order. Every phase below needs a new, explicit
operator authorization naming that phase. Approval never carries forward.

## Safety finding and prerequisites

The production adapter already restricts private execution to
`https://testnet.binance.vision`, disables redirects, signs exact transmitted parameters, uses a
5-second `recvWindow`, performs no submission retry and rejects LIVE. It implements these reads:

- signed `GET /api/v3/account` (`omitZeroBalances=false`);
- signed `GET /api/v3/openOrders?symbol=BTCUSDT`;
- signed `GET /api/v3/myTrades?symbol=BTCUSDT&startTime=...&limit=1000`;
- signed `GET /api/v3/order?symbol=BTCUSDT&origClientOrderId=...` for known durable orders;
- public `GET /api/v3/exchangeInfo?symbol=BTCUSDT`.

The infrastructure adapter now implements an isolated `test_market_order()` contract for
`POST /api/v3/order/test`; it is not wired to persistence or the durable submission lifecycle. A
bounded authenticated-read command, audited operator kill-switch command and operator-initiated
smoke decision remain later prerequisites. Central logging configuration suppresses `httpx`,
`httpcore`, `h11`, `h2` and `hpack` above CRITICAL so global DEBUG cannot emit signed URLs, query
strings, headers, form bodies or wire data. Application errors and safe status logging remain
enabled. This policy is owned by `app.core.logger`, not import-time side effects.

The configured `BINANCE_TESTNET_ACCOUNT_FINGERPRINT` is a provisioned, non-secret local lineage
identifier. The current adapter does not derive or cryptographically verify it from Binance.
Before Phase 1, the operator must bind it out of band to the dedicated Spot Testnet account and API
key record. If an official stable Testnet account identifier is available in the account response,
a future minimal read-only preflight may compare a separately configured expected identifier; it
must not derive the fingerprint from the API key or secret. Without that binding, provenance is an
operator assertion, not a cryptographic proof, and Phase 1 must stop.

## Authorization ladder

| Phase | Explicit authorization | Permitted effect | Exit gate |
| --- | --- | --- | --- |
| 0 — offline only | `AUTHORIZE M4.10 PHASE 0` | local read-only inspection and offline tests | signed checklist; no credentials required |
| 1 — authenticated read-only | `AUTHORIZE M4.10 PHASE 1 READ ONLY` | bounded signed GETs only | evidence reviewed; process stopped |
| 2 — signed order test | `AUTHORIZE M4.10 PHASE 2 ORDER TEST` | exactly one `POST /api/v3/order/test` | empty-success response and zero durable/economic mutation |
| 3 — controlled order | authorization names side, computed quantity, database, HEAD and run ID | at most one real Spot Testnet MARKET order | reconciled/settled or explicitly unresolved; kill switch ACTIVE |
| 4 — restart proof | `AUTHORIZE M4.10 PHASE 4 RESTART` | read/reconcile the Phase 3 database; zero submissions | recovered evidence reviewed; runtime stopped |

A script must require the exact current-phase token and reject tokens for all other phases. It must
not offer a `--yes`, combined-phase or automatic-next-phase option.

## Phase 0 — offline preflight

Run from the repository root without loading credentials:

```sh
git status --short --branch
git rev-parse HEAD
git rev-list --left-right --count origin/main...HEAD
git diff --check
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
.venv/bin/pip check
shasum -a 256 app/persistence/migrations/*.sql
test ! -e data/m4_10_testnet_smoke.sqlite3
test ! -e data/m4_10_testnet_smoke.sqlite3-wal
test ! -e data/m4_10_testnet_smoke.sqlite3-shm
rg -n 'testnet\.binance\.vision|binance\.com|follow_redirects|verify=False' \
  app/core/config.py app/infrastructure/binance app/exchange_runtime
git status --short
```

Expected migration SHA-256 values are:

```text
001_initial.sql                 1c44bf2c187c26b01837787b50bca5bf952a7cd9fb9ed17b8377952e090fa433
002_exchange_execution.sql      c833d949fd1fab7b4a90990ff3a1a743be655ada7b1fd162a51866a478f7040c
003_exchange_reconciliation.sql 05e19fb9ee43f992f6efb15cab537b253d27aa2ea636024cc5743852cf0a4bb1
004_exchange_settlement.sql     4fb52f23e9c4fde653d26170a88c6d8e006062653188b3380ac57a0fec410a4e
```

Record exact HEAD. Require reviewed M4.10 tooling to be committed, a clean working tree,
`TRADING_MODE=testnet`,
`TESTNET_EXECUTION_ENABLED=true`, persistence enabled, BTCUSDT, fixed HTTPS Testnet host, dedicated
database and an ACTIVE durable kill switch. Prove `TRADING_MODE=live` and any other private host
fail before private-client construction. Search sanitized configuration and logs for key/secret
names only; never print values. Any existing smoke database or sidecar is a STOP, not permission to
overwrite it.

## Credentials and local injection

Use a dedicated Binance Spot Testnet key and secret for a dedicated Testnet account lineage. Never
reuse mainnet or Demo Mode credentials. Enable only the minimum Spot Testnet permissions needed by
the authorized phase; withdrawal permission must be absent and the account must have no unrelated
use or manually created managed-prefix orders.

Do not create `.env` or store credentials in repository files. The current `Settings` accepts
process environment variables, so a compatible temporary injection is an interactive hidden read
in a fresh shell, followed by exports scoped to that shell and `unset` immediately after shutdown:

```zsh
read -r -s 'BINANCE_TESTNET_API_KEY?Testnet API key: '; echo
read -r -s 'BINANCE_TESTNET_SECRET_KEY?Testnet secret: '; echo
export BINANCE_TESTNET_API_KEY BINANCE_TESTNET_SECRET_KEY
# run only the separately reviewed command for the authorized phase
unset BINANCE_TESTNET_API_KEY BINANCE_TESTNET_SECRET_KEY
```

The values are not entered as command arguments or saved in shell history. Environment exposure to
the authorized child process remains; use a password-manager/keychain wrapper later if already
available and reviewed. Never echo the variables, dump the environment, enable shell tracing or
attach them to evidence.

## Phase 1 — authenticated read-only

Phase 1 completed with PASS under its separate authorization. The dedicated one-shot entrypoint is
`python -m app.tools.testnet_phase1`; it requires the exact non-secret
`M4_10_PHASE1_AUTHORIZED=YES` token, refuses an existing retry evidence path, and exits after
one bounded run. It is not a mode of `app.main`: it imports no trader, decision pipeline,
submission service/gate/capability, lifecycle or persistence component. Its restricted facade
exposes only instrument rules, BTC/USDT balances, BTCUSDT open orders and recent trade history.
The Phase 1 authorization was consumed by that one run and grants no authority to rerun or proceed.

The transport guard fixes `https://testnet.binance.vision`, permits only approved GET paths, counts
method/path without query material, rejects every POST/PUT/PATCH/DELETE, and fails before a sixth
request. The initial fresh-account run always uses four requests and fails fast after an unexpected
open order. It creates no database or durable reconciliation/control/order/Portfolio state and
does not inspect or mutate the kill switch. Sanitized evidence contains normalized facts only;
exit codes are 0 PASS, 2 BLOCKED, and 1 technical FAILED.

The first authorized Phase 1 attempt stopped safely on 2026-08-13 at 16:05:51 +0300 (run
`9f22a99b-7e60-4682-9da6-a1d2b54716cd`) after exactly one public
`GET /api/v3/exchangeInfo`. Its sanitized `logs/m4_10_phase1.log` evidence records
`BinanceUnsupportedContractError`; no authenticated endpoint, POST, order-test, order or durable
side effect occurred. Offline review traced the stop to documented Binance symbol-filter contract
drift: `MAX_NUM_ORDER_AMENDS` and `MAX_NUM_ORDER_LISTS` were missing from the explicit known-ignore
set. The compatibility fix recognizes only those irrelevant no-amend/no-order-list limits while
retaining fail-closed behavior for truly unknown filters and unchanged MARKET lot/notional rules.
The original evidence remains untouched. Retry #1 wrote exclusively to
`logs/m4_10_phase1_retry1.log` and refused a collision there.

Phase 1 retry #1 completed with **PASS** on 2026-08-13 at 16:31:03 +0300 (run
`09be9bb1-a293-4879-9575-6e4dedc35ad5`). Evidence SHA-256 is
`ae3c7066ceb66a82f297c87a30d6da8d3ef340df6010c0744ea5a507a1c11e75`. Exactly four requests
occurred: one GET each to `/api/v3/exchangeInfo`, `/api/v3/account`, `/api/v3/openOrders` and
`/api/v3/myTrades`. No POST, `/api/v3/order/test`, `/api/v3/order`, cancellation, order,
persistence/Portfolio/control mutation or kill-switch change occurred. Credentials and the Phase 1
authorization token were removed after the run; evidence contains no key, secret, signature,
signed URL/query/body or private header.

Sanitized exchange authority was BTC `1.00000000` free / `0` locked and USDT `10000.00000000`
free / `0` locked. This is not managed Portfolio history. Open, managed-open and unmanaged-open
order counts were all zero. Recent and managed trade counts and unsupported-commission observations
were all zero, establishing a clean observed account lineage. BTCUSDT normalized to quantity step
`0.00001000` BTC, minimum quantity `0.00001000` BTC, minimum notional `5.00000000` USDT and rules
version `5a8c5636f082e6f494d3690c011b6264820b778b1156580cd05a9a3897d0169c`.

This proves the dedicated Testnet credentials authenticated, signed timestamp/`recvWindow`
handling worked, account reads normalized, the current filter contract normalized, and the
read-only harness remained isolated. It authorizes no POST or later phase.

Offline filter-contract classification for the 2026-08-13 documented Spot symbol set:

- executable MARKET safety inputs: `MARKET_LOT_SIZE`, fallback `LOT_SIZE`, `MIN_NOTIONAL` and
  `NOTIONAL`; their quantity/minimum/application semantics remain strictly normalized;
- known outside the current MARKET-only, no-iceberg/no-amend/no-order-list execution model:
  `PRICE_FILTER`, `PERCENT_PRICE`, `PERCENT_PRICE_BY_SIDE`, `ICEBERG_PARTS`, `MAX_NUM_ORDERS`,
  `MAX_NUM_ALGO_ORDERS`, `MAX_NUM_ICEBERG_ORDERS`, `MAX_POSITION`, `TRAILING_DELTA`,
  `MAX_NUM_ORDER_AMENDS` and `MAX_NUM_ORDER_LISTS`;
- unsupported: every identifier outside that explicit reviewed set, plus representable identifiers
  whose MARKET semantics cannot be expressed safely (for example an active maximum MARKET
  notional). These continue to raise `BinanceUnsupportedContractError`.

It must make at most five HTTP requests; the initial empty-lineage command makes exactly four:

1. public `GET /api/v3/exchangeInfo?symbol=BTCUSDT`;
2. signed `GET /api/v3/account` for normalized BTC and USDT balances;
3. signed `GET /api/v3/openOrders?symbol=BTCUSDT`;
4. signed `GET /api/v3/myTrades?symbol=BTCUSDT&startTime=...&limit=1000`;
5. reserved; no fifth request is made by the initial fresh-account command.

The current adapter has no `/api/v3/time` method; do not improvise a raw request in an operator
shell. For an existing durable managed order, its signed order query and order-scoped trade query
may replace/augment reads only under a separately reviewed request budget. Never call POST.

Success requires authentication and 5-second `recvWindow` handling, clock skew inside the reviewed
threshold, exact out-of-band account-lineage match, correctly normalized balances/rules, no
unexpected orders or trades, an observation compatible with a clean initial lineage, and zero
secret/signature leakage. Phase 1 deliberately writes no durable reconciliation record. Treat
current rules and balances as exchange authority.
Faucet/reset balances are not managed Portfolio history.

STOP immediately on authentication, timestamp/signature or lineage failure; HTTP 418/429; rules
mismatch/unknown filter; unexpected open order; any unexpected managed-prefix order or historical
trade; unsupported commission affecting reconciliation; recovery failure; reconciliation
CONFLICT/FAILED; production/Demo/non-HTTPS host; redirect; or suspected secret/signature leak.
Respect `Retry-After` and do not repeatedly probe credentials. Phase 1 never proceeds automatically.

## Phase 2 — signed order-test request

Phase 2 completed with **PASS** at `2026-08-13T14:00:10.409Z` under its separate authorization.
Run `54c016e7-d5f3-48ca-9ec0-d0f87c78bce1` produced the ignored sanitized evidence file
`logs/m4_10_phase2.log`, SHA-256
`88087b28fa1b1aa2709c850b087e641dc877133017b331813686afba2220f383`.
Exactly one request occurred: `POST /api/v3/order/test`, for `BTCUSDT`, `BUY`, base quantity
`0.00016000` and deterministic client order ID `ctt1t-evgxmjostjpx6py6eql5svvwrj`.
It returned `validated=True`, with no retry, stop reason, fallback or other endpoint. Evidence says
`financial_semantics=none`; credentials and `M4_10_PHASE2_AUTHORIZED` were removed from the shell,
and no API key, secret, signature, signed request or private header was recorded.

This proves that the Testnet signed POST contract, deterministic `ctt1t-` representation, MARKET
BUY base-quantity form, `newOrderRespType=ACK`, timestamp, fixed `recvWindow` and API-key header
were accepted by the test endpoint. It does not prove that a real order was accepted or executable.
No exchange order ID, trade, Fill, durable order/attempt, Portfolio change,
reconciliation/settlement state or balance mutation was created.

The dedicated one-shot harness is now implemented at `python -m app.tools.testnet_phase2`. It
requires the exact non-secret `M4_10_PHASE2_AUTHORIZED=YES` token and permits exactly one fixed-host
`POST /api/v3/order/test`; every other method, path, authority and any second request stop before
transport. Its restricted facade exposes only `test_market_order` and composes no persistence,
Portfolio, durable submission lifecycle, reconciliation, settlement or normal trading runtime.
The minimal adapter operation is a separate
contract from `submit_market_order`: there is no fallback to `/api/v3/order`, no retry, no redirect,
and no production lifecycle mutation. Phase 1 evidence passed; the separately authorized Phase 2
run recorded above also passed.

Official contract verification on 2026-08-13 used the current Binance Spot REST
[`Test new order (TRADE)` documentation](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade#test-new-order-trade)
and Binance's official
[`binance-spot-api-docs` REST source](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#test-new-order-trade).
They specify a signed `POST /api/v3/order/test`, `X-MBX-APIKEY`, order parameters inherited from
`POST /api/v3/order` (including MARKET base `quantity`, `newClientOrderId` and
`newOrderRespType`), timestamp/`recvWindow`, no matching-engine submission and `{}` when
`computeCommissionRates` is omitted/false. Binance's official
[`binance-connector-dotnet` Testnet documentation](https://github.com/binance/binance-connector-dotnet#testnet)
states that Spot Testnet at `https://testnet.binance.vision` supports `/api/*` endpoints. No
request was sent during verification.

The reviewed public reference price is `63849.22000000` and the Phase 2-only BUY quantity is
`0.00016000` BTC, approximately `10.22` USDT. The harness validates these fixed `Decimal` values
against `stepSize=0.00001000`, `minQty=0.00001000`, `minNotional=5.00000000` and a two-times
minimum-notional margin before transport. This observation does not imply permanent validity and
must be re-reviewed before authorization; the quantity is not a Phase 3 real-order quantity. The
harness uses the existing deterministic Testnet-format client-ID generator. It sends
`symbol=BTCUSDT`, `side=BUY`,
`type=MARKET`, base `quantity`, `newOrderRespType=ACK`, timestamp and the fixed 5-second
`recvWindow`, with the signature covering the exact transmitted bytes before the signature field.

Implemented MockTransport proof shows exactly one POST to the allowlisted host; the signed bytes
equal transmitted bytes;
redirects and retries are disabled; Binance accepts the filters/client-ID representation; and no
exchange order/trade, Portfolio transition, durable submission attempt or session submission
counter is created. Evidence must never include the signature or signed request body. Failure is a
STOP and cannot fall through to Phase 3.

The method returns only a non-financial `OrderTestPreflightResult` whose `validated` marker means
the request contract was validated, never that an order was accepted. Timeout, connection/protocol
failure, redirect and 5xx raise typed preflight failures, not real-submit ambiguity and never
`RECONCILIATION_REQUIRED`. Authentication/timestamp and 418/429 retain their existing typed errors
and `Retry-After`; malformed or non-empty success bodies fail normalization. There is no sleep or
retry. A closed adapter rejects before HTTP.

Offline Phase 2 implementation and review are complete: the harness targets exactly one fixed-host
`POST /api/v3/order/test`, has no fallback or retry, carries no persistence or financial semantics,
validates the deterministic client-ID format, and preserves Phase 1 account/rules evidence. It
writes immutable sanitized evidence to `logs/m4_10_phase2.log` and refuses a pre-existing path
before credentials or transport. Phase 2 authorization was consumed by the recorded PASS and
grants no rerun or later-phase authority. It
does not permit `POST /api/v3/order`, cancellation, kill-switch clear, a real order, continuous
trading or Phase 3.

## Phase 3 — one real Testnet order

Phase 3 architecture is accepted by ADR-064 and its offline schema-v5 authority primitive and crash
proofs are implemented and the final adversarial review passed, but execution remains **BLOCKED and not authorized**.
The dedicated `python -m app.tools.testnet_phase3` one-shot entrypoint is complete and its final
offline operator safety review passed using guarded mock transport only. Production execution remains
unauthorized. Any later authorization must state exact HEAD, account fingerprint,
fresh database path, run ID, BUY, computed base quantity, maximum submitted notional and
request count.

Offline architecture review found an additional durable crash gap. Today an audited
`ACTIVE -> CLEARED` transition can occur before any submission counter is consumed. If the process
dies in that interval, restart sees a CLEARED switch and can mint a fresh process-local capability.
ADR-063 prevents in-process promotion but does not make that pre-attempt ceremony one-shot.
ADR-064 resolves the design with a schema-v5 authority bound to the
exact root/account/session, control and reconciliation generations, run/authorization identity,
command identity and risk envelope. The global switch remains ACTIVE. One `BEGIN IMMEDIATE`
transaction inserts and consumes the authority, creates the only PREPARED command/reservation/event
and commits their one-to-one audit link. No authority-only commit is permitted. The commit returns
an in-memory claim receipt for an exact-command capability; crash/restart loses it and remains
recovery-only, able to inspect/reconcile the same identity but never regain first-submit eligibility.
No migration or production implementation was added in this design review.

The safer decision source is a dedicated operator-initiated smoke command, not waiting for or
distorting EMA. It must reuse the normal financial Risk, exchange Risk, durable decision/identity,
PREPARED, final gate and execution machinery; it may not submit directly. Its durable identity must
include the smoke authorization/run identity so replay returns the same command. Implement and
review this path separately.

The first Phase 3 authority is BUY-only. Its fresh preflight must prove the expected external BTC
baseline, sufficient quote, no open orders and no unexpected managed-prefix history. SELL is not a
field in this authority policy and is typed-rejected before command creation. A future separately
authorized SELL may use only exact managed BTC from authoritative Chappie BUY trades. Never sell or
normalize unexplained BTC.

Production composition initializes the durable Portfolio with zero base and configured quote
funds, while current reconciliation checks balance scope but does not establish ownership of the
pre-existing BTC. That BTC must remain outside managed Portfolio history and must never be sold
automatically. ADR-064 accepts a quote-funded managed subledger whose invariants/evidence distinguish
pre-existing exchange BTC. Reconciliation compares total free+locked balances to immutable external
baselines plus managed exchange effects derived only from authoritative Chappie trades; unexplained
difference is conflict, never import.

The proposed initial managed allocation is exactly `25 USDT`, with zero managed BTC. It is a
subledger ceiling rather than ownership of all `10000 USDT` observed at the exchange. The reviewed
smoke envelope would target approximately `10 USDT`, cap order/session-submitted/session-turnover
and managed position notional at `12 USDT`, and preserve at least `13 USDT` of the allocation for
reserve, fees and slippage. These are maximums, not a final quantity: current rules, price,
balances, open orders and managed-prefix history must be freshly checked with `Decimal` before any
future authorization.

### Accepted authority and schema-v5 contract

`Phase3SmokeAuthority` is an immutable durable consumption receipt, not a persistent capability.
Required identity is: `authority_id`; fixed TESTNET/`binance-spot`; account fingerprint;
execution-root and session IDs; expected control and reconciliation generations; unique
`operator_run_id`; unique Phase 3 economic-slot key; normalized non-sensitive `audit_reason`;
`smoke_policy_version`; exact durable command key; rules and execution-configuration fingerprints;
canonical risk-envelope and balance-scope payloads/digests; `created_at_ms`, `consumed_at_ms` and
record digest. Credentials never participate.

The reviewed authorization input uses the existing integer-millisecond Testnet authorization max
age. Freshness is checked before entering and again inside the write transaction. A stale input
fails without a durable row. Inside one `BEGIN IMMEDIATE`, authority is transaction-locally
PREPARED, the exact BUY command/order/event/reservation is inserted, authority is linked and marked
CONSUMED, and an append-only authority event is written. COMMIT exposes all facts or none. CONSUMED
is terminal and occurs at PREPARED commit, not HTTP transmission. There is no committed unconsumed
authority, so EXPIRED/CANCELED states and automatic replacement are unnecessary.

Conceptual migration 005 adds `exchange_smoke_authorities` and
`exchange_smoke_authority_events`. Authority columns include every identity/fingerprint/timestamp
above, canonical envelope/balance payloads, `status='CONSUMED'`, nullable-during-transaction but
required-at-COMMIT consumed command linkage, and payload digest. Constraints include PK authority;
UNIQUE execution-root/operator-run, economic slot and consumed command; FKs to root/session/order;
fixed environment/venue/status; positive generations/timestamps; consumed time not before creation;
64-character digests; and one authority event sequence. UPDATE/DELETE triggers make authority and
events immutable. Deferred linkage or an equivalent transaction-safe constraint must permit the
single circular authority/command creation without permitting a committed partial pair.

The minimum application API is one operation,
`authorize_phase3_smoke_command(reviewed_request) -> Phase3ClaimReceipt`. It owns no SQL and calls
one persistence transaction. Duplicate delivery of the same authority/run/economic slot returns
the identical consumed authority/command only when every canonical fact matches; any difference is
a typed conflict. The in-memory receipt mints a non-copyable/non-serializable TESTNET capability
bound to authority, command, session and generations. The final gate rechecks the consumed linkage,
ACTIVE control, SAFE reconciliation, settlement, session, counters, rules, envelope and current
authorization. ACTIVE is satisfied only for that exact command-scoped claim and blocks everything
else. Restart never restores the receipt or capability.

Balance-scope payload records fresh exchange free+locked totals and immutable external baselines.
For the accepted initial allocation, external BTC baseline is the observed total minus zero managed
BTC; external USDT baseline is the observed total minus `25 USDT`. Reconciliation requires exchange
totals to equal external baselines plus managed exchange effects calculated only from authoritative
trades for the linked command, including known locked effects and fees. Exchange free quote must
also cover managed pending requirements and the `13 USDT` reserve. Excess is external; any
unexplained delta is conflict. Whole-account balances are never copied into Portfolio.

### Required schema-v5 crash matrix

| Failpoint | Fresh-process authority/order state | Submission eligibility |
| --- | --- | --- |
| Before authority insert | no authority, no command | none |
| After insert, command insert, PREPARED event or CONSUMED update but before COMMIT | full rollback; no authority or command | none |
| Immediately after COMMIT or lost COMMIT acknowledgement | one CONSUMED authority linked to one PREPARED command | none after restart; ACTIVE/recovery-only |
| Before `SUBMITTING` in the same process | consumed authority and PREPARED command | only the original in-memory exact-command claim |
| After `SUBMITTING` COMMIT, before gateway or with lost ACK/ambiguity | one attempt and SUBMITTING/reconciliation-required identity | reconciliation only; no replacement |
| After ACK/rejection persistence | existing terminal/non-terminal lifecycle fact | existing reconciliation/settlement rules only |

Recovery validates authority/event digests, lineage, generations, timestamps, canonical envelope
and balance payloads, exact one-to-one authority/command linkage and legal order lifecycle. A
committed unconsumed authority, multiple commands, missing event/link, altered digest or impossible
timestamp is `RECOVERY_REQUIRED`; nothing is deleted or rebuilt. Phase 4 reopens the same database,
account, authority, command and client ID, reconciles authoritative trades and settles exactly once
without submission authority.

Limits are: BTCUSDT only; MARKET only; long-only; base quantity; one order and one in-flight order;
`TESTNET_MAX_SESSION_SUBMISSION_COUNT=1`; submitted-notional and turnover caps just above the
computed valid quantity, but low relative to available Testnet balance; `RISK_MAX_ORDER_NOTIONAL`
at the same bounded ceiling; `RISK_MAX_POSITION_NOTIONAL` no lower than that ceiling; conservative
target exposure; existing quote reserve preserved; current rule fingerprint; no realized-loss
approximation. Final numeric values require Phase 1 price, rules and balances and must use Decimal.

Required sequence:

```text
phase-specific authorization -> ACTIVE kill switch -> read-only recovery/reconciliation/settlement
-> cleanliness + financial Risk + exchange Risk
-> atomic one-shot authority CONSUMED + sole PREPARED command
-> final durable gate -> SUBMITTING COMMIT -> exactly one POST /api/v3/order
-> ACK or ambiguity -> reconciliation -> authoritative trades -> settlement
-> Portfolio verification -> durable kill switch ACTIVE -> process stop
```

Stop at every mismatch. Submission has no automatic retry. Authority is consumed when the sole
PREPARED command commits, so rejection, local/gateway failure or ambiguity cannot permit a
replacement. The global kill switch stays ACTIVE throughout; only the same-process, exact-command
claim can satisfy the final gate. Environment values cannot clear or replace this control.

Before authorization, the dedicated command must enforce one submission, one non-terminal order
and one in-flight order maximum; narrowly bounded submitted-notional, turnover, order and position
caps; sufficient quote reserve; current rules and a fresh reference price; controlled durable
decision/command identity; and automatic or audited immediate kill-switch reactivation after the
first committed attempt. Reserve `logs/m4_10_phase3.log`, `logs/m4_10_phase4.log` and
`data/m4_10_testnet_smoke.sqlite3` with fail-closed collision handling; do not create them before
their authorized phases.

## Ambiguity and stop procedures

For timeout, connection loss, HTTP 5xx, `-1007` or malformed ACK: do not rerun the command, delete
or edit the database, invent a client ID, clear state or issue another order. Record the run/client
ID, activate the kill switch if the audited control path remains available, stop decision delivery,
preserve the database and sidecars, then restart/reconcile the same database and client ID under a
new explicit authorization.

If reconciliation returns NOT_FOUND, remain RECOVERY_ONLY with the switch ACTIVE. Preserve the
database and repeat only read reconciliation later under an approved procedure; NOT_FOUND is not
proof of absence and never authorizes resubmission.

If a trade has BTC, BNB or another unsupported commission asset, preserve the trade, leave
settlement BLOCKED, perform no conversion or new submission, keep the switch ACTIVE, stop and
capture sanitized evidence. If any order/trade falls outside the expected managed lineage, stop;
do not import it into Portfolio or guess ownership.

## Phase 4 — restart/reconciliation proof

Phase 4 uses Process B, the exact Phase 3 database and the same account lineage. No executable
decision is delivered and no second order is permitted. Verify local recovery, reconciliation
status/generation, immutable command/client/exchange order IDs, authoritative trade IDs,
commission, settlement state, Portfolio version/balances/position/equity/PnL, session turnover and
submission count. Compare to Process A and prove no duplicate POST, attempt, trade, settlement or
Portfolio transition. Finish with the kill switch ACTIVE and runtime stopped.

Never delete an active WAL/SHM file. Preserve `data/m4_10_testnet_smoke.sqlite3` and any SQLite
sidecars as evidence until milestone review. Never overwrite or reuse an unexpected file.

## Settings inventory

Existing non-secret names required or relevant to the smoke are:

```text
TRADING_MODE=testnet
TESTNET_EXECUTION_ENABLED=true
PERSISTENCE_ENABLED=true
PERSISTENCE_BACKEND=sqlite
SQLITE_PATH=data/m4_10_testnet_smoke.sqlite3
SQLITE_BUSY_TIMEOUT_MS
BINANCE_TESTNET_ACCOUNT_FINGERPRINT
BINANCE_TESTNET_BASE_URL=https://testnet.binance.vision
TESTNET_EXECUTION_ROOT_ID
TESTNET_SESSION_ID
TESTNET_PORTFOLIO_ID
TESTNET_INITIAL_QUOTE_BALANCE
TESTNET_AUTHORIZATION_MAX_AGE_MS
TESTNET_MAX_SESSION_SUBMITTED_NOTIONAL
TESTNET_MAX_SESSION_EXECUTED_TURNOVER
TESTNET_MAX_SESSION_SUBMISSION_COUNT
TESTNET_MAX_SESSION_EXECUTION_FAILURES
TESTNET_MAX_DRAWDOWN
RISK_MAX_ORDER_NOTIONAL
RISK_MAX_POSITION_NOTIONAL
RISK_MAX_FEE_RATE
RISK_MAX_SLIPPAGE_BPS
LONG_TARGET_EXPOSURE
DEFAULT_SYMBOL=BTCUSDT
DEFAULT_INTERVAL
RUN_LOG_FILE
LOG_LEVEL
```

The two secrets are exactly `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_SECRET_KEY`. Some
non-secret Testnet/risk names are supported by `Settings` but are not currently listed in
`.env.example`; use process environment for the future controlled command and add documented
placeholders only in a separately reviewed configuration change. No generic Binance credential
fallback exists.

Clock policy: use the injected millisecond timestamp source and fixed adapter `recvWindow=5000`.
Before signing, compare local time to a reviewed server-time source. STOP if absolute skew exceeds
the repository's configured warning threshold (`MARKET_CLOCK_SKEW_WARNING_SECONDS`, default 5) or
cannot be established. Do not increase `recvWindow` automatically.

Request budgets: Phase 1 maximum five normal requests; Phase 2 exactly one POST after any separately
authorized fresh read preflight; Phase 3 exactly one order POST plus the minimum bounded reads
needed for reconciliation; Phase 4 reads only. On 418/429, honor `Retry-After`, stop and obtain new
authorization rather than looping.

## Evidence package

Return after every phase: phase authorization reference, exact HEAD, UTC time, run ID, database
fingerprint/path, account fingerprint, host, request method/path counts (without query/body),
reconciliation and control generations, kill-switch state, readiness/recovery/settlement status,
rules fingerprint, Portfolio version and sanitized status/log lines. After Phases 2–4 also return,
as applicable, client order ID, durable command hash, exchange order ID, trade IDs, symbol, side,
base/quote quantities, commission amount/asset, session counters, balances/equity/PnL and proof of
zero duplicate submission/settlement.

Never return API key, secret, signature, authorization headers, environment dump, signed URL/body,
raw private response or unreviewed logs. The normal safe application loggers are `TraderAgent`, the
application status logger, `Persistence`, `MarketEvents` and optional shadow/paper observers. The
expected Testnet status line contains only availability, readiness reasons, capability presence,
reconciliation, settlement and non-terminal count. Dependency request logs are suppressed by the
central private-transport policy. Regression tests use unmistakable fake credentials and
signatures under global DEBUG and assert that request URLs, bodies, headers, private responses and
exception tracebacks do not leak them.

## Completion and M4.11 boundary

### ADR-065 implementation acceptance matrix

Zero is evaluated only after typed finite `Decimal` normalization and by exact `is_zero()` semantics;
there is no epsilon. Inbound `0`, `0.0`, `0.00000000` and `-0` normalize to canonical persisted `0`.
Canonical storage rejects non-canonical spellings, malformed/non-finite values and floats; the smallest
non-zero Decimal remains non-zero. Exact-zero BUY accounting adds authoritative base quantity, deducts
authoritative quote quantity, adds zero fees and adds quote quantity to turnover, independent of the
preserved commission asset label. It never subtracts base commission or converts BTC/BNB.

Implementation review must cover zero BTC, BNB and USDT labels; signed zero; positive BTC/BNB blocking;
positive USDT unchanged; smallest positive unsupported fee blocking; malformed/non-finite/float
rejection; and the production-style `0 BTC` blocked fixture resolving to `0.00016 BTC`, `14.890736
USDT`, zero fees and `10.109264 USDT` turnover. Resolve-once, duplicate resolution, restart, duplicate
trade delivery, Phase 4 replay on a copy and lost commit acknowledgement must produce one economic
effect. `RuntimeError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are injected before/after
resolution insert, Portfolio current, session turnover, audit record, commit and acknowledgement:
pre-commit is full rollback; post-commit is durable recovery.

The typed completion predicate is `SYNCHRONIZED` only when the authoritative order is terminal, base
and quote trade totals are complete, reconciliation and coverage are SAFE/complete, every trade has
either a normal SETTLED record or a valid resolved-zero record, unresolved blocked/pending counts are
zero, and Portfolio/session authority validation succeeds. `blocked_count`, `settled_count` and other
per-invocation counters are diagnostics only. Integrity/authority mismatch is FAILED/CONFLICT;
unresolved or pending truth is INCOMPLETE.

M4.10 completes only after separately authorized Phases 1–4 prove: read authentication and lineage;
one `/order/test`; one valid real Testnet MARKET order; authoritative reconciliation and settlement;
clean restart; zero duplicate submission/settlement; ACTIVE final kill switch; stopped runtime;
redacted evidence; and no LIVE hostname or secret leak. A BUY is required; a SELL is required by the
current Stage 4 completion design but may be a later separately authorized M4.10 session using only
the managed position. Cancellation remains unsupported, so roadmap wording must not imply it is
part of this smoke.

M4.11 aggregates automated and controlled evidence, resolves final Stage 4 checklist gaps and
issues the Stage 4 completion verdict. It does not add continuous Testnet trading or LIVE, and this
plan does not consume that audit/completion scope. The staged authorization ladder is operational
validation of accepted architecture, so no new ADR is needed unless implementation introduces a
new durable authority or execution capability.
