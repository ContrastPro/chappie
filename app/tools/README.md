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
- `strategy_evaluate` compares the baseline and explicitly selected pure research strategies across independent
  local datasets. Each window starts from the same capital; exported reports
  are local research artifacts. It accepts paths or a small manifest such as
  `{"datasets": ["trend.json", "range.json"]}`.
- `strategy_walk_forward` performs a deliberately small offline preset study
  using a manifest with disjoint `train`, `validation`, and `test` lists. It
  selects only on TRAIN, validates that fixed shortlist once, freezes it before
  TEST, and reports cost/activity flags. It has no exchange, credential, or
  server-runtime path.
- `long_history_download` uses bounded public `GET /api/v3/klines` pagination
  to build immutable monthly BTCUSDT chunks. `long_history_analyze` consumes
  only those verified chunks and writes causal-feature / forward-label research
  summaries. Both are local research workflows, never runtime inputs.
- `market_intelligence_download` makes bounded public, credential-free Binance
  USD-M funding-rate or recent open-interest requests and stores immutable,
  digest-verified context inputs. `market_intelligence_analyze` joins local BTC,
  ETHUSDT, ETHBTC, funding and OI inputs with explicit as-of semantics. Missing
  or stale optional context stays unavailable; it is never silently filled.
  The generated market-intelligence report is descriptive research only, never
  a strategy or runtime input.

The cost-aware research candidates are `cost_aware_momentum`,
`regime_mean_reversion`, `multi_timeframe_trend`, `volatility_continuation`,
`trend_retracement_continuation`, and `compression_expansion_breakout`. They
are not runtime defaults. Their cost hurdle is an explicit deterministic
screening proxy, not a return forecast; multi-timeframe candidates derive only
completed higher bars from one immutable replay stream and do not add live
multi-stream I/O. The latter three implement the research TOP-3 hypotheses;
the retracement model uses no Fibonacci-specific levels.

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
