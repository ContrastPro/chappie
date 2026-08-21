# Architecture decisions

This file records the durable structural decisions completed by the repository
refactor. Detailed operational and semantic decisions remain in their owning
documents and frozen contracts.

## Restricted application root

Direct `app/*.py` modules hide ownership and encourage cross-subsystem coupling.
The root is therefore limited to the package marker, stable CLI/runtime adapters,
and the frozen research methodology surface. New features go to an owning
package; the allowlist can change only through explicit architecture review.

## Neutral foundation

Configuration and logging moved to `app.foundation` because every composition
path needs them and they must not depend on any domain or runtime service. This
is a low-level dependency, not a miscellaneous utilities package.

## Runtime as composition

`app.runtime` owns application construction, lifecycle, and status projection.
It may depend downward on domain and application services. Those services cannot
import runtime. Business rules stay with their domain owners.

## Stable Knowledge and Control Plane APIs

`app.knowledge` and `app.control_plane` are stable package APIs with explicit
exports and canonical implementation modules beneath them. This preserves
consumer contracts without compatibility wrapper modules or blanket exports.
Knowledge remains below the Control Plane; it never imports the interface layer.

## Lazy AI exports

The `app.ai` package exposes selected public contracts lazily so Knowledge and AI
schemas can cooperate without an eager import cycle. Dynamic targets are
explicitly inventoried and tested. Lazy loading is a bounded cycle-prevention
mechanism, not a general service locator.

## Offline research

Research data, intelligence, and strategy evaluation live under `app.research`.
They can consume immutable/read-only domain contracts but never control runtime
trading. Allowlisted source moves require a clean-snapshot Knowledge provenance
migration; semantic contract identities do not change for file moves.

## OS-specific platform adapters

Shared installer/readiness contracts live below the macOS and Windows Ollama
adapters. OS behavior remains isolated under `app.platform`; setup composes and
verifies adapters without acquiring trading authority.

## VS Code internal boundary

`tools/vscode-chappie/src/extension.ts` remains the composition root. `api/`
owns Control Plane transport, `workspace/` owns local context/process paths, and
`ui/` owns presentation/chat. Nested runtime output is packaged explicitly and
compiled tests are excluded.

## Compatibility wrappers are exceptional

Direct migration is the default. A wrapper requires a documented external
compatibility contract, an owner, tests, and a removal plan. Stable package APIs
and stable entrypoints are preferred to parallel old/new implementations.
