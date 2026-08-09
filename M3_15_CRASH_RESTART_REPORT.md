# M3.15 Crash and Restart Safety Report

Date: 2026-08-09

Status: **completed; safety review passed**.

Scope: durable local paper runtime only. No network smoke, testnet, live execution,
private Binance endpoint or push was used.

## Golden invariant

Every tested process-loss boundary resolves after opening a fresh SQLite adapter and
constructing a fresh RecoveryCoordinator/Portfolio/Risk/Execution/Paper engine to one
of two states:

1. pre-commit: no durable decision, command, Fill or Portfolio transition; the same
   stable decision may execute exactly once after restart;
2. post-commit: one terminal decision, at most one command/Fill and an exact Portfolio
   journal/current state; restart returns `DURABLE_REPLAY` without Risk or Execution.

No tested boundary produced an ambiguous state that was allowed to continue trading.

## Failpoint matrix

| Failpoint | Durable truth after fresh open | Restart behavior |
|---|---|---|
| BEFORE_TRANSACTION | rollback/unchanged | decision executes once |
| AFTER_DECISION_CLAIM | claim rolled back | slot/key reusable |
| AFTER_COMMAND_CLAIM | decision and command rolled back | command recreated once |
| AFTER_FILL_CREATED_IN_MEMORY | no durable Fill | new Fill is sole committed Fill |
| AFTER_FILL_PERSISTED | Fill/command/decision rolled back | decision executes once |
| AFTER_FIRST_PORTFOLIO_TRANSITION | entire transaction rolled back | no partial valuation |
| AFTER_FINAL_PORTFOLIO_TRANSITION | both transitions rolled back | no partial cycle |
| AFTER_PORTFOLIO_CURRENT_WRITE | current and journal rolled back | DB remains at root |
| AFTER_TERMINAL_CYCLE_WRITE | terminal and all financial rows rolled back | safe retry |
| BEFORE_COMMIT | zero financial mutation | safe retry |
| AFTER_COMMIT/lost acknowledgement | terminal cycle and two transitions durable | replay, zero Risk/Execution |
| BEFORE/DURING_MEMORY_SYNC | DB authoritative; engine degraded | exact recovery on restart |
| AFTER_MEMORY_SYNC/observer boundaries | financial state already durable | observer never controls replay |
| DURING_CHECKPOINT | journal/current remain authoritative | earlier checkpoint + journal |
| shutdown checkpoint/close | committed journal remains authoritative | next startup recovers |

Injected transaction failures are test-only wrappers; no production debug/failpoint
mode was added.

## Production correction

The review found one production defect. A durable executed cycle may contain valuation
and Fill transitions. Applying them sequentially after COMMIT could leave the old process
at the intermediate valuation if the second synchronization failed. `Portfolio` now
validates and stages the complete committed tuple and publishes snapshot/fill-registry
state once. A late conflict leaves memory completely pre-cycle; a valid batch becomes
completely post-cycle.

## Restart, replay and corruption evidence

- HOLD with effective valuation and no-op HOLD persist terminal decisions and do not
  repeat valuation after restart.
- RISK_REJECTED, NOT_EXECUTED and EXECUTED terminal decisions reconstruct semantic
  durable replay without repeating the committed financial path.
- A corrected historical input in the same decision slot fails closed before mutation.
- Five entirely fresh runtime instances redelivering one executed BUY retain one Fill,
  two journal transitions and an unchanged Portfolio; fresh Risk/Execution call counts
  stay zero.
- A multi-cycle BUY/HOLD/SELL restart scenario restores exact final state and continues
  only on a new candle.
- Runtime UUIDs and empty recreated process-local dedup registries do not affect stable
  durable identity.
- Latest corrupt checkpoint falls back to an earlier checkpoint; all corrupt
  checkpoints fall back to root plus the complete journal.
- Earlier journal corruption is detected even when a valid checkpoint exists.
- Missing `portfolio_current` is reconstructed read-only and explicitly rebuilt by the
  application only after verified recovery; a conflicting current projection returns
  `RECOVERY_REQUIRED`.
- Root digest/config/stream mismatch, malformed Decimal, broken digest/FK/lineage,
  version gap and unsupported schema fail closed before coordinator subscription.
- SQL constraints independently reject a second Fill for one durable command and a
  duplicate/gapped Portfolio version chain.

## Lifecycle and storage evidence

- SQLite uses WAL, `synchronous=FULL`, foreign keys, busy timeout and `BEGIN IMMEDIATE`.
- Exclusive runtime ownership rejects a second application writer before subscription.
- Closing a connection with an active uncommitted transaction releases the SQLite lock;
  a fresh adapter observes rollback and can acquire ownership.
- Observer ordinary exceptions are isolated after commit. Observer `KeyboardInterrupt`,
  `SystemExit` and `GeneratorExit` propagate, but restart still cannot trade twice.
- Checkpoint failure is operational and cannot roll back or duplicate a committed cycle.
- Shutdown attempts coordinator stop, final checkpoint, persistence close and provider
  cleanup with nested `finally` boundaries.
- Online SQLite backup restores an identical internally consistent state and then
  diverges independently when new decisions are processed.
- Tests use pytest temporary directories; no DB/WAL/SHM artifact is written under
  project `data/`.
- A 2,000-transition file-backed sanity run completed recovery in 0.103 seconds on the
  review Mac (0.354 seconds to create the history). This is not a performance SLA, but
  it found no obvious quadratic replay behavior. Checkpoint and full-journal paths keep
  identical semantics; recovery still verifies the authoritative journal before using
  a checkpoint replay suffix.

## Operational limitations

- Deleting the SQLite database can look like a genuinely fresh installation. Without an
  external manifest or backup catalogue the application cannot prove that history once
  existed. Operators must protect and back up the database and its identity.
- A stale but internally valid backup is recoverable, but the application cannot prove
  it is globally latest without external backup versioning.
- SQLite WAL/SHM sidecars are SQLite-owned and must never be manually deleted while the
  database may be active.
- Observer delivery is best-effort and not exactly once; the journal is the financial
  authority.
- There is no durable OHLCV store. Recovery proves the paper financial journal and
  Portfolio lineage, not reconstruction of historical market-data availability.
- There is no live exchange execution or exchange-side reconciliation. Local paper
  recovery makes no claim about reconciling orders, fills or balances with Binance.
- A controlled public-data paper restart smoke remains M3.16 work. It must use only
  public Binance market data and must not add credentials or private order APIs.

## Final validation

- pytest: 903 passed; 0 failed, skipped, xfailed or xpassed;
- combined line/branch coverage: 95%;
- application 96%, paper engine 95%, durable runtime 98%, recovery coordinator 98%,
  SQLite adapter 95%, Portfolio module 93%;
- ResourceWarning persistence/crash suite: 303 passed;
- Ruff check and format check: passed (120 files formatted);
- pip check and git diff check: passed;
- repository audit: PASS WITH GAPS;
- no DB/WAL/SHM, `.coverage` or pytest temporary artifact remained.

## ADR and roadmap result

ADR-048 through ADR-054 are accepted after the successful review. M3.14 is completed
with final safety review passed. M3.15 is completed. M3.16 Stage 3 Completion is next;
Stage 3 is not yet declared complete.
