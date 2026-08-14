# Chappie Trader

Chappie Trader is the financial subsystem of the future Chappie platform. The current
runtime application obtains Binance Spot OHLCV data for `BTCUSDT`, validates it,
stores bounded normalized state in memory and publishes synchronous typed market
events. The Stage 2 domain layer contains the deterministic EMA baseline, Intent
Planner and Trading Decision Pipeline. An optional shadow application layer can
evaluate that pipeline on confirmed `CandleClosed` events without placing orders.

The default application remains paper/public-data only. Stage 4 contains an isolated,
disabled-by-default Binance Spot Testnet execution boundary; LIVE trading is structurally
rejected. The project does not promise or guarantee income.

## Installation

Python 3.13 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Configuration

Copy the example configuration and review its values:

```powershell
Copy-Item .env.example .env
```

Paper mode needs no API keys. M4.8 Testnet configuration is explicit and disabled by default:
`TRADING_MODE=testnet`, `TESTNET_EXECUTION_ENABLED=true`, dedicated
`BINANCE_TESTNET_API_KEY`/`BINANCE_TESTNET_SECRET_KEY`, and the fixed
`https://testnet.binance.vision` endpoint. Never commit a real `.env`; secrets are redacted and
must not be logged or persisted. `TRADING_MODE=live` is rejected. Automated validation uses
only fake exchange dependencies and makes no authenticated request.

If Testnet recovery finds an active kill switch, unresolved/non-terminal order or unsafe
reconciliation state, startup remains available in recovery-only mode. Persistence,
reconciliation, settlement and status remain available, while no submission capability, execution
service or Testnet decision consumer exists. After the durable blocker is resolved, restart the
process to perform a fresh readiness check; recovery-only does not promote itself in-process.

M4.9 offline crash/restart torture recreates fresh production graphs around file-backed SQLite and
a deterministic fake exchange. Repeated NOT_FOUND, later FOUND, lost acknowledgements, duplicate
decisions and settlement restarts remain no-resubmit/exact-once. This is automated safety evidence,
not an authenticated Testnet smoke. M4.10 Phase 1 authenticated read-only and Phase 2 signed
`/api/v3/order/test` evidence pass; LIVE remains unsupported.

The M4.10 controlled Spot Testnet security preflight is completed and its final safety review
passed. The dedicated `python -m app.tools.testnet_phase1` harness is structurally read-only,
GET-only, capped at five requests and isolated from persistence/submission composition. Phase 1
retry #1 passed with four GETs, no open orders/trades and no side effects; that authorization did
not authorize Phase 2. The first attempt's `logs/m4_10_phase1.log` evidence is retained after a safe
one-public-GET contract-drift stop; separately authorized retry #1 used
`logs/m4_10_phase1_retry1.log` without overwriting it.
The dedicated `python -m app.tools.testnet_phase2` harness requires the exact non-secret
`M4_10_PHASE2_AUTHORIZED=YES` token and permits one request only: fixed-host
`POST /api/v3/order/test`. Its reviewed BUY quantity is `0.00016000` BTC, validated entirely with
`Decimal` against the Phase 1 rules and the manually observed public reference price
`63849.22000000`; it is Phase 2-only evidence and is not a real-order quantity. Phase 2 passed with
exactly one test-endpoint POST and no financial semantics. Phase 3 execution remains blocked and
unauthorized; the account's pre-existing 1 BTC remains external rather than managed Portfolio history.
The Phase 3 final design review accepts ADR-064: a zero-BTC, `25 USDT` managed
subledger plus durable one-shot authority consumed atomically with the sole PREPARED command. A
The schema-v5 safety boundary is implemented. The global switch remains ACTIVE; only the transaction's
in-memory exact-command claim may reach the final gate, so restart cannot recreate eligibility.
The offline schema-v5 one-shot authority implementation is complete and its final adversarial safety
review passed. The dedicated one-shot Phase 3 operator completed its authorized ACK-level submission;
ACK is not fill or settlement evidence.
See [`M4_10_TESTNET_SMOKE_PLAN.md`](M4_10_TESTNET_SMOKE_PLAN.md). Configuring credentials does not
authorize an authenticated read, `/api/v3/order/test` or a real order; each is a separate gate.

Phase 3 subsequently completed its single ACK-level submission; ACK is not fill evidence. The Phase 4
same-database recovery/settlement harness is complete and its final offline safety review passed. It
has no submission path and remains unauthorized pending separate execution approval.

The first real Phase 4 observation proved a complete FILLED trade and SAFE reconciliation, then stopped
INCOMPLETE because Binance labeled an exact zero commission as BTC. ADR-065 accepts exact finite Decimal
zero as asset-neutral while preserving the original trade and blocked audit record. Schema-v6
append-only resolution is implemented offline and its final offline safety review passed; production
remediation subsequently completed under separate authorization and produced synchronized durable
truth. The dedicated
`python -m app.tools.testnet_phase4_remediate_zero_fee` harness is implemented offline: it has no
HTTP/credential dependency, accepts only its exact remediation token, validates the known durable
shape, and writes a new exclusive remediation evidence file only after local synchronized truth. Its
final offline safety review passed. The observation-only
`python -m app.tools.testnet_m410_recovery_proof` harness is now implemented with a distinct exact
authorization, immutable read-only SQLite access, full durable lineage/digest validation, unchanged
database checksum enforcement and exclusive sanitized evidence. Its separately authorized production
proof passed, completing M4.10. M4.11 evidence aggregation is complete with `STAGE_4_BLOCKED`:
the accepted architecture still requires controlled Testnet SELL evidence in addition to the proven
BUY. That SELL closure is not started or authorized; Stage 5, continuous Testnet and LIVE remain
not started. See [`M4_11_STAGE_4_COMPLETION_REPORT.md`](M4_11_STAGE_4_COMPLETION_REPORT.md).

