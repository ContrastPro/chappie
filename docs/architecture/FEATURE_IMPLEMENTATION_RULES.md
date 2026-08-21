# Feature implementation rules

Every Chappie feature starts with ownership, not a filename. Before code is
written, classify the change across these responsibilities:

1. **Domain** — deterministic market, trading, strategy, Risk, portfolio, or
   execution rules.
2. **Application/orchestration** — sequencing existing services and lifecycle.
3. **Infrastructure** — external APIs, exchange adapters, storage, clocks, or I/O.
4. **Platform** — host OS, installation, local providers, setup, or workstation.
5. **UI/interface** — CLI, read-only Control Plane, or VS Code behavior.
6. **AI/research** — non-authoritative reasoning, evaluation, or offline evidence.
7. **Persistence** — durable schemas, repositories, migrations, or recovery.

The package owning the invariant owns the implementation. Cross-layer features
are split into a domain contract, an application service, adapters, and an
interface composition change; they are not placed in one convenient root module.

## Placement decision tree

```text
Does it change a deterministic trading decision or approval?
  strategy -> app.strategies
  trading intent/pipeline -> app.trading
  risk limit/decision -> app.risk
  portfolio/accounting -> app.portfolio
  execution contract -> app.execution or app.exchange_execution

Does it only sequence services or lifecycle?
  process-wide -> app.runtime
  exchange-mode -> app.exchange_runtime
  cross-service workflow -> app.coordination

Does it talk to storage or an external system?
  durable repository/recovery -> app.persistence
  exchange/external adapter -> app.infrastructure
  OS/install/local provider -> app.platform

Is it offline evidence or a study?
  data/provenance -> app.research.data
  market intelligence -> app.research.intelligence
  strategy evaluation -> app.research.strategy

Is it AI?
  product reasoning/provider contract -> app.ai
  RDE/evidence workflow -> app.ai.research
  benchmark/calibration -> app.ai.evaluation

Is it canonical project knowledge?
  provenance/store/retrieval -> app.knowledge

Is it a read-only project endpoint?
  service/transport/workspace context -> app.control_plane

Is it IDE behavior?
  Control Plane client -> vscode src/api
  workspace/process/context -> vscode src/workspace
  rendering/interaction -> vscode src/ui
  composition only -> vscode src/extension.ts
```

If no existing package owns the invariant cleanly, stop and request an
architecture review. Do not silently create a new layer or direct `app/*.py`
module.

## Examples

| Feature | Placement |
| --- | --- |
| New exchange adapter | Port/model in the owning domain boundary; implementation in `app.infrastructure`; composition in `app.exchange_runtime` |
| New trading strategy | `app.strategies`; advisory output through existing `app.trading` contracts |
| New Risk rule | `app.risk`; deterministic tests; no AI or UI dependency |
| New portfolio feature | Models/invariants in `app.portfolio`; repository changes in `app.persistence` |
| New AI analysis capability | Task/schema/provider-neutral service in `app.ai`; research-only workflow in `app.ai.research` |
| New research study | `app.research.intelligence` or `app.research.strategy`; CLI wrapper in `app.tools` if needed |
| New Control Plane endpoint | Read-only projection in `app.control_plane.service`; loopback schema/route in `transport` |
| New VS Code action | UI/workspace/API module by responsibility; registration and composition in `extension.ts` |
| New platform installer | Shared contract in `app.platform`; OS adapter below `app.platform`; setup wiring in `app.platform.setup` |
| New persistence repository | Port with the owner when domain-facing; implementation and migration in `app.persistence` |
| New data model | Package that owns the invariant; DTO only at a stable cross-package boundary |
| New background agent | First classify its authority; orchestration in `app.runtime` or `app.coordination`, never an autonomous authority package |
| User feature spanning layers | Separate domain rule, application service, adapter, interface endpoint, and UI wiring |

## Small feature or bounded package

A small feature uses the smallest coherent implementation: one existing module,
its tests, and an existing contract if applicable. Do not create folders,
factories, ports, or classes solely for symmetry.

A feature large enough to contain independent contracts, orchestration, and
adapters may use:

```text
feature/
├── models.py       # owned domain/value models
├── contracts.py    # stable DTOs, enums, identities
├── ports.py        # narrow dependency interfaces
├── service.py      # application behavior
└── adapters/       # I/O implementations, only when multiple are warranted
```

Names are optional; responsibilities are mandatory. Split only when cohesion or
dependency direction improves.

## Chappie OOP rules

- Give each class one lifecycle or invariant-owning responsibility.
- Depend on narrow ports/`Protocol`s, not concrete higher-layer services.
- Prefer interface segregation and composition over inheritance.
- Use explicit domain models; make contracts immutable where identity or
  provenance matters.
- Do not create god classes, service locators, hidden mutable globals, or direct
  cross-subsystem mutation.
- Adapters implement ports; orchestration composes services; domain code does
  not know infrastructure.
- Prefer a simple function for stateless deterministic transformations.

## Contract rules

- Schemas, DTOs, `Protocol`s, enums, immutable IDs, versions, digests, and frozen
  policies live with the lowest stable owner of their semantics.
- Import a canonical contract; never copy it into another package or UI.
- Keep provider- or transport-specific shapes at their adapter boundary.
- A semantic change to a frozen contract requires a version and digest change,
  dependent contract/test updates, migration documentation, and a Knowledge
  rebuild when an allowlisted source is affected.
- Structural moves and import changes preserve semantic identities.

## Testing requirements

Every feature needs unit tests in the owning subsystem. Add contract tests for a
cross-package API, integration tests for orchestration/adapters, architecture
coverage for a changed boundary, and platform regressions for OS-specific work.
An allowlisted Knowledge source change requires clean-snapshot rebuild and
Project QA grounding validation. VS Code changes require `npm run compile`,
`npm test`, `npm run lint`, and package inspection.

## Automated coding-agent contract

Before implementation, Codex or another coding agent must:

1. identify the owning package and layer;
2. list required dependencies and verify their direction;
3. reuse existing ports/contracts where applicable;
4. avoid new direct root modules and duplicate contracts;
5. keep behavior changes separate from structural changes;
6. add tests in the owning subsystem;
7. update architecture documents only when a real boundary changes;
8. run architecture guards and relevant subsystem validation;
9. preserve frozen versions/digests unless semantics intentionally change; and
10. stop for architecture review if no current package cleanly owns the feature.

An automated agent must not invent a new architectural layer, compatibility
wrapper, authority path, or persistent global as an implementation shortcut.
