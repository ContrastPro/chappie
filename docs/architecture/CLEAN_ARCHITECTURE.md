# Chappie Clean Architecture

Chappie separates deterministic trading authority from orchestration, adapters,
offline research, AI assistance, and user interfaces. The central rule is that
information may flow inward to deterministic decision boundaries, but authority
does not leak outward: research and AI can produce evidence or explanations;
they cannot approve risk, submit orders, mutate portfolios, or bypass runtime
coordination.

## Layers and direction

```text
interfaces              app.cli, app.main, app.control_plane, VS Code
composition             app.runtime, app.exchange_runtime, app.coordination
platform                app.platform, app.infrastructure, app.persistence
application services    app.paper, app.shadow, app.exchange_execution
domain                  app.market, app.trading, app.strategies,
                        app.risk, app.portfolio, app.execution

offline side planes     app.research, app.ai, app.knowledge, app.tools
neutral foundation      app.foundation
```

Domain packages never depend on composition, platform, UI, tools, research, or
AI. Composition roots depend on bounded domain and adapter contracts and own
lifecycle, not business rules. Infrastructure implements ports and owns I/O.
Persistence owns durable representation and recovery. `app.foundation` is
usable from any Python layer but cannot import upward.

The side planes are deliberately constrained:

- Research is offline and reproducible. It may consume domain data contracts,
  but runtime code cannot import it and research cannot activate behavior.
- AI is provider-neutral, structured, and read-only. It may reason over supplied
  facts and evidence but owns no trading, Risk, execution, or portfolio authority.
- Knowledge owns clean-snapshot provenance, canonical records, and deterministic
  retrieval. It does not own arbitrary generation or application authority.
- The Control Plane projects read-only state over loopback. It may invoke bounded
  Project QA, but cannot become a runtime or trading composition root.
- Tools are leaf entry points. Production packages never import `app.tools`.

## Package ownership

| Package | Responsibility and public boundary | Allowed dependencies | Forbidden dependencies | New feature code? |
| --- | --- | --- | --- | --- |
| `app.ai` | Structured tasks, schemas, providers, routing, Project QA; lazy package API | AI internals, Knowledge and research contracts | runtime, trading, Risk, execution, platform authority | Yes, for provider-neutral AI capabilities |
| `app.ai.research` | RDE queries, evidence review, research reasoning | AI contracts, Knowledge contracts, offline research | runtime, platform, Control Plane and domain authority | Yes, for research-only reasoning |
| `app.ai.evaluation` | Offline benchmarks and calibration | AI and immutable research evidence | provider runtime, platform, Control Plane, trading authority | Yes, for evaluators only |
| `app.control_plane` | Stable read-only service API, transport, workspace context | AI, Knowledge, bounded platform status | runtime/trading/execution authority and persistence mutation | Yes, for read-only application endpoints |
| `app.market` | Market models, validation, events and provider ports | itself and neutral types | runtime, platform, UI, AI, research | Yes |
| `app.trading` | Intent, snapshots, strategy ports and deterministic pipeline | market contracts | adapters, runtime, UI, AI, research | Yes |
| `app.strategies` | Advisory strategy implementations | market and trading contracts | execution submission, platform, runtime, AI | Yes |
| `app.risk` | Deterministic approvals, limits and rejection reasons | market, portfolio, trading and bounded execution contracts | runtime, platform, UI, AI, research | Yes |
| `app.portfolio` | Portfolio state and accounting | market, trading and execution contracts | runtime, platform, UI, AI, research | Yes |
| `app.execution` | Execution models, provider ports and paper boundary | domain contracts | platform/UI/research/AI authority | Yes |
| `app.exchange_execution` | Durable external-execution lifecycle and reconciliation | market and persistence ports | runtime/UI/research/AI | Yes, for execution application logic |
| `app.exchange_runtime` | Exchange-mode application services and composition | domain, adapters, persistence, foundation | offline research, AI authority, UI | Yes, for exchange orchestration |
| `app.paper` | Paper application service | domain and persistence ports | platform/UI/research/AI authority | Yes |
| `app.shadow` | Read-only shadow observation and comparison | market and trading contracts | execution authority, platform/UI | Yes |
| `app.coordination` | Cross-service trading workflow coordination | bounded runtime services and trading contracts | research, AI, platform UI | Yes, for orchestration only |
| `app.runtime` | Application composition, process lifecycle and status | domain/application services, adapters, foundation | offline research and AI authority | Yes, only for composition/lifecycle |
| `app.foundation` | Configuration and logging primitives | standard library and declared neutral libraries | every upward Chappie package | Rarely; only neutral primitives |
| `app.infrastructure` | External adapters implementing ports | the ports/contracts implemented | runtime composition, UI, research | Yes, for external-system adapters |
| `app.persistence` | Durable repositories, migrations and recovery | persisted domain contracts | UI, setup, research, AI authority | Yes, for storage/recovery |
| `app.platform` | Host bootstrap and workstation integration | platform contracts plus bounded AI/Knowledge verification | trading/risk/execution authority | Yes, for host concerns |
| `app.platform.ollama` | Shared and OS-specific Ollama installation/readiness adapters | shared platform and AI model-profile contracts | domain/runtime/Control Plane/research | Yes, for supported OS adapters |
| `app.platform.setup` | Setup orchestration and smoke verification | platform, AI and Knowledge contracts | domain authority and runtime trading composition | Yes, for setup lifecycle only |
| `app.knowledge` | Stable API for provenance store and retrieval | Knowledge internals and narrow AI schema contract | Control Plane, platform, runtime and domain authority | Yes, for canonical records/retrieval |
| `app.research` | Offline data paths and reproducible studies | research internals and read-only domain contracts | runtime/platform/Control Plane authority | Yes, for offline research |
| `app.research.data` | Storage, alignment, outcomes and cache identity | research/data contracts | runtime authority | Yes |
| `app.research.intelligence` | Offline market, flow, event and behavioral studies | research and read-only market/AI contracts | runtime authority | Yes |
| `app.research.strategy` | Offline history, lab and walk-forward evaluation | research, strategy and market contracts | runtime authority | Yes |
| `app.tools` | Operator, historical and research command entry points | production packages | imported by production | Yes, only for explicit commands |

