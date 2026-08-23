# Development to Implementation Context Projection

`app.development_analysis.implementation_context` is a deterministic,
producer-side adapter from already validated Development artifacts to the
consumer-neutral `ImplementationContextV1`. It has no inference, retrieval,
filesystem, Git, shell, model, or mutation behavior.

Development artifacts remain the source of their own semantics. The projection
preserves bounded provenance, CURRENT facts, proposed scope, unknowns, owner,
contracts, direct files/symbols/tests where already explicit, relations,
authority boundaries, risks, and acceptance criteria. Handoff task IDs and
copy/paste presentation are intentionally consumer-specific and excluded.

Future consumer adapters may consume `ImplementationContextV1`. They are not
implemented here. Planned DiffReview remains specialized: source/target diff
comparison, changed-file outcomes, test-run evidence, contract drift, and
review state stay outside the universal context. `MissingContextRequestV1`
describes exact missing detail only; it performs no retrieval.
