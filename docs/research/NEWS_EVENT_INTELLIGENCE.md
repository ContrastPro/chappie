# News & Event Intelligence v1

News/Event Intelligence is an offline research foundation. It is not a source
ingestor, Market Intelligence Agent, strategy, or trading capability.

## Canonical chain

```text
source fact → deterministic normalization/provenance → optional AI enrichment
→ RDE evidence → future research reasoning
```

The canonical taxonomy is `news_event_taxonomy/1.0.0`. It is descriptive and
contains no trade direction, signal, or execution instruction.

## Provenance and time

`SourceReferenceV1` holds immutable source identity, source timestamps, title,
minimal excerpt, raw representation digest, retrieval metadata, and source
quality. Copyrighted full article text is not persisted by default.

`NewsEventV1` preserves source references and factual event identity separately
from classifier interpretation. Historical download time is never represented as
historical first-seen time. `UPDATED_AT` never replaces `PUBLISHED_AT`.

Only a precisely evidenced public publication can create `MARKET_KNOWN_AT`.
Date-only records, an unevidenced source time, and a release schedule alone are
not valid intraday anchors. The RDE anchor is the first completed canonical
five-minute interval strictly after market knowledge.

## Deduplication

References first form one source record. A deterministic dedup group prefers
official release, docket, incident, announcement, or governance identifiers.
Fallback clustering is conservative: same family, overlapping entities, bounded
time window, and identical normalized title tokens. Ambiguous items remain
separate.

## Optional AI boundary

`AIEnrichmentV1` may contain a summary, candidate interpretation, relationships,
ambiguity notes, and scenarios. It cannot alter source references, hashes,
timestamps, factual entities, event identity, or market-known basis. No AI call
is part of v1 Phase 1.

`MarketHypothesisV1` is a research record only. AI-originated hypotheses begin
`UNVALIDATED`; only a later deterministic validation process may assign another
status.

## RDE boundary

The sidecar database is `data/research/compact/news_events_v1.sqlite3`; it is
never part of Paper or Testnet persistence. A future bridge will record the
event’s strict-after RDE anchor, state digest, outcome-cache identity, and base
methodology identity. It must not claim canonical matched-event validation until
its external-event alignment and controls have been validated separately.

Only causal state fields at or before that anchor may reach future reasoning:
multi-asset breadth/rotation, returns, volatility, BTC/ETH/SOL/BNB derivatives,
funding, OI, taker pressure, and positioning. Existing forward outcomes are
reserved for subsequent deterministic validation, never AI input.

## Future ingestion scope

The intended first source subset is official FOMC, CPI, Employment, targeted
SEC/CFTC/Federal Register regulatory events, official ETF decisions, and only
exchange incident histories with reproducible timestamps. Social sources remain
a later input class feeding the same normalized model; they are not a separate
event counter.

## Official macro corpus (Phase 2)

The first factual corpus is restricted to the Federal Reserve FOMC statement
archive and BLS CPI and Employment Situation releases. `news_event_official_macro_sources/1.0.0`
is the versioned primary-source registry. The downloader caches only official
representations under `data/research/source/news/macro/` and
`data/research/cache/downloads/news/macro/`; these generated research files are
ignored.

The normalizer preserves official URL, title, minimal excerpt, raw digest,
published/update evidence, and parser version. It classifies only factual macro
subtypes. A precise `MARKET_KNOWN_AT` is created only when the preserved source
itself supplies an explicit publication/release timestamp; date-only source
records remain preserved but are excluded from intraday RDE alignment.

For eligible records, the bridge uses the first fully closed 5-minute interval
strictly after market knowledge. The AI-ready export is facts plus the causal
snapshot at that anchor only. Forward outcomes are structurally separate and
cannot affect its context digest.

## Phase 2 implementation status

IMPLEMENTED:
- Official macro source registry and versioned provenance contract.
- Official FOMC/CPI/Employment parser contracts (offline normalization).
- Intraday anchoring, causal bridge emission, and separate forward outcomes.
- `official_macro_*` paths and deterministic offline test coverage.

PENDING:
- Full historical official corpus backfill across 2023-01-01 → 2026-08-01.
- Manifested production-scale official download/replay reports.

BLOCKER:
- Real BLS official page access is blocked by this environment (HTTP 403 /
  TLS trust path), so this phase must not claim historical corpus completeness
  from this run.
