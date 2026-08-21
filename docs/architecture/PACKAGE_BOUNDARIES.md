# Package boundaries

This document freezes the repository boundary contract before the namespace
refactor. It describes current and target ownership; it does not itself change
runtime behavior or authorize a module move.

## Stable paths

The following paths are frozen compatibility surfaces during the initial
refactor:

- `app.cli` — console-script adapter (`chappie = app.cli:main`)
- `app.main` — process-module adapter (`python -m app.main`)
- `app.persistence.migrations` — packaged migration resources
- `tools/vscode-chappie/dist/` — tracked VSIX distribution and `SHA256SUMS`

`app.cli` and `app.main` remain thin adapters even when their implementations
move into the target packages. Historical `app.tools` module entry points are
evaluated individually; documented command surfaces receive a temporary wrapper
only when a direct migration would break an operator contract.

## Target package tree

```text
app/
├── cli.py
├── main.py
├── foundation/
├── runtime/
├── platform/
│   ├── setup/
│   └── ollama/
├── control_plane/
├── knowledge/
├── research/
│   ├── data/
│   ├── intelligence/
│   └── strategy/
├── ai/
│   ├── evaluation/
│   └── research/
├── market/
├── trading/
├── strategies/
├── risk/
├── portfolio/
├── execution/
├── paper/
├── shadow/
├── coordination/
├── persistence/
├── exchange_execution/
├── exchange_runtime/
├── infrastructure/
└── tools/
```

## Responsibilities

| Area | Responsibility |
| --- | --- |
| `market`, `trading`, `risk`, `portfolio`, `execution` | Domain contracts, deterministic decisions, approvals, fills, and accounting. |
| `persistence`, `infrastructure` | Durable storage and external adapters implementing bounded ports. |
| `paper`, `coordination`, `exchange_runtime`, `shadow` | Application/runtime services. |
| `foundation` | Neutral configuration and logging primitives. |
| `runtime` | Process composition, status, and service lifecycle. |
| `platform` | Setup, local-provider installation, workstation integration, and bounded smoke checks. |
| `control_plane` | Read-only local application projection and loopback transport. |
| `knowledge` | Provenance-bound source ingestion and deterministic retrieval. |
| `research` | Offline data infrastructure and reproducible studies. |
| `ai` | Provider-neutral structured reasoning and research assistance without operational authority. |
| `tools` | Explicit operator, research, and historical command entry points. |

## Dependency rules

1. Domain models and ports do not depend on runtime, CLI, setup, or tools.
2. Production code never imports `app.tools`.
3. Runtime trading/application paths do not depend on offline research.
4. Research cannot influence runtime trading behavior.
5. AI has no Risk, execution, or trading authority.
6. Knowledge owns provenance and retrieval, not generation authority.
7. The Control Plane is read-only and non-authoritative for trading.
8. Platform/setup may compose AI, Knowledge, installers, and workstation integration.
9. `app.cli` and `app.main` remain thin stable adapters.
10. Historical modules are not active production contracts.

## Migration policy

Direct root-level production modules are temporarily allowlisted in
`tests/test_architecture_boundaries.py`. The allowlist may only shrink. A new
root module requires an explicit architecture decision rather than silently
becoming part of the pre-refactor namespace.

Compatibility wrappers are selective, time-bounded compatibility surfaces; they
are not automatic for every move. Every wrapper needs an owner and a removal
slice. Moving an allowlisted Knowledge source also requires a clean-snapshot
Knowledge rebuild because source paths are part of provenance.