`app.cli` and `app.main` are stable thin composition adapters.
`app.research_methodology` is the intentional root-level frozen methodology
contract. New feature implementations do not belong at the `app/` root.

## Composition roots and state ownership

- `app.main` creates the bounded runtime through `app.runtime.application`.
- `app.cli` composes setup and Control Plane commands without owning their rules.
- `app.runtime.application` owns process lifecycle and subsystem wiring.
- `app.exchange_runtime.composition` owns exchange-mode wiring.
- `app.platform.setup.engine` owns setup-step orchestration and verification.
- `tools/vscode-chappie/src/extension.ts` composes extension API, workspace, and
  UI modules.

Domain state is mutated only through its owning service. Persistence writes go
through repository ports. Adapters own network, filesystem, OS, or subprocess
I/O. No service locator or hidden mutable global may bypass these boundaries.

## OOP and contracts

Use explicit immutable domain models and versioned DTOs where identity matters.
Adapters implement narrow ports or `Protocol`s; orchestration composes those
interfaces. Prefer composition over inheritance and segregated interfaces over
large multipurpose services. A class needs a lifecycle, invariant, or injected
dependency; a pure deterministic transformation should remain a function.

Contracts live at the lowest stable boundary that owns their meaning. Do not
duplicate schemas, DTOs, enums, identifiers, or policies across packages.
Cross-package contracts must not depend on higher layers. A semantic change to
a frozen/versioned contract requires a version bump, digest update, dependent
tests and migration notes. File moves and import edits alone do not change a
semantic version or digest.

## Provenance and authority

Canonical Knowledge is built only from a clean committed snapshot using the
explicit `SOURCE_ALLOWLIST`. Dirty builds are refused; moved sources require a
locator migration and clean rebuild; a previous store must become explicitly
stale. Project QA remains grounded in retrieved fact IDs and preserves record
authority/status.

AI output is evidence or explanation, never execution permission. Research is
offline evidence, never runtime configuration. Risk and execution remain
deterministic application/domain authorities. Platform setup can install and
verify dependencies, but cannot decide trades.

## VS Code boundary

The extension keeps `extension.ts` as its composition root. `api/` owns Control
Plane contracts and the loopback client; `workspace/` owns paths, process and
bounded editor context; `ui/` owns presentation and chat rendering. API and
workspace modules cannot import UI. UI does not own backend state. Python remains
the source of setup, Knowledge and project truth, and the extension never
duplicates setup or model-consent logic.
