# M2.6 Shadow Smoke-Run Report

- Date: 2026-08-04
- Result: **PASS**
- Mode: paper with shadow decisions enabled
- Exchange data: Binance public market data
- Stream: `BTCUSDT`, `1m`
- Run ID: `12398c13-eec6-4ed4-b64c-6fd51741c7af`
- Duration: approximately 8 minutes 9 seconds

## Runtime evidence

Bootstrap loaded 1000 candles successfully and emitted one bounded INFO summary.
The cache remained bounded at 1000 candles. All 49 polling cycles succeeded with
zero polling and delivery failures. A regular close cycle confirmed the atomic
`OPEN -> CLOSED_CONFIRMED` transition with `updated=1`, `inserted=1`, `evicted=1`,
`recovery=False` and successful delivery.

Shadow processing received eight real `CandleClosed` events and produced eight
successful HOLD decisions. Final counters were `handled=8`, `successful=8`,
`hold=8`, `failed=0`, `observation_failures=0` and `duplicates=0`.

The run stopped gracefully through `Ctrl+C` with `exit_code=0`. No Risk Manager,
portfolio mutation, execution adapter, paper order or live order path was invoked.

## Clock skew

The run reported local-minus-exchange clock skew of approximately `-3,031,000 ms`.
This warning did not affect market validation because authoritative market time was
obtained from Binance server time. Local wall time remained diagnostic only;
monotonic time controlled cadence and duration measurement.

No full runtime log, candle payload, credential or secret is included in this report.
