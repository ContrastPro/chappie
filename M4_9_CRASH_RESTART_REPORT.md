# M4.9 Crash/Restart/Duplicate-Order Safety Review

Status: **completed; final crash/restart safety review passed** (2026-08-13).

Stage 4 remains in progress. M4.10 controlled authenticated Testnet smoke is next, not started, and was
not performed. LIVE remains structurally unsupported.

## Scope and safety boundary

M4.9 adds deterministic torture evidence, not exchange functionality. Every production-graph
process uses a fresh `build_application()` composition, a file-backed SQLite database and an
`httpx.MockTransport` fake exchange under a socket/DNS guard. No authenticated network request or
real order occurred.

The reviewed authority timeline is:

```text
shared decision -> financial Risk -> exchange Risk -> durable PREPARED
-> final gate -> single-use authorization
-> attempt insert -> SUBMITTING projection -> session counters -> COMMIT
-> gateway -> transport observation -> durable status/reconciliation
-> authoritative trades -> settlement -> Portfolio memory synchronization
```

Before the attempt transaction commits, injected failures roll back attempt, projection and
counters together. After that commit, every failure is conservatively treated as potentially
transmitted: restart reconciles the stable client order ID and never blindly submits again.

## Failpoint taxonomy and results

| Boundary | Injected families/evidence | Durable result | Restart rule |
| --- | --- | --- | --- |
| PREPARED/authorization | RuntimeError and BaseException matrices | PREPARED or complete rollback | fresh decision and final gate required |
| attempt insert/projection/counters/before commit | RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit | transaction rollback; zero attempt/counter | safe reconsideration only through a new gate |
| immediately after attempt commit | all BaseException families | SUBMITTING; one attempt/counter | reconciliation only |
| gateway timeout/reset/protocol/5xx/-1007 | typed adapter transport matrix | SUBMITTING or RECONCILIATION_REQUIRED | same client ID; no retry |
| ACK/rejection/ambiguity persistence | pre-, intra- and post-commit matrices | rollback or one committed status | SQLite truth controls restart |
| reconciliation facts/run/current/control commit | injected transaction matrix | committed facts retained; generation atomic | no partial SAFE authority |
| trade persistence | duplicate/conflicting identity matrix | one fact or typed conflict | no inferred/overwritten truth |
| settlement journal/current/turnover commit | all four exception families where meaningful | full rollback or one committed settlement | exact recovery from durable head |
| post-settlement memory sync | RuntimeError and BaseException matrix | durable settlement committed, old runtime degraded | fresh process restores exact state |

No production defect was found. Two test-harness mistakes were corrected: an empty fake exchange
must not advertise an open order, and a live exclusive writer must not be inspected through a
second SQLite adapter.

## Duplicate-order torture

- Twenty fresh production graphs receiving NOT_FOUND retained one command, one client ID, one
  attempt, one submission counter contribution and one fake gateway submission.
- After seven NOT_FOUND restarts, ACKNOWLEDGED, PARTIALLY_FILLED and FILLED observations each
  resolved the original durable lineage without replacement submission.
- One hundred repeated deliveries in one process produced one order and one gateway call.
- After terminal FILLED settlement, ten fresh process registries redelivered the same economic
  decision with new runtime identities; durable decision-slot identity still returned the single
  original order.
- Existing eviction, corrected-history conflict, stale actor, competing signal, kill-switch,
  control-generation, reconciliation-generation, settlement and session-race matrices remain
  green. In-memory dedup is only an optimization; SQLite identity and the final gate are authority.

## Reconciliation and settlement restart torture

Repeated reconciliation preserves committed order/trade facts, rejects conflicts and advances a
SAFE generation atomically. Lost commit acknowledgement reads the committed generation on the
next process; a later reconciliation is a new audited generation, never a regression or replayed
authority.

The complete-history barrier prevents a later trade from settling while an older trade may be
missing. Late discovery settles older then later. Multipart BUY/SELL, reversed and shuffled input,
and same-timestamp trade IDs converge to identical Portfolio and turnover. Duplicate trade and
settlement identities are idempotent; conflicting duplicates fail closed. Unsupported base/BNB
fees remain durable, keep settlement blocked, never fabricate conversion and prevent capability
issuance across restart.

## Stress and deterministic schedule

- 20 recovery-only start/stop cycles: no capability, consumer, coordinator, PREPARED order or
  gateway call.
- 20 clean READY start/stop cycles: twenty distinct process-local capabilities and zero order side
  effects; no capability is persisted.
- 24 isolated fixed-seed schedules (`Random(49)`): one to eight NOT_FOUND restarts followed by a
  deterministic unresolved or FOUND state; all finish with one durable command/client ID/attempt
  and at most one remote submission.
- Existing large reconciliation/settlement and bounded-history tests exercise database growth.
  A synthetic 500-order/2,000-trade benchmark was not added because it would bypass the current
  one-nonterminal-order production policy and would not establish a new safety invariant. No
  pathological behavior was observed in the full suite.

## Reusable invariant audit

The M4.9 assertion helper checks after torture cases:

- `PRAGMA integrity_check` and `foreign_key_check`;
- clean local exchange recovery;
- unique durable command and client-order identities;
- contiguous order-event versions;
- at most one submission attempt per command under current Stage 4 policy;
- reconciliation generation monotonicity and single-step advancement.

Existing settlement authority tests additionally validate coverage evidence/digests, consecutive
Portfolio versions and current head, settlement uniqueness and recomputed session turnover.

## Writer, WAL, resources and secrets

File-backed tests use SQLite WAL naturally and never delete WAL/SHM while a connection is active.
Second live writers remain rejected; fresh writers open after teardown. Owned clients/persistence
close, caller-owned clients remain open and focused suites promote `ResourceWarning` to errors.
Recognizable fake credentials remain absent from logs, status, exceptions and durable payloads.

## Known limits

- True OS kill/power-loss testing is represented by deterministic post-commit failure injection;
  controlled subprocess kill evidence remains appropriate for operational smoke infrastructure.
- Session rollover creation is deferred; closing/invalidation of the current session is fail-closed.
- Authenticated Testnet smoke remains M4.10 and requires separate authorization.
- Cancellation, WebSocket/background execution and LIVE are outside M4.9.
- Static type checking and randomized pytest-order plugins remain repository-wide tooling gaps;
  the M4.9 schedule uses a local fixed seed instead.

## Milestone conclusion

M4.9 is complete and its final crash/restart safety review passed. The torture
evidence found no path to a blind duplicate order, duplicate financial settlement, stale
capability submission or silent authority repair. M4.10 is next and not started; Stage 4 remains
in progress.
