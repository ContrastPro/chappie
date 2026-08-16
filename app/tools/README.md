# Tool classification

`app.tools` is not part of normal server composition. Start the server with
`python -m app.main` and follow [`SERVER_MVP_RUNBOOK.md`](../../SERVER_MVP_RUNBOOK.md).

## ACTIVE_MVP

There is no separate operational one-shot tool for the continuous MVP. Paper and
Testnet are configured and started through `app.main`; the normal runtime uses
the schema-v4 order, reconciliation, settlement, and Portfolio flow.

## RESEARCH_ONLY

The following explicit commands use no credentials and are not part of server
composition or order execution:

- `market_history_download` makes bounded public `GET /api/v3/klines` requests
  and writes a digest-verified local dataset under `data/research/`.
- `strategy_compare` replays one local JSON candle input without network use.
- `strategy_evaluate` compares all four pure strategies across independent
  local datasets. Each window starts from the same capital; exported reports
  are local research artifacts. It accepts paths or a small manifest such as
  `{"datasets": ["trend.json", "range.json"]}`.
- `strategy_walk_forward` performs a deliberately small offline preset study
  using a manifest with disjoint `train`, `validation`, and `test` lists. It
  selects only on TRAIN, validates that fixed shortlist once, freezes it before
  TEST, and reports cost/activity flags. It has no exchange, credential, or
  server-runtime path.

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
