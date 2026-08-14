# M4.11 Stage 4 Final Evidence Report

Date: 2026-08-14

## Verdict

- **M4.10: COMPLETED — PASS.** The bounded one-shot BUY, reconciliation,
  remediation and immutable fresh-process recovery proof are complete.
- **M4.11: COMPLETED — PASS.** This report aggregates the accepted evidence and
  issues the Stage 4 verdict without adding runtime behavior.
- **Stage 4: BLOCKED.** The accepted execution architecture requires controlled
  Testnet BUY **and SELL** with persisted exchange trades. The durable evidence
  contains exactly one BUY and no SELL. Every other reviewed completion criterion
  is satisfied. Stage 5 has not started.

This is an evidence-coverage gap, not a production safety defect. Closing it requires
a new, separately authorized bounded SELL ceremony using only the managed
`0.00016 BTC`; the existing external Testnet BTC remains unmanaged. This report does
not authorize that request, continuous Testnet trading, cancellation, LIVE trading or
any new execution capability.

## Audit chronology

| Milestone | Result | Sanitized evidence |
|---|---|---|
| Phase 1 authenticated read-only | PASS; four GETs | `logs/m4_10_phase1_retry1.log`; run `09be9bb1-a293-4879-9575-6e4dedc35ad5` |
| Phase 2 signed validation | PASS; one `POST /api/v3/order/test`; no financial semantics | `logs/m4_10_phase2.log`; run `54c016e7-d5f3-48ca-9ec0-d0f87c78bce1` |
| Phase 3 one-shot BUY | PASS at submission/ACK; five GETs and one `POST /api/v3/order` | `logs/m4_10_phase3.log`; run `27dbb928-b3a3-4cf4-bf40-8e4094d6cb79` |
| Phase 4 recovery | SAFE reconciliation and FILLED trade; initially INCOMPLETE | `logs/m4_10_phase4.log`; run `09bb40de-70f1-4e6e-a234-398420db7516` |
| Root cause | `EXPECTED_SAFETY_BLOCK`: exact zero commission was labelled BTC | preserved blocked settlement |
| ADR-065/schema v6 | accepted and implemented | policy `zero-commission-asset-neutral/v1` |
| Persistence-only remediation | PASS | `logs/m4_10_phase4_remediation.log`; run `bd132ec3-5887-4170-9edb-be24494d311f` |
| Immutable fresh-process proof | PASS | `logs/m4_10_recovery_proof.log`; run `e78051a4-1d8a-43a5-be3a-d486d7f5f4d4` |

The initial Phase 4 `INCOMPLETE` result was the intended fail-closed response to an
unsupported fee label, not a failed safety outcome. ADR-065 and schema v6 resolved it
append-only while leaving both the authoritative trade and historical block immutable.

Relevant implementation milestones are M4.3 domain/ports (`0068e99`), M4.4 risk
hardening (`90e9ecd`), M4.5 adapter (`ac713bd`), M4.6 lifecycle (`6895113`), M4.7
reconciliation (`445f0a7`), M4.8 runtime integration (`37038f7`), M4.9 restart review
(`bf5db62`) and the M4.10 chain ending at recovery proof `60788a1`.

## Protected evidence

| Artifact | SHA-256 |
|---|---|
| `data/m4_10_testnet_smoke.sqlite3` | `1d3df2d7a07d236a7054319f52c64b72550a5bcb9818796cb376b2a11ac8ae90` |
| `logs/m4_10_phase3.log` | `2773158ae14179f42632d914c0022324bdf5a0dec2a6faaa055e2d20970aaad6` |
| `logs/m4_10_phase4.log` | `c4afcc4972d9b584849c7988ee035465fa9d4ef476a93ffe5a0ce042dc6ba015` |
| `logs/m4_10_phase4_remediation.log` | `de5a17ef533e3c59e90a03497fda1e1c06afe980340aa2b510f7363216cd2938` |
| `logs/m4_10_recovery_proof.log` | `b4611033201903980de341272232c119d330012979d209a7b3fd82db3412c997` |
| `006_zero_commission_resolution.sql` | `7b6543b489fd9ca67435093e3e34f5fde48101803e65cd502ae5c1004af80f1d` |

Migration integrity is additive and byte-stable:

