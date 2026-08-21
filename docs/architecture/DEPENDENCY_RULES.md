# Dependency rules

Imports follow ownership and authority, not convenience. “Allowed” means the
dependency is permitted when needed; it does not require that dependency.

## Package matrix

| Source family | May depend on | Must not depend on |
| --- | --- | --- |
| `foundation` | standard library, neutral declared libraries | every upward Chappie package |
| `market` | its own models/ports | runtime, platform, UI, AI, research |
| `trading`, `strategies`, `risk`, `portfolio`, `execution` | lower domain contracts explicitly listed in the ownership map | runtime, platform, Control Plane, tools, AI, research |
| `persistence` | persisted domain models and repository ports | UI, setup, research, AI authority |
| `infrastructure` | ports/contracts it implements | runtime composition, Control Plane, UI, research |
| `paper`, `shadow`, `exchange_execution` | bounded domain and persistence contracts | platform/UI/research/AI authority |
| `coordination`, `exchange_runtime`, `runtime` | application services, domain, adapters, foundation | offline research and AI authority |
| `platform.ollama` | shared platform contracts and AI model-profile schema | runtime, Control Plane, trading/Risk/execution, research, tools |
| `platform.setup` | platform adapters, Knowledge and AI verification contracts | runtime/trading authority, Control Plane, research, tools |
| `knowledge` | Knowledge internals and narrow AI schema lookup | Control Plane, platform, runtime, trading authority |
| `ai` | AI internals, Knowledge/research contracts where explicitly bounded | runtime, trading, Risk, execution, portfolio authority |
| `ai.research` | AI contracts, Knowledge and offline research | platform, Control Plane, runtime/domain authority |
| `ai.evaluation` | AI contracts and immutable research evidence | provider runtime, platform, Control Plane, domain authority |
| `research` | offline research internals and read-only domain contracts | runtime/platform/Control Plane authority |
| `control_plane` | AI, Knowledge and bounded platform status | runtime/trading/execution authority and persistence mutation |
| `tools` | production packages | imported by production packages |
| VS Code `api` | transport contracts and workspace context type | UI |
| VS Code `workspace` | Node/VS Code workspace boundaries | UI |
| VS Code `ui` | API DTOs and bounded workspace abstractions | backend authority or setup logic |

The machine-readable source of the Python ownership policy is
`PACKAGE_OWNERSHIP` in `tests/test_architecture_boundaries.py`. Nested owners
override their parent policy. The test suite rejects imports outside each
owner’s allowed internal families.

## Permanent prohibitions

- Production never imports `app.tools`.
- Foundation never imports upward.
- Domain packages never import runtime, platform, UI, AI, or research.
- Runtime and trading paths never import offline research.
- Research never controls runtime behavior.
- AI never imports or owns trading, Risk, portfolio, or execution authority.
- Knowledge never imports Control Plane.
- Control Plane never imports runtime trading/execution authority.
- Platform/setup never owns a domain decision.
- Eager AI↔Knowledge cycles are forbidden. Lazy/dynamic imports remain bounded by
  the explicit dynamic-import inventory.
- Production module import SCCs are forbidden; type-only relationships should
  use narrow protocols and must not create an eager cycle.

## Root and composition policy

The only direct `app/*.py` files are:

- `__init__.py` — package marker;
- `cli.py` — stable CLI adapter and composition boundary;
- `main.py` — stable runtime process adapter; and
- `research_methodology.py` — frozen methodology contract retained as an
  intentional root compatibility/provenance surface.

Any new direct root module requires an explicit allowlist change, written
architectural justification, and review. Feature code should almost never be
placed there.

## I/O, state, and contracts

OS/network/subprocess I/O belongs to platform or infrastructure adapters.
Durable writes and recovery belong to persistence repositories. UI and Control
Plane code can request or project state but cannot mutate another subsystem
directly. Cross-package contracts live at their lowest stable semantic owner and
are imported rather than duplicated.

Platform subprocesses use argv arrays, never shell command concatenation.
External downloads use fixed trusted sources, verified TLS, bounded timeouts,
and action-scoped consent. Readiness loops are bounded and fail closed.

## Versioned and provenance-sensitive changes

A semantic change to a versioned/frozen contract requires a version bump,
digest update, dependent tests/contracts, and migration documentation. A source
move alone preserves semantic identity.

Canonical Knowledge uses a clean committed snapshot and explicit
`SOURCE_ALLOWLIST`. Dirty ingestion is forbidden. Source moves update canonical
locators; the prior store must report stale; rebuild and grounded Project QA
validation are required before acceptance.
