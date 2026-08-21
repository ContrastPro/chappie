# Chappie Self-Knowledge SK-1: canonical inventory and gap analysis

**Scope:** read-only audit of the canonical Knowledge store at repository commit
`56602154705e2f422e3c5ef28b8d21bfe8214821`. This report does not add records,
change retrieval, alter `SOURCE_ALLOWLIST`, rebuild Knowledge, or implement
News/Event Intelligence.

## Baseline and observed status

| Check | Observed result |
| --- | --- |
| Branch / working tree | `main` / clean before this documentation change |
| Commit | `56602154705e2f422e3c5ef28b8d21bfe8214821` |
| `origin/main...HEAD` | ahead 18 / behind 0 |
| `chappie status` | Overall `DEGRADED`; Knowledge `READY`; local AI provider `DEGRADED`, model `UNKNOWN` |
| `chappie knowledge status` / `verify` | `READY`, verified |
| `chappie ai status` | `ollama`, `LIGHT`, `qwen3:4b`; provider `DEGRADED`, model `UNKNOWN` |
| Knowledge snapshot | same commit, `clean=True` |
| Knowledge manifest | `e340ea507b169e2ef6cf440609518b3bf1e3fd9074b54cf1030df22f3a63cc92` |

The AI state is a local operational condition, not evidence that canonical
Knowledge or retrieval is unavailable. No provider/model call was made for this
audit.

## Current Knowledge pipeline

```text
clean committed repository
  -> app.knowledge.store.SOURCE_ALLOWLIST
  -> committed_snapshot() + initial_records()
  -> KnowledgeRecordV1 / KnowledgeManifestV1 + write_store()
  -> read_store() + verify_knowledge_store()
  -> app.knowledge.retrieval.retrieve()
  -> KnowledgeContextV1
  -> app.control_plane.service._run_project_qa()
  -> ChappieAI.answer_project_qa() + parse_project_qa()
  -> loopback transport / VS Code Workbench presentation
```

| Stage | Canonical owner and contract | Boundary |
| --- | --- | --- |
| Source policy | `app.knowledge.store.SOURCE_ALLOWLIST` | Explicit repository-relative provenance allowlist |
| Ingestion | `committed_snapshot`, `initial_records`, `_source`, `manifest`, `write_store` | Clean committed snapshot required; AI has no write path |
| Record/store | `KnowledgeRecordV1`, `KnowledgeSourceReferenceV1`, `KnowledgeManifestV1`, `chappie_knowledge/1.0.0` | Deterministic IDs, authority, status, source digest, snapshot identity |
| Verification | `read_store`, `verify_knowledge_store` | Manifest, record identities, related IDs, canonical paths |
| Retrieval | `KnowledgeRetrievalRequestV1`, `retrieve`, `KnowledgeContextV1`, `chappie_knowledge_retrieval/1.0.0` | Bounded lexical selection, provenance-preserving context |
| Project QA | `app.control_plane.service._run_project_qa`, `PROJECT_QA_V1` | Refuses empty context with `PROJECT_QA_NO_RELEVANT_KNOWLEDGE` |
| AI synthesis | `ChappieAI.answer_project_qa`, `parse_project_qa` | Strict JSON; citations may name only supplied fact IDs |
| Interface | `app.control_plane.transport`; VS Code `api`/`workspace`/`ui` | Read-only projection and presentation, not Knowledge authority |

## SOURCE_ALLOWLIST inventory

There are **14** allowlisted paths. “Records” means records emitted by the
current `initial_records()` definitions, not facts merely present in source.

