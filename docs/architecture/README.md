# Chappie Architecture Map V1

This directory is the authoritative human-readable system map for Chappie: a safety-first Spot
trading research project with a deterministic Paper runtime, an explicitly gated Binance Spot
Testnet MVP, and a separate AI-assisted development platform. It does not claim profitability,
and LIVE execution is structurally unavailable.

## Architecture authorities

- `workspace.dsl` owns C4 meaning, boundaries, relationships, views, and current/planned status.
- `architecture.yaml` maps those components to code and declares a small set of high-value
  dependency constraints.
- `diagrams/*.mmd` explain workflows where C4 is not sequential enough.
- `generated/*` is deterministic supplemental evidence derived from the registry and static AST
  imports. Generated dependencies do not invent C4 semantics.
- `tests/test_architecture_boundaries.py` remains the detailed Python ownership authority.

## How to read the map

Start with `system-context`, then select the bounded Level 2 view for Core, Paper, Testnet,
Research, AI/Development, or planned research. Level 3 views show only architecturally important
components. Implemented elements use solid styling; Experimental and Planned elements are dashed;
Historical elements are muted; safety-critical boundaries carry a stronger red border.

Current and Planned are deliberately separate. Planned research components produce offline
research context only and have no relationship to Risk, Testnet, or LIVE execution.

## Important terminology

`TraderAgent` is currently a small runtime lifecycle wrapper around the polling engine. It is not
an autonomous LLM agent. Development Intelligence is an AI-assisted, read-only analysis and
advisory-handoff workflow, not a multi-agent runtime.

There is no general-purpose Memory subsystem. `Runtime State & Caches` covers process-local caches,
deduplication, durable SQLite state, and bounded cached evidence. `Knowledge` is different: it is a
clean-commit, allowlisted, provenance-bound store with deterministic retrieval, and is explicitly
not model memory.

## Research and execution isolation

Research is offline. It may read canonical data and strategy contracts and may write ignored
evidence, but it cannot enter runtime composition. Strategies are advisory and cannot access
credentials or persistence. Every actionable intent passes deterministic Risk. Testnet additionally
requires readiness, durable state, reconciliation, settlement, and a process-local capability.
Ambiguity is reconciled rather than blindly retried.

## Market Precursor Discovery

The released flow is:

```text
Historical BTC/ETH/SOL authority
→ Base Precursor V1
→ Multi-Market Context Authority
→ Typed Context V2
→ Authority Adapter
→ Multi-Horizon Observations
→ FEATURE_RESEARCH Quantiles
→ Frozen 8-Condition Catalogue
→ Research/Validation Evaluation
→ Temporal Stability
→ Candidate Classification
→ Ranking
→ chappie_precursor_discovery_report/2.0.0
```

The report is research evidence only. Architecture metadata intentionally excludes individual
candidate results and other volatile statistics.

## Rendering

Open `workspace.dsl` with Structurizr Lite or validate it with the Structurizr CLI when that tool is
already available. No Structurizr binary is required by the generator. Mermaid-aware Markdown
editors can render files under `diagrams/`; they are plain Mermaid source and require no repository
Node dependency.

## Generate and check

From the repository root:

```text
python tools/architecture/generate.py
python tools/architecture/generate.py --check
python tools/architecture/generate.py --status
```

Generate mode atomically refreshes the four generated artifacts. Check and status modes are
read-only. Actionable drift includes missing declared paths, unmapped application modules, stale
outputs, malformed registry data, or forbidden component dependencies. An unmapped package reports
`ARCHITECTURE_MODEL_UPDATE_REQUIRED`; the generator never creates a component automatically.

## When a human C4 update is required

Update `architecture.yaml` and `workspace.dsl` when a change modifies subsystem ownership, an
architectural dependency, a major data flow, an external integration, or a public architecture
contract. Ordinary internal refactors and tests do not require a C4 update unless they cause one of
those changes.

## Standard future Codex pre-commit workflow

For future final commit tasks, Codex should perform this automatically:

```text
tests
→ python tools/architecture/generate.py
→ python tools/architecture/generate.py --check
→ git diff --check
→ stage
→ commit
→ push
```

The user should not need to maintain generated architecture artifacts manually.