| Version | SHA-256 |
|---|---|
| 001 | `1c44bf2c187c26b01837787b50bca5bf952a7cd9fb9ed17b8377952e090fa433` |
| 002 | `c833d949fd1fab7b4a90990ff3a1a743be655ada7b1fd162a51866a478f7040c` |
| 003 | `05e19fb9ee43f992f6efb15cab537b253d27aa2ea636024cc5743852cf0a4bb1` |
| 004 | `4fb52f23e9c4fde653d26170a88c6d8e006062653188b3380ac57a0fec410a4e` |
| 005 | `57c1422ffa74e7f0975ba2d271793088da591ab843f4380d79e1bbcb7d8f5345` |
| 006 | `7b6543b489fd9ca67435093e3e34f5fde48101803e65cd502ae5c1004af80f1d` |

No production WAL/SHM was present during aggregation. No credential, authorization
token, signature, signed URL or raw private response is included here.

## Durable identity and exactly-once proof

- authority: `ffeb4c54321dd7797af214cebd875fae830fb79068b60e7c9f83fcda05f85b1d`
- command: `0ac9414dc11efbf3220cecab1004d1b2cf7ea2aedc911914a38a8329b582a326`
- client order ID: `ctt1t-napyrku5v5n6fywoyrsmc24y6g`
- exchange order/trade: `2590783` / `615567`
- durable counts: one authority, command, order, submission attempt, trade,
  historical blocked settlement, resolution and Portfolio transition
- terminal order: `FILLED`; pending settlements: `0`; unresolved blocks: `0`;
  global settlement: `SYNCHRONIZED`

PREPARED and SUBMITTING persisted before the only network submission. The client
identity was deterministic, ambiguity never created a replacement identity or blind
retry, and recovery never reconstructed submission capability. The authoritative trade
was persisted once. Its append-only zero-fee resolution produced one economic
transition; duplicate submission and duplicate settlement counts are both zero.

## Portfolio, reconciliation and recovery

| State | Version | Managed BTC | Managed USDT | Fees | Turnover |
|---|---:|---:|---:|---:|---:|
| Before BUY settlement | 0 | 0 | 25 | 0 | 0 |
| Recovered final | 1 | 0.00016 | 14.890736 | 0 | 10.109264 |

Reconciliation was SAFE with complete authoritative order/trade truth. The fresh
process opened schema v6 immutable/read-only, proved the full authority-to-settlement
lineage, observed the same Portfolio/session state and verified the database checksum
before and after. The pre-existing exchange BTC and excess USDT were not imported into
managed Portfolio state. Reconciliation generation `0→1` established the clean root;
generation `1→2` examined and advanced the sole order and persisted the sole trade. The
current durable generation is `2`, SAFE, with latest reconciliation
`8a88c2f9422db87d31bae8a630cac96075df8b9b7419582f0a3fe1326a24591f`.

## Completion criteria mapping

| Criterion | Evidence verdict |
|---|---|
| Safe Testnet adapter; fixed host; LIVE rejected | PASS |
| Authenticated read-only smoke | PASS |
| `/order/test` validation | PASS |
| Exactly one real bounded Testnet order | PASS — one BUY |
| Durable PREPARED/SUBMITTING and deterministic identity | PASS |
| No blind retry or replacement identity | PASS |
| FILLED order and authoritative persisted trade | PASS |
| SAFE reconciliation and exactly-once settlement | PASS |
| Zero duplicate submission/settlement | PASS |
| Crash/restart and fresh-process durable recovery | PASS |
| Portfolio restoration and unmanaged-balance isolation | PASS |
| ACTIVE kill-switch and no recovered submission capability | PASS |
| No LIVE behavior or credential leakage | PASS |
| Additive migration/recovery integrity, versions 001–006 | PASS |
| Controlled Testnet BUY **and SELL** with persisted trades | **GAP — SELL absent** |

Accepted review results remain: full pytest `1,875 passed`, zero skipped, combined
line/branch coverage `95%`, recovery-proof module `96%`, strict ResourceWarning suite
`1,875 passed`, Ruff check/format PASS, `pip check` PASS, public imports PASS,
repository audit PASS WITH GAPS (then-known gaps non-blocking), and `git fsck --full`
PASS. M4.11 is documentation-only and does not rerun those accepted suites.

## Boundary and next work

The proven scope is safe bounded Testnet BUY execution and recovery. It does not prove
profitability, authorize continuous or signal-driven Testnet execution, authorize LIVE,
or support cancellation. The exact next work is the **Stage 4 controlled managed-balance
SELL evidence closure**. It is not started or authorized. Stage 5 Production Readiness
must remain not started until the missing accepted Stage 4 criterion is closed and a new
completion verdict is issued.
