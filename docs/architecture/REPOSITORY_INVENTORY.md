# Repository Inventory

`chappie_repository_inventory/1.1.0`, owned by `app.repository_inventory`, is a
consumer-neutral, read-only inventory of facts from one verified clean Git
tracked snapshot. It does not classify files as unused, obsolete, dead, safe to
delete, or cleanup candidates.

The adapter executes only fixed read-only Git commands (`rev-parse HEAD`,
tracked-only `status`, and cached `ls-files`). It never enumerates untracked or
ignored paths and never falls back to recursive filesystem discovery. Tracked
files are safely resolved beneath the repository root; source bodies are not
included in the DTO.

Detectors are bounded and deterministic: Python AST imports/declarations,
static JavaScript/TypeScript imports, package/TOML declarations, Markdown
links, direct test/architecture references, checksum manifests, and optional
verified Knowledge source metadata. Relations are direct only. Parser and
coverage limitations remain explicit; `NO_KNOWN_REFERENCE` means only that all
applicable detectors completed and found no direct reference.

Hard file, byte, relation, symbol, per-file, Git-output, and serialization
budgets fail closed without silent truncation. Only a `COMPLETE` inventory has
a reusable semantic digest. The digest excludes machine paths, timestamps,
process data, caches, and OS separators.

Repository Inventory has no model, shell, mutation, graph, Development, or
Workbench authority. Task-triggered Development outcome/context integration is
future work and is not implemented by this core contract.

Path policy `chappie_repository_inventory_policy/1.0.1` distinguishes exact
repository-root local-state directories from equally named source packages.
For example, `app/runtime` is source, while root `runtime/` is local state.
Exact `.env.example`, `.env.sample`, and `.env.template` names are safe tracked
templates; other `.env` variants remain forbidden. Database, log, credential,
token, and private-key paths fail closed based on path metadata before content
is read.

Policy `1.0.2` permits a database fixture only through an exact, policy-declared
fixture-to-test pair. Both paths must be tracked, the fixture must be directly
under `tests/fixtures`, and the bounded test source must directly name it.
Database and log suffixes everywhere else remain forbidden. Approval never
requires reading database contents and produces a direct `USES_FIXTURE` fact.

Contract and policy `1.1.0` budget canonical semantic edges rather than raw
parser occurrences. Repeated `(source, relation kind, target kind, target)`
evidence is one edge with an occurrence count, up to four stable representative
locators, and a digest binding every locator. Distinct targets and relation
kinds remain distinct. Declarations are represented once in `declared_symbols`
and consume the separate declared-symbol budgets; duplicate `DECLARES` relation
objects are not emitted. The evidence-backed per-file reference bound is 72:
the current repository maximum after canonical grouping is 70.

Relations do not duplicate a source content digest or a separate primary
evidence locator. Consumers obtain the source digest from the exact
`source_path` file record and use `representative_locators[0]` as the primary
locator. `locator_digest` continues to bind all occurrence locators, including
those beyond the bounded representative set.

Symbol coverage is explicitly
`TOP_LEVEL_SOURCE_COMPLETE_TEST_FUNCTIONS_SUMMARIZED`. Production top-level
classes, functions, async functions, and test helpers/fixtures remain individual
navigation facts. Top-level `test_*` functions are represented by a per-file
`TEST_FUNCTION` summary containing the exact count, four stable representative
identifiers, and a digest over every identifier. Methods, nested functions,
constants, and aliases are outside RepositoryInventory coverage; exact coding
symbols remain available through bounded TargetedContext requests.
