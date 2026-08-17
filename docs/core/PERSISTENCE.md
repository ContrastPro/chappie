# Persistence and Recovery

## Scope

This is the canonical persistence contract for the active Chappie MVP. It
covers the durable Paper trading journal and the normal continuous Testnet
execution schema. It does not authorize LIVE trading, repair financial data, or
turn local persistence into evidence of an exchange outcome.

The active runtime is deliberately smaller than the historical M4.10 / Stage 4
experiments. Those experiments, their schemas v5–v10, and their one-shot
authority/recovery evidence are preserved as [historical Stage 4
material](../historical/stage4/README.md); they are not part of normal MVP
startup or readiness.

Related active contracts:

- [Paper trading](PAPER_TRADING.md) defines Paper accounting and provider
  behavior.
- [Trading decisions](TRADING_DECISIONS.md) defines the strategy → planner →
  Risk boundary.
- [Decision runtime](DECISION_RUNTIME.md) defines coordinator startup and
  process-local `SAFE_STOP`.
- [Risk](RISK.md) defines approval envelopes, not persistence or settlement.
- [Exchange execution](EXCHANGE_EXECUTION.md) is the companion contract for
  Testnet reconciliation, submission, and settlement.

## Safety boundary

Persistence owns durable local facts and their consistency. It does not:

- create an exchange outcome from an acknowledgement, timeout, local order, or
  a `PREPARED` record;
- repair, discard, synthesize, or overwrite a financial fact to make startup
  succeed;
- make observer delivery exactly-once;
- grant an order-submission capability; or
- perform network reconciliation itself.

Financial values are finite `Decimal` values serialized canonically as text.
Record payloads and identity inputs use canonical JSON and SHA-256 digests;
Python object representations, floats, arbitrary metadata, tracebacks, API
keys, and credentials are not durable financial facts.

## Storage ownership and active schemas

Both active stores use a composition-root-owned SQLite connection and
single-writer runtime ownership. SQLite is configured with WAL,
`synchronous=FULL`, foreign keys, a bounded busy timeout, and explicit
`BEGIN IMMEDIATE` write transactions. A competing exclusive runtime writer is
rejected rather than silently waiting and becoming another trading runtime.

| Runtime | Durable store | Required schema | Purpose |
| --- | --- | --- | --- |
| Paper | `SQLITE_PATH` (default `data/chappie-paper.sqlite3`) | v1 | Durable Paper decisions, commands, fills, Portfolio journal, and checkpoints. |
| Continuous Testnet MVP | `Settings.active_testnet_sqlite_path` (default `data/testnet_mvp.sqlite3`) | v4 | Normal exchange lifecycle, reconciliation, authoritative trade facts, settlement, and managed Portfolio projection. |
| LIVE | none | none | Unavailable. |

`ACTIVE_MVP_EXCHANGE_SCHEMA_VERSION` is explicitly **4**. The migration runner
validates the stored migration names and checksums, refuses a database newer
than the configured contract, and applies only missing migrations through the
configured version. Therefore an active Testnet process starts against
migrations 001–004, not 005–010.

The historical `data/m4_10_testnet_smoke.sqlite3` path is rejected by normal
Testnet configuration. Migrations 005–010 remain versioned source and
historical evidence only; they do not determine active MVP readiness.

## Active Paper persistence

Paper uses an append-only financial journal plus verified checkpoints:

```text
portfolio root + append-only transitions/fills/decision facts = authority
portfolio_current                                         = current projection
portfolio_checkpoints                                    = replay acceleration
```

The root fixes the initial Portfolio identity, version zero, canonical state
digest, stream, and configuration fingerprint. A different initial balance or
configuration cannot be substituted once a root exists.

A stable decision key identifies the actual strategy input and its relevant
strategy/planner/snapshot configuration. A decision slot additionally blocks a
second trade for the same candle/strategy/configuration slot when corrected
history would otherwise produce a different input. An executable decision has a
durable command key; a committed fill is unique by both fill ID and durable
command key. These durable constraints, not bounded in-memory caches, are the
restart idempotency boundary.

Each effective valuation and each fill transition is journaled. A no-op
valuation may leave Portfolio version unchanged while the terminal decision
audit remains durable. Checkpoints are created after configured effective
transitions (default 100) and on orderly shutdown when needed. They never
replace the journal and cannot hide a broken earlier chain.

