# Chappie Trader

Chappie Trader is the financial subsystem of the future Chappie platform. The current Stage 1 application obtains Binance Spot OHLCV data for `BTCUSDT`, validates it, stores bounded normalized state in memory and publishes synchronous typed market events.

The current version does not contain strategies, order execution or live trading. It does not promise or guarantee income.

## Installation

Python 3.13 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Configuration

Copy the example configuration and review its values:

```powershell
Copy-Item .env.example .env
```

Stage 1 accepts only `TRADING_MODE=paper`. Binance market-data endpoints used by this application are public; API keys are not required and must not be added unless a later approved stage needs them.

Important defaults:

- market: Binance Spot;
- symbol: `BTCUSDT`;
- interval: `1m`;
- cache capacity and bootstrap limit: `1000` candles;
- polling cadence: `10` seconds;
- regular request limit: `2` candles.
- clock-skew warning threshold: `5` seconds; Binance server time remains authoritative.

## Finite smoke-run

Set a positive cycle count in `.env`:

```text
SMOKE_RUN_CYCLES=3
```

Then run:

```powershell
python -m app.main
```

The application performs exactly the configured number of polling cycles unless a fatal error occurs, prints the final Stage 1 status through logging and closes the HTTP provider.

## Logs

The application writes two UTF-8 log files:

- `logs/chappie.log` is the cumulative history and is opened in append mode;
- `logs/latest_run.log` is the complete most recent run and is overwritten at startup.

The per-run path can be changed with `RUN_LOG_FILE`. Every line carries the same
`run_id`; the run header contains a safe configuration summary without credentials,
and the footer records the exit code and final lifecycle state. The console remains
concise: detailed market events and HTTP request logs stay in the files, while
warnings and errors are still displayed.

After a smoke-run, inspect its complete log with:

```powershell
Get-Content .\logs\latest_run.log
```

Or open it in Visual Studio Code:

```powershell
code .\logs\latest_run.log
```

## Continuous polling

Leave `SMOKE_RUN_CYCLES` empty and run:

```powershell
python -m app.main
```

Stop with `Ctrl+C`. A normal manual stop completes without a traceback and exits with code `0`.

## Quality checks

```powershell
ruff check .
pytest
git diff --check
```

Automated tests do not require network access. A real smoke-run is a separate manual verification because it contacts Binance and may otherwise run for an unintended duration.