| Canonical source | Family / identity where applicable | Current records / concepts taught | Primary category |
| --- | --- | --- | --- |
| `ARCHITECTURE.md` | active architecture document | `Chappie Architecture`; active MVP boundaries | ARCHITECTURE |
| `VISION.md` | product direction | `Trading Agent` (planned only) | CAPABILITY |
| `app/research_methodology.py` | frozen methodology / `compact_matched_event_research/1.0.0` | `Compact matched-event methodology` | METHODOLOGY / CONTRACT |
| `app/research/intelligence/universe.py` | frozen research source / `market_intelligence_universe_v1/1.0.0` | none | RESEARCH |
| `app/research/intelligence/multi_asset_flow.py` | research evidence and decision / methodology-and-universe-bound study | `Multi-asset flow result`; `Derivatives expansion decision` | RESEARCH |
| `app/ai/research/capabilities.py` | RDE capability contract / `rde_capability_resolver/1.0.0` | none | CAPABILITY / RESEARCH |
| `app/ai/research/evidence_review.py` | evidence authority contract / `research_evidence_critique/1.1.0` | `RDE evidence authority` | AI / CONTRACT |
| `docs/research/README.md` | research documentation | none | RESEARCH |
| `docs/research/CHAPPIE_AI.md` | AI architecture/documentation | provider abstraction; OI limitation; confidence limitation; semantic-retrieval backlog | AI |
| `docs/research/DATASETS.md` | dataset documentation | none | OPERATIONS / RESEARCH |
| `docs/research/NEWS_EVENT_INTELLIGENCE.md` | News/Event taxonomy documentation / `news_event_taxonomy/1.0.0` | none | RESEARCH |
| `docs/control_plane/README.md` | Control Plane documentation / `chappie_control_plane/1.2.0` | `Chappie Control Plane` | ARCHITECTURE |
| `docs/knowledge/README.md` | Knowledge documentation / `chappie_knowledge/1.0.0` | `Project QA`; `Knowledge Store` | ARCHITECTURE |
| `docs/platform/SETUP_CONTRACT.md` | setup contract / `chappie_platform_setup/1.0.0` | `Setup Engine` | OPERATIONS / CONTRACT |

Source references preserve content digests, locators, snapshot commit, and an
optional contract/evidence identity. The current records have no represented
cross-record relationships: every `related_record_ids` tuple is empty.

## Current canonical record inventory

There are **14** records. The record titles and searchable tags are the current
lexical surface available to Project QA.

