# Tool classification

`app.tools` is not part of normal server composition. Start the server with
`python -m app.main` and follow [`SERVER_MVP_RUNBOOK.md`](../../SERVER_MVP_RUNBOOK.md).

## ACTIVE_MVP

There is no separate operational one-shot tool for the continuous MVP. Paper and
Testnet are configured and started through `app.main`; the normal runtime uses
the schema-v4 order, reconciliation, settlement, and Portfolio flow.

## HISTORICAL

The following modules preserve M4.10/Stage 4 experiment and forensic evidence
only. They are deliberately not imported by normal MVP composition and are not
recommended server commands:

- `testnet_phase1`, `testnet_phase2`, `testnet_phase3`, `testnet_phase4`
- `testnet_phase4_remediate_zero_fee`
- `testnet_m410_recovery_proof`
- `testnet_stage4_sell`

These tools retain their own explicit authorization and safety constraints. Their
presence does not authorize network access, Testnet orders, or LIVE execution.
