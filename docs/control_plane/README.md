# Chappie Local Control Plane v1

`chappie_control_plane/1.0.0` is Chappie's frontend-neutral local application boundary.
It projects bounded, typed status and capability information for the product CLI today
and for a future VS Code extension or local transport later.

## Phase 1 scope

Phase 1 is in-process and read-only. `ChappieControlPlaneV1` exposes:

- system/project status;
- doctor diagnostics;
- AI provider/configuration status; and
- a capability inventory derived from implemented Chappie surfaces.

It does not start a provider, pull a model, execute setup, rebuild Knowledge, generate
AI output, execute RDE work, or perform trading actions. Responses are deterministic,
JSON-safe typed contracts with bounded `OK`, `DEGRADED`, `UNAVAILABLE`, or
`INTERNAL_ERROR` states and safe reason codes.

## Authority boundary

The Control Plane is not a model provider, agent, database, HTTP API, or VS Code API.
It preserves ownership: Knowledge owns canonical project facts; RDE owns research
execution and verified evidence; AI may reason only over supplied facts; Trading
authority is outside this read-only boundary. Runtime capability inventory and canonical
Knowledge remain distinct authority sources.

## Consumers and next steps

The CLI is a thin adapter: `chappie ai status` already consumes the service while
preserving its human output. A future VS Code extension can consume the same contracts
through a later local process, IPC, or HTTP transport decision. Future namespaces such
as `agent.trading.status`, `agent.news.status`, `agent.research.status`, and
`agent.developer.status` are reserved by convention only; they are not Phase 1
capabilities.

The initial local HTTP adapter is documented in [Transport v1](TRANSPORT.md). It remains
loopback-only and read-only; it is not a remote API or an agent interface.
