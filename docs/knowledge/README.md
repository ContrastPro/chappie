# Chappie Knowledge v1

Chappie Knowledge is a deterministic, rebuildable index of concise project facts,
contracts, decisions, research conclusions, limitations, and backlog. It is not
LLM memory, a vector database, a repository dump, model training data, or trading
authority.

`chappie_knowledge/1.0.0` records have deterministic identities, bounded type,
status, and authority vocabularies, plus allowlisted repository-relative source
references and a committed repository snapshot. Canonical ingestion refuses a dirty
working tree. The development store is separate and ignored at
`data/knowledge/chappie_knowledge_v1.json`; it can be rebuilt from source.

Records never copy source files wholesale. Research conclusions preserve their
verdict/evidence provenance, decisions remain distinct from research results, and
supersession preserves historical records. Conflicts are surfaced as diagnostics,
not resolved by an LLM. Chappie AI has no write authority; a future candidate is
not a canonical record.

Phase 1 supports exact ID/type/status/tag/authority filtering only:

```bash
python -m app.tools.chappie_knowledge --list --type CONTRACT
python -m app.tools.chappie_knowledge --verify
```

Embeddings, vector search, semantic retrieval, prompt injection, and AI-generated
canonical knowledge are deliberately deferred. Chappie Knowledge v1 Phase 2 may
build bounded retrieval context from these records.

## Deterministic retrieval

`chappie_knowledge_retrieval/1.0.0` adds bounded exact retrieval without semantic
similarity. Default selection includes only `ACTIVE` and `FROZEN` records; historical,
planned, superseded, and rejected records require explicit inclusion. Ranking is exact
ID, type, authority priority, tag/token overlap, status, then stable knowledge ID.
Context is character-budgeted, preserves selected record provenance and conflicts, and
has a digest bound to the retrieval contract and knowledge manifest. Task profiles are
deterministic; no knowledge is automatically injected into an AI prompt yet.

## Project QA

`PROJECT_QA_V1` receives only selected `KnowledgeContextV1` facts such as
`knowledge.<record-id>.statement`, authority, and status. Its strict output cites only
those facts and surfaces supplied conflicts or unknowns. Knowledge remains Chappie-owned;
the provider cannot write it. The local opt-in command is `python -m
app.tools.chappie_project_qa --provider ollama --model qwen3:8b --question QUESTION`.
