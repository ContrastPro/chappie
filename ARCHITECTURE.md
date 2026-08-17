# Chappie Trader Architecture

This document is the current, high-level system map. The detailed, normative
contracts live in [docs/core/](docs/core/); historical milestone evidence is
separate under [docs/historical/](docs/historical/README.md).

## Active MVP

Chappie is a modular, in-process Python application for BTCUSDT Spot research
and controlled trading simulation. Paper is the normal mode. Testnet is an
explicit, readiness-gated MVP path using the active exchange schema **v4**.
LIVE execution is structurally unavailable.

```text
CHAPPIE
├── Market System
│   completed exchange candles → bounded cache → synchronous events
├── Trading Decisions
│   strategy signal → Planner intent → mandatory Risk decision
├── Decision Runtime
│   one coordinator evaluates each confirmed close once for composed consumers
├── Risk
│   deterministic Decimal sizing and approval/rejection envelopes
├── Paper Trading
│   deterministic fills → durable local cycle → authoritative Portfolio
├── Persistence
│   SQLite single-writer stores, recovery verification, and local audit facts
├── Exchange Execution
│   Testnet PREPARED → SUBMITTING → at most one POST → reconciliation/settlement
├── Operations
│   status snapshot, graceful shutdown, reset/soak procedures, SAFE_STOP
└── Research / Market Intelligence
    offline datasets, evaluation, and hypotheses; never an execution authority
```

## Runtime flow

```text
startup
  → configuration and mode boundary
  → owned database and verified recovery
  → Testnet reconciliation + settlement + readiness (when configured)
  → market polling
  → confirmed CandleClosed
  → one shared decision
  → Paper and/or ready Testnet consumer
  → durable facts, authoritative reconciliation/settlement, Portfolio
  → repeat
```

The active Testnet path persists a durable command and `PREPARED` order before
the final gate, records `SUBMITTING` before its single local POST boundary, and
treats timeout, lost response, interruption, or uncertain exchange state as
reconciliation-only. Local durability cannot prove exchange-side exactly once.

## Safety invariants

- Financial values are finite `Decimal` values; invalid data fails closed.
- Strategies, research, monitoring, and AI are advisory: none can bypass Risk
  or call an exchange adapter directly.
- Paper is local and credential-free; Testnet and LIVE are separate
  capabilities; LIVE remains unavailable.
- Portfolio is the financial accounting authority. Persistence records local
  facts; it never fabricates exchange outcomes or manually repairs inconsistency.
- Testnet has at most one local POST per durable command. Ambiguity routes to
  reconciliation, never automatic retry.
- An unexpected Testnet execution-consumer failure activates process-local
  `SAFE_STOP`; subsequent submissions are blocked until a fresh process.
- The active runtime exclusively owns its SQLite database. SQLite owns its
  WAL/SHM sidecars; monitors use the sanitized status snapshot instead of
  competing runtime SQL access.

## Canonical contracts

| Area | Authority |
| --- | --- |
| Market cache, events, polling | [Market system](docs/core/MARKET_SYSTEM.md) |
| Strategy, Planner, Risk boundary | [Trading decisions](docs/core/TRADING_DECISIONS.md) |
| Coordinator, consumers, SAFE_STOP | [Decision runtime](docs/core/DECISION_RUNTIME.md) |
| Paper execution and Portfolio accounting | [Paper trading](docs/core/PAPER_TRADING.md) |
| Financial and exchange-risk policy | [Risk](docs/core/RISK.md) |
| SQLite ownership, recovery, schemas | [Persistence](docs/core/PERSISTENCE.md) |
| Testnet lifecycle and reconciliation | [Exchange execution](docs/core/EXCHANGE_EXECUTION.md) |

## Operating and research boundaries

The [operations runbook](docs/operations/SERVER_MVP_RUNBOOK.md) defines the
server contract, Paper soak workflow, Testnet startup procedure, status JSON,
and disposable database reset policy. [Research documentation](docs/research/README.md)
defines the offline-only research boundary. Generated research data is ignored,
and a research result is never an approved strategy by itself.

M4.10, Stage 4, their schema-v5–v10 experiments, and one-shot recovery tools
are historical evidence only. They are not imported by normal MVP composition
and do not define fresh Testnet readiness. See the [historical index](docs/historical/README.md).
