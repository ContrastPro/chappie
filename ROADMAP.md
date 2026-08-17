# Chappie Trader Roadmap

This is a forward-looking roadmap, not a record of completed milestones. Current
contracts are in [docs/core/](docs/core/), decisions are in [DECISIONS.md](DECISIONS.md),
and completed evidence is under [docs/historical/](docs/historical/README.md).

## Current

- Maintain the safe active MVP: Paper-first operation, explicit Testnet v4
  readiness, durable lifecycle/reconciliation/settlement, and process-local
  `SAFE_STOP` on unexpected execution failure.
- Complete Research Data Engine v2, including compact storage plus the alignment,
  outcome, and matching layer needed for repeatable historical analysis.
- Complete V4C leverage-stress research and determine whether incremental basis,
  open-interest, and funding signals add robust value beyond existing controls.
- Preserve the separation between generated research data and runtime stores.

## Next

- Finish bounded, reproducible research reports and evaluate whether any
  hypothesis survives causal, cost-aware, and out-of-sample review.
- Build News/Event Intelligence and Social Intelligence as research inputs only.
- Develop a Market Intelligence Agent that produces explainable market-state
  observations, never direct execution instructions.
- Define a Strategy Agent and adaptive strategy lifecycle: hypotheses,
  evaluation, rejection/approval evidence, Paper validation, and rollback.
- Validate any separately approved strategy through extended Paper operation
  before considering Testnet order capability.

## Later

- Broaden market-data and market-intelligence coverage only where provenance,
  completeness, and causal evaluation remain explicit.
- Improve operational observability, deployability, and Paper soak evidence.
- Consider further Testnet hardening only after an explicit design and safety
  review.
- Consider a separate LIVE-admission program only after sustained Paper/Testnet
  evidence, reviewed controls, strict limits, and an explicit decision. LIVE is
  unavailable today.

## Non-goals for the current MVP

- No claim that backtests, research, Paper, or Testnet results imply future
  profitability.
- No strategy, model, or intelligence layer can bypass deterministic Risk or
  execution safety boundaries.
- No manual financial-state repair to preserve disposable development/Testnet
  experiments.