The offline design for the remaining managed-SELL evidence gap is
[`STAGE_4_MANAGED_SELL_CLOSURE_DESIGN.md`](STAGE_4_MANAGED_SELL_CLOSURE_DESIGN.md). It is
`READY_FOR_STAGE4_SELL_SCHEMA_IMPLEMENTATION`; ADR-066 was accepted on 2026-08-14. It permits only an
exact full managed-position exit under fresh rules in the same durable
lineage; external account BTC is never Risk inventory. No implementation, SELL authorization,
continuous Testnet, LIVE or Stage 5 work has started.

Important defaults:

- market: Binance Spot;
- symbol: `BTCUSDT`;
- interval: `1m`;
- cache capacity and bootstrap limit: `1000` candles;
- polling cadence: `10` seconds;
- regular request limit: `2` candles.
- clock-skew warning threshold: `5` seconds; Binance server time remains authoritative.

Clock-skew warning означает, что локальные часы Windows отличаются от Binance
server time. Market validation, cache lifecycle, events и trading snapshots используют
авторитетное exchange time, поэтому warning сам по себе не является runtime failure
и не блокирует ingestion. Локальные часы рекомендуется синхронизировать вручную в
настройках Windows; внешний NTP dependency приложению не требуется.

## Shadow decisions

Shadow mode is disabled by default. Enable it with
`SHADOW_DECISIONS_ENABLED=true`. Its main settings are `EMA_FAST_WINDOW`,
`EMA_SLOW_WINDOW`, `LONG_TARGET_EXPOSURE`, `SHADOW_HISTORY_LIMIT`,
`SHADOW_DEDUP_CAPACITY` and `SNAPSHOT_STALE_AFTER_MS`.

Every result, including `HOLD`, is written as a compact `ShadowDecisions` entry to
the normal application logs. Shadow mode performs analysis only: it has no Risk
Manager, execution adapter, portfolio mutation, paper order or live order path.
Its output is observational and does not promise or guarantee profit.

Market Cache атомарно reconciles финальную версию свечи, которая была сохранена
открытой, и публикует `CandleUpdated` перед однократным `CandleClosed`. Уже
подтверждённые закрытые свечи остаются immutable.

## Finite smoke-run

Set a positive cycle count in `.env`:

```text
SMOKE_RUN_CYCLES=3
```

Then run:

```powershell
python -m app.main
```

The application performs exactly the configured number of polling cycles unless a fatal error occurs, prints the final Stage 1 status through logging and closes the HTTP provider.

## Logs

The application writes two UTF-8 log files:

- `logs/chappie.log` is the cumulative history and is opened in append mode;
- `logs/latest_run.log` is the complete most recent run and is overwritten at startup.

The per-run path can be changed with `RUN_LOG_FILE`. Every line carries the same
`run_id`; the run header contains a safe configuration summary without credentials,
and the footer records the exit code and final lifecycle state. The console remains
concise: detailed market events and HTTP request logs stay in the files, while
warnings and errors are still displayed.

After a smoke-run, inspect its complete log with:

```powershell
Get-Content .\logs\latest_run.log
```

Or open it in Visual Studio Code:

```powershell
code .\logs\latest_run.log
```

## Continuous polling

Leave `SMOKE_RUN_CYCLES` empty and run:

```powershell
python -m app.main
```

Stop with `Ctrl+C`. A normal manual stop completes without a traceback and exits with code `0`.

## Quality checks

```powershell
ruff check .
pytest
git diff --check
```

Automated tests do not require network access. A real smoke-run is a separate manual verification because it contacts Binance and may otherwise run for an unintended duration.
# Shared decision runtime

Decision processing remains disabled by default. `SHADOW_DECISIONS_ENABLED=true`
enables observational delivery. `PAPER_TRADING_ENABLED=true` requires
`PERSISTENCE_ENABLED=true` and enables deterministic durable paper execution. Either
decision flag creates one `TradingDecisionCoordinator`, so Strategy/Pipeline is
evaluated once per confirmed candle. Paper mode uses no private Binance endpoint.

The persistence architecture is documented in
[`PERSISTENCE_RECOVERY_DESIGN.md`](PERSISTENCE_RECOVERY_DESIGN.md): an append-only
authoritative paper journal, verified Portfolio checkpoints, stable restart identities
and fail-closed recovery. M3.13 implements the SQLite core; M3.14 integrates startup
recovery, durable paper transactions, restart-stable identities, commit-before-memory
synchronization and checkpoint lifecycle. The M3.15 crash/restart safety review passed;
its failpoint matrix and operational limits are recorded in
[`M3_15_CRASH_RESTART_REPORT.md`](M3_15_CRASH_RESTART_REPORT.md). Live execution is absent.

M3.16 completed Stage 3 with a controlled two-process public Binance data smoke using
one ignored SQLite database. Recovery restored the exact durable head and a new candle
advanced Portfolio and journal versions from `0 -> 1 -> 2`; both decisions were HOLD,
so no real-smoke Fill occurred. Deterministic automated suites cover executable
BUY/SELL restart and duplicate-Fill behavior. Keep the database together with any
SQLite-owned WAL/SHM files while it is active; observer delivery remains best-effort.
`BINANCE_TESTNET_ACCOUNT_FINGERPRINT` is also required as a non-secret durable account-lineage
identifier. Testnet startup is intentionally fail-closed: an operator must have explicitly cleared
the durable kill switch, and reconciliation plus settlement must reach typed READY before the
shared decision coordinator subscribes.