| Record ID | Title / type | Authority / status | Source locator | Purpose and searchable concepts | Architecture synthesis help |
| --- | --- | --- | --- | --- | --- |
| `knowledge-7d23dbf20cf02c6a462a61a1` | Chappie AI provider abstraction / ARCHITECTURE | DOCUMENTATION / ACTIVE | `docs/research/CHAPPIE_AI.md#Provider abstraction` | backend-independent provider abstraction; `ai`, `architecture` | Partial |
| `knowledge-ad4ec94a6e1841fcf7df3a9b` | RDE evidence authority / AUTHORITY_BOUNDARY | VERSIONED_CONTRACT / FROZEN | `app/ai/research/evidence_review.py#EvidenceRelationV1` | immutable evidence; AI cannot rewrite it; `ai`, `authority`, `rde`, `research` | Strong but narrow |
| `knowledge-bae025e510b13260d9fee2ca` | Compact matched-event methodology / CONTRACT | VERSIONED_CONTRACT / FROZEN | `app/research_methodology.py#METHODOLOGY_NAME` | frozen methodology identity; `research`, `methodology` | Narrow |
| `knowledge-86a4e590af74e59b761e2fd7` | Multi-asset flow result / RESEARCH_RESULT | VERIFIED_RESEARCH_EVIDENCE / HISTORICAL | `app/research/intelligence/multi_asset_flow.py#summary verdict` | historical no-clear-information verdict; `research`, `multi-asset` | No, excluded by default |
| `knowledge-c66e02cf7e3769c35f25a56e` | Derivatives expansion decision / DECISION | PROJECT_DECISION / ACTIVE | `app/research/intelligence/multi_asset_flow.py#remaining derivatives recommendation` | do-not-expand decision; `decision`, `derivatives` | Narrow |
| `knowledge-881a9b6638be0491ea3a7987` | Generic OI predicate limitation / LIMITATION | DOCUMENTATION / ACTIVE | `docs/research/CHAPPIE_AI.md#Current local calibration evidence` | validated-methodology limitation; `limitation`, `predicate` | Narrow |
| `knowledge-f07b124fb7ba588a181f1322` | AI confidence limitation / LIMITATION | DOCUMENTATION / ACTIVE | same locator | confidence is not authority/probability; `ai`, `limitation` | Narrow |
| `knowledge-a082ba15db13dae163b3f1ab` | Semantic retrieval backlog / BACKLOG | PROJECT_DECISION / PLANNED | same locator | semantic retrieval is planned; `knowledge`, `backlog` | No, excluded by default |
| `knowledge-1237466c54200aa6cffa9fae` | Chappie Control Plane / ARCHITECTURE | DOCUMENTATION / ACTIVE | `docs/control_plane/README.md#Chappie Local Control Plane v1` | read-only status/diagnostics/capability/QA boundary; `control`, `plane`, `project`, `qa` | Partial |
| `knowledge-c03fa5591baf4adbca29faa0` | Project QA / MODULE_RESPONSIBILITY | DOCUMENTATION / ACTIVE | `docs/knowledge/README.md#Project QA` | one bounded grounded question; read-only; `bounded`, `grounded`, `knowledge` | Partial |
| `knowledge-ac7255ed9a109a5cc95df4cb` | Knowledge Store / ARCHITECTURE | DOCUMENTATION / ACTIVE | `docs/knowledge/README.md#Chappie Knowledge v1` | deterministic project-fact index; `canonical`, `facts`, `knowledge`, `store` | Partial |
| `knowledge-097e34e17f462a9a2f121c9c` | Trading Agent / ARCHITECTURE | DOCUMENTATION / PLANNED | `VISION.md#Direction` | future bounded role; no direct order authority; `agent`, `future`, `runtime`, `trading` | No, excluded by default |
| `knowledge-f1ec5b554dce6c1606df8452` | Setup Engine / MODULE_RESPONSIBILITY | VERSIONED_CONTRACT / ACTIVE | `docs/platform/SETUP_CONTRACT.md#Lifecycle` | CHECK→READY lifecycle; `apply`, `authorize`, `setup`, `verify` | Narrow |
| `knowledge-59c2b56992c3360bf48e744c` | Chappie Architecture / ARCHITECTURE | DOCUMENTATION / ACTIVE | `ARCHITECTURE.md#Active MVP` | generic active-MVP modules/boundaries; `application`, `architecture`, `boundaries`, `system` | Partial |

No record represents package ownership, allowed/forbidden dependencies, feature
placement, persistence ownership, platform adapter boundaries, VS Code internal
structure, or the final refactor governance documents.

## Architecture-source coverage

| Active document | Direct Knowledge source? | Indirect representation | Important facts absent from Project QA context |
| --- | --- | --- | --- |
| `ARCHITECTURE.md` | Yes | `Chappie Architecture` | detailed current package map and authority relationships |
| `docs/architecture/CLEAN_ARCHITECTURE.md` | No | only broad architecture record | layers, composition roots, package ownership, state/I/O ownership, AI/research boundaries |
| `docs/architecture/DEPENDENCY_RULES.md` | No | none | allowed/forbidden matrix, root policy, platform I/O rules, SCC rule |
| `docs/architecture/FEATURE_IMPLEMENTATION_RULES.md` | No | none | feature decision tree, placement, contracts, tests, coding-agent process |
| `docs/architecture/PACKAGE_BOUNDARIES.md` | No | broad architecture record | stable paths, migration/compatibility policy |
| `docs/architecture/ARCHITECTURE_DECISIONS.md` | No | none | rationale for foundation/runtime/lazy AI/research/VS Code decisions |
| `docs/architecture/REFACTOR_FINAL_REPORT.md` | No | none | completed moves, permanent guards, deferred items, acceptance evidence |
| `docs/control_plane/README.md` | Yes | Control Plane record | detailed non-authority and consumer rules are condensed |
| `docs/knowledge/README.md` | Yes | Knowledge Store and Project QA records | retrieval policy is only partially represented |
| `docs/platform/SETUP_CONTRACT.md` | Yes | Setup Engine record | action-scoped consent, adapter/platform parity and state policy are condensed |

