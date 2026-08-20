# Chappie Local Control Plane v1

`chappie_control_plane/1.2.0` is Chappie's frontend-neutral local application boundary.
It projects bounded, typed status and capability information for the product CLI today
and for a future VS Code extension or local transport later.

## Phase 1 scope

The Control Plane exposes:

- system/project status;
- doctor diagnostics;
- AI provider/configuration status; and
- a capability inventory derived from implemented Chappie surfaces.
- one explicit, non-streaming `ask_project(question)` operation. It reads an existing
  verified Knowledge store, applies canonical retrieval, and invokes `PROJECT_QA_V1` only
  when a configured local provider is already usable.

Project QA has read-only application authority: it does not write Knowledge, mutate the
repository, execute RDE or Trading work, change configuration, start a provider, or pull
a model. It is not generic chat and accepts only an explicit bounded project question.
Responses are deterministic,
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
loopback-only and application read-only; it is not a remote API or an agent interface.

`WorkspaceContextV1` is an optional user-supplied, request-scoped input to Project QA. It
is explicitly non-canonical: its facts use `USER_SUPPLIED_WORKSPACE_CONTEXT`, never
`SOURCE_CODE` or Knowledge authority. It contains only bounded relative active-document
metadata/text and optional Git metadata; it is not persisted or ingested into Knowledge.
