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