The final governance documents are canonical repository documentation but are
not canonical Knowledge sources today. Their information is not indirectly
represented at usable granularity.

## Package ownership and authority coverage

The machine-readable owner is `PACKAGE_OWNERSHIP` in
`tests/test_architecture_boundaries.py`. It gives each family a responsibility,
allowed dependency families, and a computed forbidden complement. No package
ownership entry is currently a Knowledge record.

| Package family | Layer / responsibility | Owned authority | Explicit non-authority | Knowledge coverage |
| --- | --- | --- | --- | --- |
| `ai` | AI side plane; provider-neutral reasoning | structured reasoning over supplied facts | Risk, execution, trading, portfolio authority | Partial |
| `ai.research` | AI/RDE research workflow | query/evidence reasoning | platform, Control Plane, runtime/domain authority | Partial |
| `ai.evaluation` | offline evaluator | benchmarks/calibration | provider runtime and operational authority | Absent |
| `control_plane` | interface | read-only local projection | runtime/execution/persistence mutation | Partial |
| `coordination` | orchestration | cross-service workflow sequencing | research/AI/platform authority | Absent |
| `exchange_execution` | application service | durable exchange lifecycle/reconciliation | runtime/UI/research/AI authority | Absent |
| `exchange_runtime` | composition | exchange-mode service composition | research/AI/UI authority | Absent |
| `execution` | domain | execution models and provider ports | platform/UI/research/AI authority | Absent |
| `foundation` | neutral foundation | config/logging primitives | every upward package | Absent |
| `infrastructure` | adapter layer | external-system adapters | runtime/UI/research ownership | Absent |
| `knowledge` | side plane | provenance and deterministic retrieval | Control Plane/platform/runtime/domain authority | Partial |
| `market` | domain | market models, validation, events, provider ports | runtime/platform/UI/AI/research | Absent |
| `paper` | application service | paper trading service | platform/UI/research/AI authority | Absent |
| `persistence` | adapter/durability | repository, migration, recovery | UI/setup/research/AI authority | Absent |
| `platform` | host infrastructure | bootstrap/workstation integration | trading/Risk/execution authority | Partial |
| `platform.ollama` / `platform.setup` | platform adapters/orchestration | provider mechanics and setup lifecycle | domain decision authority | Partial |
| `portfolio` | domain | accounting and portfolio state | runtime/platform/UI/AI/research | Absent |
| `research` | offline side plane | reproducible research/data paths | runtime/platform/Control Plane authority | Partial |
| `research.data` | offline data | storage/alignment/outcome/cache identities | runtime authority | Absent |
| `research.intelligence` | offline study | market/flow/event/behavioral studies | runtime authority | Partial |
| `research.strategy` | offline study | history/lab/walk-forward evaluation | runtime authority | Absent |
| `risk` | domain | deterministic limits/decisions | runtime/platform/UI/AI/research | Absent |
| `runtime` | composition | process lifecycle and status | research/AI authority | Absent |
| `shadow` | application service | read-only observation/comparison | execution/platform/UI authority | Absent |
| `strategies` | domain | advisory strategy implementations | execution submission/platform/runtime/AI | Absent |
| `tools` | leaf interface | explicit operator/research commands | production dependency authority | Absent |
| `trading` | domain | intent, snapshot, pipeline, strategy ports | adapter/runtime/UI/AI/research authority | Absent |

## Canonical authority matrix

