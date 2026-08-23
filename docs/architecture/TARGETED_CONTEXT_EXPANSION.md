# Bounded Targeted Context Expansion

`app.targeted_context` is the read-only adapter for an
`ImplementationContextV1` which reports `TARGETED_EXPANSION_REQUIRED`.
The neutral `app.implementation_context` package stays DTO-only and has no
filesystem authority.

The cardinal rule is: **Targeted expansion resolves only explicitly requested
context and never performs broad repository discovery.** It accepts exact
`SYMBOL`, `CONTRACT`, `FILE`, `RELATION`, and `TEST` requests, an already
observed clean repository snapshot, and a repository root. It validates the
source commit and context digest before opening only the exact requested file.
It does not recurse directories, expand globs, search imports/callers, invoke
Git or a shell, use an LLM, mutate Knowledge, or write files.

`TargetedContextExpansionV1` binds its source context digest and commit,
records resolved/unresolved requests and bounded symbols/contracts/tests/direct
relations/files, then `apply_targeted_expansion()` creates a new immutable
context. Merge rejects provenance mismatch and contradictory evidence.
Sufficiency is re-evaluated using the existing deterministic function; no
status is hard-coded. Planned artifacts remain `PROPOSED` even if exact current
supporting files are found.
