# Stage 1 Completion Checklist

Date: 2026-08-04.

Stage 1 is complete only after every criterion below is confirmed. Automated implementation and tests do not replace the explicit real Binance smoke-run gate.

- [x] Binance REST market data works in a real finite smoke-run.
- [x] Binance OHLCV passes independent candle and sequence validation.
- [x] Market Cache is bounded, in-memory and atomic per batch.
- [x] Polling handles provider retry, bounded backoff and terminal failures.
- [x] Ctrl+C and finite smoke-run lifecycle are covered by deterministic tests.
- [x] Cache and diagnostic events are published in the specified order.
- [x] Unit, contract and integration tests pass without internet access.
- [x] Application composition owns and closes the provider exactly once.
- [x] Secrets are not required for Stage 1 public market data.
- [x] Strategies, order execution and live trading are absent.

## Manual real smoke-run

1. Create `.env` from `.env.example`.
2. Set `SMOKE_RUN_CYCLES=3`.
3. Run `python -m app.main` with network access.
4. Confirm three completed cycles, valid cache state, event logs and exit code `0`.
5. Confirm that no secrets were logged and mark the first checklist item complete.

Verification result: the real finite Binance smoke-run completed successfully on
2026-08-04. Milestone M1.8 may therefore be marked completed.