| Authority area | Owner / source of truth | Allowed consumers | Explicit non-owners | Current Knowledge coverage |
| --- | --- | --- | --- | --- |
| External I/O | `platform` for host/provider; `infrastructure` for external adapters | setup/runtime through ports | domain, research, UI | Absent |
| Market data | `market` contracts/providers | domain, application, offline research read-only | AI/UI as authority | Absent |
| Research evidence | RDE/research and frozen evidence contracts | AI read-only critique, Project QA when supplied | AI, runtime trading | Partial: RDE record |
| Immutable evidence | `EvidenceRelationV1` | AI may reason over it | AI write path | Strong, narrow |
| AI reasoning | `app.ai` over supplied facts | Control Plane/Project QA | Risk/execution/trading authority | Partial |
| Canonical Knowledge | `app.knowledge` | retrieval, Project QA, Control Plane read-only | AI/provider, UI, Control Plane write path | Partial |
| Persistence | `app.persistence` | owning application services | UI/Control Plane/platform as semantic owner | Absent |
| Risk decisions | `app.risk` | runtime/execution consumers | AI, research, Control Plane, UI | Absent |
| Execution authorization | deterministic trading/Risk/execution path | exchange runtime/application services | AI, research, Control Plane, UI | Absent |
| Exchange execution | `app.exchange_execution` | exchange runtime/persistence | AI/research/UI | Absent |
| Runtime orchestration | `app.runtime`, `exchange_runtime`, `coordination` | stable adapters | Control Plane, research, AI | Absent |
| Control Plane exposure | `app.control_plane` | CLI and VS Code | model provider, runtime authority | Partial |
| UI presentation | VS Code `ui`; CLI adapters | users | Python backend as UI owner | Absent |

## Current and future capability semantics

The record schema distinguishes `ACTIVE`, `FROZEN`, `HISTORICAL`, `PLANNED`,
`SUPERSEDED`, and `REJECTED`; retrieval defaults to only `ACTIVE` and `FROZEN`.
This correctly hides the planned Trading Agent and semantic-retrieval backlog
unless requested. The RDE capability resolver separately distinguishes existing
cache execution, unsupported predicates, and methodology extension needs.

However, current Knowledge does not provide a coherent capability inventory of
what exists today versus what the architecture permits later. The record set
does represent some correct boundaries:

- `Trading Agent` is explicitly `PLANNED`, not an active capability.
- Multi-asset research result is `HISTORICAL`, not a current command.
- The methodology and RDE evidence authority are `FROZEN`.
- The Control Plane, Project QA, Knowledge Store, and Setup Engine are `ACTIVE`.

It does not represent current status for package families, News/Event work,
Workbench behavior, persistence/execution capabilities, or a feature-level
implemented-versus-planned inventory.

## Feature-governance coverage

| Topic | Current Knowledge coverage | Evidence |
| --- | --- | --- |
| Choosing package ownership | ABSENT | no ownership record; governance doc not allowlisted |
| Contracts/versioning | PARTIAL | methodology and RDE authority records only |
| Dependency direction | ABSENT | no dependency-rule record |
| Domain/application/infrastructure separation | PARTIAL | generic architecture statement only |
| Adapter placement | ABSENT | no platform/infrastructure record |
| Persistence ownership | ABSENT | no persistence record |
| Authority preservation | PARTIAL | RDE authority, AI limitation, Control Plane and Project QA records |
| Interface/UI placement | PARTIAL | Control Plane record; no VS Code record |
| Testing/architecture guards | ABSENT | no governance/guard record |
| Migration/versioning/provenance | PARTIAL | Knowledge Store and methodology records, but no migration rule |
| Implementation sequencing | PARTIAL | Setup lifecycle only; no feature implementation rule |

## Retrieval and Project QA audit

### Retrieval semantics

`tokenize_topic()` lowercases, extracts `[a-z0-9]+`, drops bounded question
filler (`what`, `understand`, `explain`, `design` is **not** a stop word), sorts
unique tokens, and caps them at 16. `retrieve()` defaults to `ACTIVE` and
`FROZEN`, at most 8 records and 3,000 context characters, with relation depth 1.