Paper accounting and fill semantics are defined in [Paper trading](PAPER_TRADING.md).
In particular, this document does not duplicate BUY/Sell fee formulas; it
records the resulting verified Portfolio transition and immutable fill facts.

## Paper transaction boundaries and commit-before-memory

One Paper cycle uses one `BEGIN IMMEDIATE` transaction. In order, it persists:

1. the stable decision claim and immutable decision audit;
2. optional command authorization and risk/execution facts;
3. optional full fill;
4. one or more validated Portfolio transitions;
5. the `portfolio_current` projection and terminal decision outcome.

All of those authoritative facts commit or roll back together. A visible
committed Paper fill without its Portfolio transition is corruption, not a
retryable normal state. Unique constraints make an identical committed replay
return the stored terminal result; conflicting payload reuse is an integrity
failure.

The engine calculates a staged transition before commit. It does not mutate the
authoritative in-memory Portfolio first. Only after database commit does it
apply the committed transition to memory, then notify observers. If the
post-commit in-memory synchronization fails, the engine becomes `DEGRADED` and
does not execute another Paper cycle; the next process learns the truth from
SQLite. Observer/checkpoint failure is non-financial and never authorizes a
fill replay.

## Paper startup and recovery

The Paper composition sequence is:

```text
open SQLite with exclusive ownership
→ validate schema and SQLite integrity
→ create root only for a fresh configured Portfolio
→ recover and validate durable state
→ restore Portfolio from verified state
→ construct Risk / Paper execution / engine
→ subscribe the coordinator
→ allow polling
```

Recovery validates the database, root identity, canonical state digests,
ordered transition chain, fill/command/decision lineage, Portfolio versions,
current projection, and applicable checkpoint linkage. It restores from the
newest valid checkpoint and verifies the full journal lineage; if a checkpoint
is invalid it may use an earlier valid checkpoint or replay from the root.

Recovery results are `CLEAN`, `RECOVERED`, or `RECOVERY_REQUIRED`. A missing
`portfolio_current` projection can be rebuilt only after recovery proves the
journal head. A present but conflicting projection is never auto-repaired.
`RECOVERY_REQUIRED`, schema/integrity failure, or an unavailable exclusive
writer prevents Paper coordinator startup. A fresh empty Portfolio is not a
fallback for an existing invalid lineage.

## Active Testnet persistence and reconciliation boundary

Continuous Testnet uses schema v4 under the separate
`SQLiteExchangePersistence` surface. It persists a local execution root and
session, exchange-order lifecycle facts, reconciliation runs and coverage,
normalized exchange trade facts, settlement facts, and the exchange Portfolio
root/current projection.

Testnet startup is deliberately ordered:

```text
open schema-v4 store
→ local recovery validation
→ exchange reconciliation
→ settlement initialization and pending settlement
→ readiness evaluation
→ construct the submission graph only when READY
```

Local persistence can prove the consistency of recorded facts; exchange
reconciliation supplies the authoritative external observation. Settlement is
separate from reconciliation: a durable trade fact is not a Portfolio mutation
until settlement accepts it under the required reconciliation evidence. A
`PREPARED` local order is intent evidence only and never becomes a network side
effect merely because the process restarts.

The normal v4 settlement transaction records the authoritative settlement,
advances the exchange Portfolio projection, and advances applicable session
economic counters together before in-memory synchronization. It validates
canonical payload/digest and identity linkage on recovery. Unknown, incomplete,
or conflicting exchange facts remain a readiness/recovery problem; persistence
does not infer a fill, cancel, or rejection. See [Exchange execution](EXCHANGE_EXECUTION.md)
for the submission, reconciliation coverage, and settlement rules.

## Journal, checkpoints, and idempotency

Paper journal rows, decision records, commands, fills, Portfolio roots, and
checkpoints are append-only. The current projections are maintained in the same
financial transaction and are rebuildable only from verified authority. Journal
sequence is a committed database order; Portfolio versions must be consecutive
within a Portfolio lineage. Gaps caused by rolled-back SQLite row allocation do
not themselves prove corruption.

In-memory coordinator, Paper-engine, and fill caches are bounded fast paths.
They may be empty after restart. Durable unique keys and complete lineage checks
are the authority that prevents a second committed Paper fill for the same
durable command. This is an at-most-one committed local financial mutation
guarantee for the authoritative Paper store, not a claim of exactly-once
observer delivery or exactly-once external exchange submission.

