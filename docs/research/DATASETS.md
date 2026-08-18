# Research datasets

All local research data is ignored by Git and is separate from Paper, Testnet,
and LIVE runtime persistence. The canonical layout is rooted at
`data/research/`:

- `source/` contains reusable immutable market inputs, organized by market
  type and asset. Mark/index archives live under `source/basis/archives/`;
  USD-M metrics, funding, verified manifests, and flow rows live under
  `source/futures/<SYMBOL>/`. A futures source may record explicit source
  gaps or unavailable fields: consumers must preserve those as unavailable,
  never forward-fill them.
- `compact/research.sqlite3` is the verified, research-only compact store.
- `cache/downloads/`, `cache/conversion/`, and `cache/temporary/` separate
  producer caches from durable source and study data.
- `studies/` contains derived datasets and manifests owned by a named study.
- `results/` contains generated reports and retained research conclusions.
- `legacy/` retains frozen historical windows and superseded small stores only
  when they remain useful for reproduction.

Research tools write canonical paths. During the transition, readers select a
canonical artifact first and may read a declared legacy artifact only when the
canonical target is absent. A migrated BTC/ETH compact, flow, or V4C artifact
must not require that fallback.

Source history is stored once and reused by studies where practical. Generated
smoke/probe data is disposable after it is unreferenced and reproducible.
Archive a useful database and its reports before resetting research data; never
repair research or trading state manually. This policy does not apply to future
LIVE durability.