Candidates are selected by exact record ID, relationship, requested type/tag/
authority, canonical multiword title containment, or title/tag token overlap.
For a multi-token topic, a candidate without an exact/relationship match needs
an entity title match or at least two distinct title/tag overlaps; otherwise it
is rejected as `WEAK_TOPIC_MATCH`. Rank order is exact ID, entity match,
title-overlap count, tag-overlap count, requested type, authority priority,
status, then stable ID. Relation expansion is implemented but unused by current
records because all relationship tuples are empty.

This is intentionally bounded lexical retrieval, not semantic search. It does
not decompose a multi-concept request into independent subqueries, ensure topic
coverage across selected records, expand package relationships, or include a
record solely because another selected record is architecturally complementary.

### Failed News/Event design fixture

The original deep request is analyzed as a fixture, not a News-specific answer.
Relevant concepts include architecture, package ownership, dependency rules,
external provider/ingestion, research intelligence, AI/RDE, Knowledge,
persistence, Control Plane, runtime, Risk, execution, Workbench, authority,
contracts, feature implementation, and failure model.

Read-only retrieval against the current READY store produced:

| Fixture | Tokens / result | Evidence |
| --- | --- | --- |
| `Understand architecture` | `architecture` → 2 records | Chappie Architecture and AI provider abstraction |
| `Design a future News and Event Intelligence feature` | `and, design, event, feature, future, intelligence, news` → 0 records | 10 `NO_EXACT_MATCH`, 1 `WEAK_TOPIC_MATCH`, 3 status-policy exclusions |
| Full multi-concept design fixture | capped lexical token set → 1 record | only RDE evidence authority selected; 6 `WEAK_TOPIC_MATCH` exclusions |

The short fixture has no lexical overlap with a current title/tag at the required
strength. The longer fixture has enough overlap only for RDE authority. A human
would expect architecture, research, Knowledge, Control Plane, platform,
runtime, persistence, Risk, execution, and interface facts to contribute, but
records for those concepts either do not exist or do not interrelate.

### Project QA synthesis limits

Project QA can combine supplied facts only in a constrained sense: its prompt
requires direct answers from supplied statements, exact fact-ID citations,
preserved authority/status, bounded unknowns for insufficient context, and no
invented claims. The parser rejects unknown citations and requires unknowns if
there are neither citations nor unknowns. It can state uncertainty safely.

It is not currently authorized to derive a novel architecture recommendation
from undocumented general knowledge. Even when several facts are supplied, the
prompt does not define a structured fact-versus-inference recommendation model.
This is correct grounding behavior, but it limits feature-design synthesis until
canonical architecture facts and a bounded composition policy exist.

## Primary classification: BOTH

**Primary: `KNOWLEDGE_CONTENT_GAP`.** The final architecture/ownership/
dependency/governance documents are not allowlisted and have no records. Existing
allowlisted News/Event documentation also emits no record. Most required package
and authority facts therefore cannot be retrieved at all.

**Secondary: `RETRIEVAL_COMPOSITION_GAP`.** Current retrieval is deliberately
lexical and record-local. It cannot decompose a feature-design request, seek
coverage across architecture dimensions, or traverse relationships because no
current records use them. The one-record result for the full fixture proves that
adding content alone may improve coverage but does not establish safe
multi-record design synthesis.

## Target self-Knowledge model and granularity

The existing schema can already represent `ARCHITECTURE`,
`MODULE_RESPONSIBILITY`, `AUTHORITY_BOUNDARY`, `CONTRACT`, `DECISION`, and
`LIMITATION`. SK-2 should assess whether these are sufficient before extending
the schema. Needed semantic families, each grounded in existing governance, are:

- `SYSTEM_IDENTITY` / architecture overview;
- `ARCHITECTURE_LAYER` and `PACKAGE_OWNERSHIP`;
- `DEPENDENCY_RULE` and `AUTHORITY_BOUNDARY`;
- `STATE_OWNERSHIP`, `IO_BOUNDARY`, and `PERSISTENCE_BOUNDARY`;
- `AI_BOUNDARY`, `RESEARCH_BOUNDARY`, and `INTERFACE_BOUNDARY`;
- `FEATURE_IMPLEMENTATION_RULE` and `ARCHITECTURE_DECISION`;
- `CAPABILITY` only where current versus planned state is canonical.

Do not create one giant architecture record or hundreds of sentence fragments.
Use a small coherent graph: one system/layer record, one record per package
family or tightly coupled boundary, one dependency/authority rule where it has
an independent invariant, and explicit relationships between them. This allows
both direct questions such as “What is Control Plane?” and bounded cross-package
questions such as placement of a provider adapter, domain contract, repository,
AI reasoning, and UI.

The schema already has `related_record_ids`, authority, status, tags, and source
locators, so it can express relationships indirectly. It cannot currently
encode typed edges such as `MAY_DEPEND_ON`, `MUST_NOT_DEPEND_ON`, or `OWNS`
directly; all current relations are empty. SK-2 should first test whether
well-scoped statement records plus untyped related IDs are sufficient. A typed
relationship schema extension is not justified by SK-1 alone, but may be needed
if untyped links cannot preserve dependency/authority meaning without prose
duplication.

## Gap matrix

| Area | Current source / record | Retrievable and compositionally useful? | Gap | Recommended future action |
| --- | --- | --- | --- | --- |
| System architecture | `ARCHITECTURE.md` / Chappie Architecture | partial / no | generic only | system and layer records |
| Clean Architecture | governance doc / none | no | no canonical record | source and records in SK-2 |
| Package ownership | ownership map / none | no | absent | package-family records |
| Dependency rules | dependency doc / none | no | absent | rule records plus links |
| Authority boundaries | evidence/CP docs / narrow records | partial / weak | most authorities absent | authority family records |
| AI | AI documentation / provider and limitations | partial / weak | no full AI boundary | AI boundary record |
| RDE/research | methodology/evidence/result | partial / weak | package/research boundary absent | research boundary records |
| Knowledge | knowledge doc / two records | partial / weak | provenance/retrieval policy condensed | provenance and retrieval records |
| Control Plane | Control Plane doc / one record | partial / weak | non-owner constraints condensed | interface authority record |
| Runtime | architecture doc / generic | no | no runtime ownership record | runtime composition record |
| Risk | governance doc / none | no | absent | Risk authority record |
| Execution | governance doc / none | no | absent | execution authority record |
| Persistence | governance doc / none | no | absent | persistence boundary record |
| Platform/infrastructure | setup doc / Setup Engine | partial / weak | adapter/I/O ownership absent | platform and I/O records |
| VS Code interface | extension architecture / none | no | absent | interface/extension records |
| Feature methodology | feature rules / none | no | absent | placement, contract, test rule records |
| Current capabilities | capability source / no emitted record | no | no capability inventory | bounded ACTIVE capability records |
| Future distinction | Vision/backlog/planned Agent | partial / no | sparse and disconnected | explicit current/planned records and links |

## Rejected non-solutions

SK-1 rejects keyword stuffing “News” into the architecture record, a bespoke
News architecture answer, loosening retrieval so unrelated records always
match, and allowing AI to answer from unsupported general knowledge. The target
is reusable architecture intelligence grounded in canonical sources.

## Proposed SK-2: Canonical Architecture Self-Knowledge

**Scope:** add canonical architecture self-Knowledge from the existing governance
documents and machine-readable ownership map; no News/Event feature work.

- Evaluate adding `CLEAN_ARCHITECTURE.md`, `DEPENDENCY_RULES.md`,
  `FEATURE_IMPLEMENTATION_RULES.md`, `ARCHITECTURE_DECISIONS.md`, and the
  final report as allowlisted sources. Retain source-specific locators and
  clean-snapshot provenance.