## SQLite ownership, WAL, and monitoring

The running Paper/Testnet process owns its writable SQLite database. WAL and
SHM sidecars are SQLite-owned runtime files. Do not delete, copy independently,
or manually edit the database or sidecars while the runtime is active. Use the
SQLite online backup API for a coordinated backup; a raw copy of only the main
database can omit committed WAL pages.

The live Paper monitor reads the sanitized atomic status snapshot rather than
opening a competing SQL reader against an active runtime database. After the
runtime has stopped and released ownership, read-only diagnostics may inspect a
copy or the stopped database as appropriate. Monitoring does not become a
financial authority and no operator performs manual SQL repair.

## Failure semantics

| Condition | Active response |
| --- | --- |
| Missing, mismatched, failed, or newer-than-supported schema | Abort startup; do not trade. |
| SQLite integrity, journal, digest, lineage, or projection disagreement | `RECOVERY_REQUIRED` / startup failure; preserve diagnostics and investigate. |
| Runtime writer conflict or persistence unavailable | Fail startup or transaction; no second writer or automatic retry. |
| Paper transaction error before commit | Roll back the entire cycle; no visible financial mutation. |
| Commit acknowledgement ambiguity or post-commit memory-sync failure | Degrade/stop the Paper path; discover durable truth only by reopening/recovery. |
| Observer/checkpoint failure after financial commit | Log diagnostic failure; do not roll back a committed financial fact and do not replay the fill. |
| Testnet local/external reconciliation or settlement cannot be trusted | Do not construct/retain submission capability; readiness remains blocked or recovery-only. |
| Unexpected Testnet execution-consumer failure | Process-local `SAFE_STOP`; later events cannot submit and a fresh process/operator action is required. |

There is no automatic financial repair, retry from an ambiguous external result,
or fallback to a new Portfolio over an invalid existing one.

## Reset, archival, and disposable development data

Paper and Testnet MVP databases are development/operational artifacts, not a
reason to manually repair financial state. On unexpected state: safe-stop,
preserve logs/database/status when useful for diagnosis, then reset/bootstrap a
fresh disposable database through the documented workflow. Do not edit SQL rows
to make a run continue. The server reset/archive procedure is in the
[MVP runbook](../operations/SERVER_MVP_RUNBOOK.md).

This MVP policy does not define a future LIVE retention, backup, restoration, or
forensic-repair policy.

## Historical M4.10 and Stage 4 notes

The following source material is historical and intentionally excluded from
the active persistence contract:

- schema v5 Phase-3 smoke authority and its process-local claim;
- schema v6 exact-zero-commission settlement resolution and proof harness;
- schema v7 managed SELL authority, schema v8 recovery/disposition decisions,
  schema v9 disposition journal, and schema v10 successor lineage;
- M4.10 immutable proof fixture/database checks, one-shot operators, and
  Stage-4 recovery/continuation experiments.

They remain preserved in [historical Stage 4](../historical/stage4/README.md),
including the smoke plan, M4.11 report, managed-SELL closure design, and SELL
recovery/disposition design. They must not be used as operational commands or
as evidence that a fresh continuous Testnet MVP database requires schema v5–v10.

## Tests and verification

The active regression suite retains Paper lifecycle, duplicate prevention,
restart recovery, journal/checkpoint integrity, transaction failpoints, SQLite
writer ownership, reconciliation, settlement, Portfolio integrity, Risk, and
`SAFE_STOP` coverage. Representative locations include:

- `tests/test_persistence.py` — schema/migration, journal, checkpoint,
  recovery, writer, backup, and integrity boundaries;
- `tests/test_paper_engine.py` and `tests/test_paper_execution.py` — staged
  Paper execution, commit/replay, crash, and accounting behavior;
- `tests/test_exchange_persistence.py`, `tests/test_exchange_reconciliation.py`,
  and `tests/test_exchange_runtime.py` — normal v2–v4 exchange persistence and
  readiness;
- `tests/test_application.py` and `tests/test_trading_coordination.py` — startup
  ordering, runtime ownership, and `SAFE_STOP`.

Historical M4.10/Stage 4 proof suites are intentionally classified as
historical; they remain runnable explicitly but do not define active MVP
readiness.

