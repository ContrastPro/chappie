# M4.10 Controlled Binance Spot Testnet Smoke Plan

Status: **Phase 1 authenticated read-only PASS; Phase 2 authorization pending** (2026-08-13).

Stage 4 remains in progress. This document authorizes no network access, credential use,
`/api/v3/order/test` request or real Testnet order. Every phase below needs a new, explicit
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

The minimal adapter operation now exists for exactly `POST /api/v3/order/test`. It is a separate
contract from `submit_market_order`: there is no fallback to `/api/v3/order`, no retry, no redirect,
and no production lifecycle mutation. Phase 1 evidence passed; Phase 2 remains authorization
pending and requires a new explicit operator authorization.

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

Before Phase 2, obtain a fresh public/read reference price `P`; Phase 1 did not record price, so it
is unsafe to guess an exact quantity now. Compute entirely with `Decimal` using target test
notional `2 * minNotional = 10.00000000 USDT` and
`quantity = max(minQty, ceil((10 / P) / stepSize) * stepSize)`. Recheck the resulting notional,
current rules and available balance immediately before authorization. This Phase 2-only candidate
is not a Phase 3 real-order quantity. Include a deterministic Testnet-format client ID. Send
`symbol=BTCUSDT`, chosen `side`,
`type=MARKET`, base `quantity`, `newOrderRespType=ACK`, timestamp and the fixed 5-second
`recvWindow`. Do not hardcode the quantity in this plan.

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

Offline Phase 2 prerequisites are otherwise met: the committed preflight method targets exactly
one fixed-host `POST /api/v3/order/test`, has no fallback or retry, carries no persistence or
financial semantics, validates the deterministic client-ID format, and has Phase 1 account/rules
evidence preserved. Before authorization, this evidence update must be committed with a clean Git
tree and a reviewed fresh public price observation must support the Decimal quantity calculation.
No production behavior is missing. Any Phase 2 authorization permits exactly that one POST. It
does not permit `POST /api/v3/order`, cancellation, kill-switch clear, a real order, continuous
trading or Phase 3.

## Phase 3 — one real Testnet order

Phase 3 remains not authorized and is design-only until Phase 2 evidence passes and the missing
one-shot operator tooling is reviewed. Authorization must state exact HEAD, account fingerprint,
fresh database path, run ID, BUY/SELL, computed base quantity, maximum submitted notional and
request count.

The safer decision source is a dedicated operator-initiated smoke command, not waiting for or
distorting EMA. It must reuse the normal financial Risk, exchange Risk, durable decision/identity,
PREPARED, final gate and execution machinery; it may not submit directly. Its durable identity must
include the smoke authorization/run identity so replay returns the same command. Implement and
review this path separately.

Select BUY only if Phase 1 proves a dedicated lineage with expected USDT, no unmanaged BTC and no
unexpected history. Select SELL only when a prior managed BUY has produced the exact durable BTC
position intended for the paired smoke. Never sell or normalize unexplained BTC. Any unexpected
position/history requires operator review.

Limits are: BTCUSDT only; MARKET only; long-only; base quantity; one order and one in-flight order;
`TESTNET_MAX_SESSION_SUBMISSION_COUNT=1`; submitted-notional and turnover caps just above the
computed valid quantity, but low relative to available Testnet balance; `RISK_MAX_ORDER_NOTIONAL`
at the same bounded ceiling; `RISK_MAX_POSITION_NOTIONAL` no lower than that ceiling; conservative
target exposure; existing quote reserve preserved; current rule fingerprint; no realized-loss
approximation. Final numeric values require Phase 1 price, rules and balances and must use Decimal.

Required sequence:

```text
phase-specific authorization -> audited kill-switch clear -> fresh startup
-> recovery -> reconciliation -> settlement -> READY
-> one controlled smoke command -> financial Risk -> exchange Risk -> PREPARED
-> final durable gate -> SUBMITTING COMMIT -> exactly one POST /api/v3/order
-> ACK or ambiguity -> reconciliation -> authoritative trades -> settlement
-> Portfolio verification -> durable kill switch ACTIVE -> process stop
```

Stop at every mismatch. Submission has no automatic retry. A reviewed one-shot policy should
atomically exhaust authority after the first committed attempt and reactivate the kill switch as
soon as the outcome is durably recorded. Until that behavior exists, the operator must reactivate
through an audited command immediately after the first attempt; the runtime stays supervised and
must be stopped if reactivation cannot be proven. Environment values may not clear the switch.

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
