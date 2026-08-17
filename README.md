# Chappie Trader

Chappie Trader is a safety-first Spot trading research and runtime project. It
turns completed market observations into advisory strategy signals, applies
deterministic Risk, and can run the resulting lifecycle in Paper mode or in a
separately configured Binance Spot Testnet MVP. It does not promise returns.

## Current status

Paper is the default supported runtime. The continuous Testnet MVP is available
only with explicit configuration and uses the disposable schema-v4 store. LIVE
trading is structurally unavailable.

Strategies are advisory only. They cannot submit orders, access credentials, or
bypass Planner and Risk. Testnet ambiguity is reconciled, never blindly retried.
An unexpected Testnet execution failure enters process-local `SAFE_STOP`: no
later event in that process can submit, and restarting requires operator
diagnosis or a fresh disposable development database.

## Quick Paper start

Python 3.13 or newer is required.

```text
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
python -m app.main
```

Paper needs no API credentials. Stop with `Ctrl+C`. The Paper dashboard is
read-only:

```text
python -m app.tools.paper_monitor
```

For configuration, database locations, reset policy, server operation, and the
separate Testnet-readiness procedure, use the [Server MVP runbook](docs/operations/SERVER_MVP_RUNBOOK.md).

## Supported modes

| Mode | Status | Boundary |
| --- | --- | --- |
| Paper | Supported | Local deterministic simulation; no private exchange calls. |
| Testnet | Explicit MVP configuration | Schema v4, startup readiness, reconciliation, settlement, and `SAFE_STOP`. |
| LIVE | Unavailable | Rejected by configuration and composition. |

Testnet and development databases are disposable. On unexpected state, stop
safely, preserve DB/logs only when useful for diagnosis, otherwise reset and
bootstrap a fresh store. Do not manually repair financial records to keep an
experiment running.

## Research

Offline research tools support strategy comparison, walk-forward evaluation, and
market-intelligence experiments. Research data under `data/research/` is ignored;
findings are not strategy approval and do not change the runtime without a
separate review. Start at [Research documentation](docs/research/README.md).

## Documentation

- [Documentation map](docs/README.md)
- [Current architecture](ARCHITECTURE.md)
- [Roadmap](ROADMAP.md)
- [Vision](VISION.md)
- [Architecture decisions](DECISIONS.md)
- [Operations runbooks](docs/operations/README.md)
- [Canonical core contracts](docs/core/)

Historical M4.10/Stage 4 material is preserved as evidence under
[docs/historical/](docs/historical/README.md). It is not an active operator
workflow or a prerequisite for fresh MVP startup.
