# Chappie Server MVP Runbook

## Scope

This runbook covers the disposable Paper and Binance Spot Testnet MVP only.
LIVE execution is unavailable and must not be enabled.

## Active Testnet schema contract

The continuous Testnet MVP uses exchange schema **v4** (`001_initial.sql` through
`004_exchange_settlement.sql`). It is the minimum schema that provides durable
orders, reconciliation, authoritative-trade settlement, and a managed Portfolio.

Migrations v5–v10 are historical M4.10/Stage 4 experiment support. They are not
required for normal MVP startup or trading cycles.

## Database locations and reset policy

- Paper: set `SQLITE_PATH`, for example `data/chappie-paper.sqlite3`.
- Testnet: set a separate `SQLITE_PATH`, for example
  `data/chappie-testnet.sqlite3`.
- Never point Paper and Testnet at the same database file.

Paper and Testnet databases are disposable development artifacts. On an
unexpected execution state, safe-stop first. Archive the database and logs only
when they help diagnose the issue; otherwise stop the process, remove the
specific database and its SQLite sidecars, and start with a fresh database.
Never manually repair financial rows to continue a run.

Future LIVE durability requires a separately designed and audited policy.

## Paper startup

Configure a dedicated Paper database and set:

```text
TRADING_MODE=paper
PAPER_TRADING_ENABLED=true
PERSISTENCE_ENABLED=true
SQLITE_PATH=data/chappie-paper.sqlite3
```

Start with `python -m app.main`. The process polls continuously unless
`SMOKE_RUN_CYCLES` is set for a bounded diagnostic run.

## Testnet startup

Use a dedicated Testnet database and explicitly configure:

```text
TRADING_MODE=testnet
TESTNET_EXECUTION_ENABLED=true
PERSISTENCE_ENABLED=true
SQLITE_PATH=data/chappie-testnet.sqlite3
```

Inject the dedicated Testnet API key, secret, and account fingerprint through
the server's secret mechanism. Do not commit them or log them. Startup performs
the bounded reconciliation/readiness checks before it can submit anything.

## SAFE_STOP

Any unexpected Testnet execution-consumer failure enters `SAFE_STOP` for that
process. Later market events are ignored by the Testnet coordinator and cannot
produce another submission. There is no automatic retry or in-process restart.

After SAFE_STOP:

1. Stop or let the supervisor stop the process.
2. Preserve logs and the database only if they are useful for diagnosis.
3. Inspect the sanitized failure category and durable order state.
4. Reset the disposable Testnet database when investigation is complete.
5. Start a fresh process; never manually resume the old order.

## Logs and basic operations

`RUN_LOG_FILE` defaults to `logs/latest_run.log`; configure persistent log
rotation in the process manager. Monitor the last successful market cycle,
coordinator lifecycle, Testnet readiness, reconciliation status, settlement
status, and any `SAFE_STOP` record.

The MVP does not provide a LIVE mode, automatic recovery ceremony, cancellation
workflow, or unattended repair of inconsistent Testnet state.