- Emit coherent families for system/layers, package ownership, dependency rules,
  authority boundaries, state/I/O/persistence boundaries, interface boundaries,
  feature implementation rules, and current/planned capability distinctions.
- Use existing `ARCHITECTURE`, `MODULE_RESPONSIBILITY`, `AUTHORITY_BOUNDARY`,
  `CONTRACT`, `DECISION`, `LIMITATION`, and `BACKLOG` types unless a tested
  semantic ambiguity proves an extension necessary.
- Preserve source authority: governance documents are `DOCUMENTATION`; the
  machine-readable guard or frozen contract may support `SOURCE_CODE` or
  `VERSIONED_CONTRACT` only where its own source establishes that fact. Preserve
  `ACTIVE`, `FROZEN`, `HISTORICAL`, and `PLANNED` meaning.
- Establish relationships between system/layer/package/rule records. Start with
  `related_record_ids`; evaluate typed edges only if necessary.
- Expected range: approximately 30–55 total architecture-oriented records,
  not hundreds. Exact count must follow source-backed concepts rather than a
  quota.
- Tests: deterministic source/record inventory, clean-snapshot manifest,
  canonical locators, authority/status preservation, current-vs-planned
  separation, relationship integrity, and direct retrieval of package/authority
  questions.
- Frozen contracts that must not change without explicit semantic migration:
  `chappie_knowledge/1.0.0`, retrieval identity/digest, Project QA contract,
  RDE methodology, Control Plane/transport, setup engine, and platform policies.

## Proposed SK-3: Compositional Architecture Retrieval

Only proceed after SK-2 proves that relevant architecture records exist but a
bounded feature-design query still cannot select a useful set.

- Define deterministic query decomposition into existing canonical dimensions
  (package, authority, dependency, interface, persistence, current/planned), not
  semantic similarity or provider-generated query expansion.
- Select a bounded, provenance-preserving coverage set across those dimensions;
  preserve authority/status filters, character/record budgets, stable tie-breaks,
  no-match behavior, and rejection of weak unrelated collisions.
- Make relationship traversal explicit, bounded, and diagnosable. Do not infer
  unstated edges or include planned/historical records by default.
- Define a fact-versus-inference response boundary for Project QA only after a
  new contract review; unsupported recommendations must remain unknown.
- Tests: exact deterministic selection/reasons, negative unrelated queries,
  current-vs-planned exclusion, conflict handling, bounded context, and the
  feature-design acceptance set below.

## Future acceptance questions

1. What is Chappie Control Plane?
2. What owns canonical Knowledge provenance?
3. What owns immutable verified research evidence?
4. Is AI allowed to approve a Risk decision?
5. Can research activate runtime trading behavior?
6. Which package owns deterministic portfolio accounting?
7. Where does a new external exchange adapter belong?
8. Where does a new provider installation mechanism belong?
9. Where does a new read-only local endpoint belong?
10. Where does VS Code presentation code belong?
11. May Knowledge import Control Plane?
12. May production code import `app.tools`?
13. What must happen when a frozen contract’s semantics change?
14. What must happen to Knowledge after an allowlisted source move?
15. Is Trading Agent implemented today or planned?
16. What is the difference between an active capability and a planned role?
17. Where should a feature’s domain contract, persistence repository, AI
    reasoning boundary, external provider adapter, and UI action be placed?
18. **Stress test:** Design a future News & Event Intelligence feature using
    Chappie’s architecture, ownership, dependency, authority, persistence,
    provider, research, Knowledge, Control Plane, runtime, Risk, execution, and
    interface rules; label every conclusion as fact, bounded inference, or
    unknown.

## SK-1 conclusion

The honest `PROJECT_QA_NO_RELEVANT_KNOWLEDGE` result protects grounding and must
remain fail-closed. The next step is not News-specific implementation or weaker
matching. It is SK-2, a clean-snapshot, source-backed self-Knowledge expansion;
SK-3 is conditional on evidence after that content exists.
