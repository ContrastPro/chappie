# Universal Implementation Context Core

`app.implementation_context` owns the immutable consumer-neutral contract
`chappie_implementation_context/1.0.0` and its deterministic sufficiency
assessment. It is a new narrow package because neither `app.foundation`
(configuration/logging) nor `app.development_analysis` (read-only Development
Intelligence orchestration) owns this cross-consumer contract.

The core has no adapter, retrieval, model, persistence, graph, filesystem,
shell, Git, trading, or execution behavior. Future mappings are additive only:
`DevelopmentContextMapV1` supplies bounded evidence locators,
`DevelopmentAnalysisV2` supplies validated CURRENT/PROPOSED/UNKNOWN analysis,
`CodexHandoffV1` becomes a consumer presentation adapter, and planned
`DevelopmentDiffReviewV1` may supply specialized review outputs as context.
None are migrated by this slice. DiffReview comparison evidence,
changed-file checks, test-run evidence, and review status remain specialized.

`CURRENT` items require a source locator, source-commit binding, and evidence;
`PROPOSED` items cannot claim current source evidence; `UNKNOWN` items carry an
exact bounded missing-context category and reason. Unordered semantic fields
are sorted and deduplicated before the stable payload is hashed.