## Source invariant crosswalk

| Legacy source section / invariant | Canonical destination | Status |
| --- | --- | --- |
| 1, M4.6: v2 order lifecycle, `PREPARED` is intent, pre-POST facts are durable, local and exchange truth differ | Active Testnet persistence and reconciliation boundary | Preserved for active v2–v4 semantics |
| 1, M4.7: reconciliation run/coverage evidence, SAFE is bounded, settlement remains separate | Active Testnet persistence and reconciliation boundary | Preserved |
| 2: in-memory crash gap and bounded process-local dedup are insufficient after restart | Safety boundary; journal, checkpoints, and idempotency | Preserved |
| 3: durable Paper goals; no observer exactly-once, no auto financial repair | Safety boundary; failure semantics | Preserved / narrowed to active MVP |
| 4–6: application-owned persistence port, append-only journal + checkpoint authority | Active Paper persistence; transaction boundaries | Preserved |
| 7: stable decision/slot/command identities and canonical hashing | Active Paper persistence; journal, checkpoints, and idempotency | Preserved |
| 8–9: single financial transaction; transition/current/terminal ordering; immutable financial records | Paper transaction boundaries and commit-before-memory | Preserved |
| 10–11: root-to-current reconstruction, checkpoints as optimization, valuation history | Active Paper persistence; Paper startup and recovery | Preserved |
| 12: finite canonical Decimal and timestamp distinction | Safety boundary | Preserved |
| 13: durable constraints outrank in-memory dedup | Journal, checkpoints, and idempotency | Preserved |
| 14–18: recovery order, crash matrix, corruption and shutdown behavior | Paper startup and recovery; failure semantics | Preserved, with current application order |
| 19: observers follow financial commit and are diagnostic only | Paper transaction boundaries and commit-before-memory | Preserved |
| 20: SQLite WAL/FULL/FK/`BEGIN IMMEDIATE`, ownership, sidecar and backup rules | Storage ownership; SQLite ownership, WAL, and monitoring | Preserved; no obsolete writer-lease proposal as active contract |
| 21–23: migration checksums, forward-only upgrade, backup/security boundaries | Storage ownership and active schemas; reset policy | Preserved where implemented; LIVE policy excluded |
| 24: crash, duplicate, schema, writer, observer, and restart test categories | Tests and verification | Preserved |
| 25: future durable OHLCV, retention, multi-host lock, encryption questions | Scope; reset policy | Superseded/future work, not active MVP guarantees |
| 26 and M3.13–M3.16: SQLite implementation, root, staged transitions, runtime wiring, recovery results | Active Paper persistence; transaction boundaries; Paper startup and recovery | Preserved |
| M4.8–M4.9/schema v4: exchange Portfolio, reconciliation-gated settlement, restart-safe local facts | Active Testnet persistence and reconciliation boundary | Preserved |
| M4.10/ADR-064–069 and schemas v5–v10: one-shot smoke, managed SELL, recovery, disposition, successors | Historical M4.10 and Stage 4 notes | Historical only |

## Code/document discrepancies resolved by this consolidation

| Legacy claim | Current code/test evidence | Disposition |
| --- | --- | --- |
| Default Paper path was `data/chappie-trader.sqlite3` | `Settings.sqlite_path` defaults to `data/chappie-paper.sqlite3`; Paper startup tests cover current composition. | Superseded. |
| Proposed writer lease and future settings were active behavior | `SQLiteTradingPersistence` uses SQLite exclusive locking when configured; the old advisory-lease proposal is not the active runtime contract. | Superseded; document actual exclusive ownership. |
| M4.10 v5–v10 described as a next/active persistence extension | `ACTIVE_MVP_EXCHANGE_SCHEMA_VERSION = 4`; Testnet composition explicitly requests v4 and tests reject the historical M4.10 DB. | Historical only. |
| A fixed $10,000 Paper root was assumed | Paper initial quote balance is configuration-controlled and the root binds the actual configured value. | Superseded. |
| Stage 4 recovery claims/capabilities were described as future runtime behavior | Active runtime exports only normal lifecycle surface; Stage 4 tests/docs are historical. | Historical only. |
| Recovery status might be described as a generic automatic remediation | Current Paper recovery rebuilds only a missing projection from verified authority and otherwise fails closed; Testnet readiness with untrusted facts has no submit graph. | Clarified. |
