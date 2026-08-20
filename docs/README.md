# Chappie documentation

## Current

- [Project overview](../README.md)
- [Current architecture](../ARCHITECTURE.md)
- [Roadmap](../ROADMAP.md)
- [Vision](../VISION.md)
- [Architecture decisions](../DECISIONS.md)

## Core

These are the current authoritative design references:

- [Market system](core/MARKET_SYSTEM.md)
- [Trading decisions](core/TRADING_DECISIONS.md)
- [Decision runtime](core/DECISION_RUNTIME.md)
- [Paper trading](core/PAPER_TRADING.md)
- [Risk](core/RISK.md)
- [Persistence](core/PERSISTENCE.md)
- [Exchange execution](core/EXCHANGE_EXECUTION.md)

## Operations

Operator-facing runbooks live in [operations/](operations/README.md).

## Research

- [CLI and bootstrap](cli/README.md) — product control plane and offline setup diagnostics.

Research documentation lives in [research/](research/README.md).

## Knowledge

[Chappie Knowledge v1](knowledge/README.md) is the deterministic, provenance-bound
project-knowledge index. It is separate from AI prompts and runtime state.

## Control Plane

[Chappie Local Control Plane v1](control_plane/README.md) is the frontend-neutral,
read-only application boundary for current CLI status and future local consumers.

## VS Code

[Chappie VS Code Extension v1](vscode/README.md) consumes the local Control Plane for
read-only connection, status, diagnostics, and capability visibility.

## Historical evidence

Completed milestone and Stage 4/M4.10 evidence is indexed in
[historical/](historical/README.md). It is not current operational guidance.
