# M3.16 Stage 3 Completion Report

Date: 2026-08-09

Status: **PASS — M3.16 and Stage 3 completed**.

## Repository baseline

- branch: `main`;
- baseline HEAD: `ba1ae937f7a3f63804609dc474b2ad233bc27728`;
- local branch before this completion commit: one commit ahead and zero behind
  `origin/main`;
- initial worktree: clean;
- `git fsck --full`: intact, with one permissible dangling tree and no broken refs;
- Git identity: `Gleb Krasnov <glebon0202@gmail.com>`.

## Automated and static audit

- pytest: 903 passed; 0 failed, skipped, xfailed or xpassed;
- combined line/branch coverage: 95%;
- application 96%, paper engine 95%, durable runtime 98%, recovery coordinator 98%,
  SQLite adapter 95%, Portfolio module 93%;
- persistence/runtime ResourceWarning suite: 303 passed;
- Ruff check and format check: passed;
- `pip check` and `git diff --check`: passed;
- `scripts/full_audit.py`: PASS WITH GAPS;
- no private/live order path, merge markers, tracked runtime artifacts or test DBs.

The retained audit gaps are: no configured static type checker, no randomized-order
pytest plugin, no durable OHLCV store and no live exchange reconciliation.

## Static no-real-order proof

Production search found no `/api/v3/order`, `create_order`, `new_order`, `cancel_order`,
signed trading request, testnet order client or private execution adapter. Optional API
key settings are not consumed by a trading adapter. With `PAPER_TRADING_ENABLED=true`,
composition constructs only `PaperExecutionProvider`; public Binance market-data HTTP
is the sole exchange network boundary.

## Controlled real restart smoke

The two processes used the same ignored database:
`data/m3_16_smoke.sqlite3` (102400 bytes after shutdown). No WAL/SHM sidecars remained
after either graceful shutdown. The database and logs were not staged.

### Run 1 — PASS

- run ID: `3cebfd2f-f003-4f0e-9462-4de58ab7c6af`;
- startup: `recovery=CLEAN`, Portfolio version 0, equity 10000, journal 0, no checkpoint;
- public Binance bootstrap succeeded and a new `CandleClosed` was processed;
- shared pipeline ID: `86484269-8542-4c18-9561-b379e072e081`;
- decision: HOLD / `no_crossover`; shadow DELIVERED; paper DELIVERED;
- paper outcome: NO_INTENT; no command and no Fill;
- durable ending: Portfolio version 1, journal 1, checkpoint 1, quote 10000, base 0,
  equity 10000, trades 0, `last_fill_id=None`;
- Stage 1: 6 completed, 6 successful, 0 failed, 0 delivery failures;
- graceful user shutdown, exit code 0.

### Run 2 recovery — PASS

- run ID: `13ce0fc6-e79b-4f16-81ac-78a3e2fcff6d`;
- same database reopened;
- startup: `recovery=CLEAN`, Portfolio version 1, equity 10000, journal 1,
  checkpoint 1, replayed 0 — exactly matching run 1's durable ending;
- the coordinator started after recovery; no fresh financial Portfolio replaced the
  recovered one;
- a new post-restart public Binance `CandleClosed` was processed;
- shared pipeline ID: `14992ed9-6f59-4792-ac62-94bf06fae05c`;
- decision: HOLD / `no_crossover`; shadow DELIVERED; paper DELIVERED;
- paper outcome: NO_INTENT; no command and no Fill;
- durable ending: Portfolio version 2, journal 2, checkpoint 2, quote 10000, base 0,
  equity 10000, trades 0, `last_fill_id=None`;
- Stage 1: 5 completed, 5 successful, 0 failed, 0 delivery failures;
- graceful user shutdown, exit code 0.

Direct read-only SQLite inspection after run 2 found two HOLD decisions, two journal
entries, two checkpoints, zero commands and zero fills. Exchange clock skew was about
163 ms and caused no warning or failure; exchange server time remained authoritative.

## Restart proof

```text
Run 1: fresh DB -> v0/journal0 -> new candle -> shared HOLD -> durable v1/journal1
       -> graceful shutdown
Run 2: same DB -> recover v1/journal1 -> new candle -> shared HOLD
       -> durable v2/journal2 -> graceful shutdown
```

This is real public-data runtime continuity across a process restart. It does not claim
a real BUY or SELL: both market decisions were HOLD, so fill count and trade count
correctly stayed zero. Executable BUY/SELL restart behavior, lost-ack handling and
duplicate-Fill protection remain proven by deterministic automated crash/restart and
multi-runtime duplicate suites.

## Architecture and lifecycle result

Startup opens/migrates SQLite, verifies and restores the Portfolio, builds the paper
components, and only then subscribes the shared decision coordinator. One confirmed
candle produces one pipeline result shared by shadow and paper. A financial mutation
is published in memory only after its SQLite transaction commits. Shutdown stops
polling and subscriptions, attempts a final checkpoint, closes persistence and the
public market provider, reports status, and flushes logging.

## Known operational limits

- the real smoke observed HOLD only;
- manual database deletion cannot be distinguished from a fresh installation without
  an external identity manifest;
- an internally valid stale backup cannot prove global freshness;
- SQLite-owned WAL/SHM files must not be manually manipulated while the DB is active;
- observer delivery is best-effort, not exactly once;
- no durable OHLCV store exists;
- no live/testnet execution or exchange-side reconciliation exists;
- clock-skew warnings remain operational and exchange time is authoritative.

## Completion decision

Automated/static result: **PASS**.

Controlled two-run real restart smoke: **PASS**.

M3.16: **COMPLETED**.

Stage 3: **COMPLETED**.
