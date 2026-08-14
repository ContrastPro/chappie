# Persistence, Audit and Recovery Design

## 1. Purpose

Статус: **M3.14 runtime integration completed; M3.15 safety review passed**.

Дата design review: 2026-08-08. Дата M3.13 final safety review: 2026-08-09.

Документ определяет production-grade persistence boundary для синхронного paper
runtime Chappie Trader. Его цель — доказуемо восстановить финансовое состояние после
остановки процесса, не повторить уже committed Fill и не продолжать торговлю, если
целостность нельзя доказать.

Baseline:

```text
append-only trading journal (authoritative audit trail)
    + periodic verified Portfolio checkpoints (recovery optimization)
    + one application-level durable transaction per paper cycle mutation
```

Первый adapter — SQLite. Application/domain contracts не зависят от SQLite и должны
допускать будущий PostgreSQL adapter.

### M4.6 exchange lifecycle extension

Stage 3 paper journal остаётся schema v1 и не изменяется. M4.6 добавляет ordered,
transactional schema v2 для отдельной Binance Spot Testnet lineage. Exchange order events и
trades являются append-only authorities; `exchange_orders` — проверяемая current projection.
PREPARED означает только durable intent. Attempt row, SUBMITTING transition и session counters
commit-ятся атомарно до будущего gateway call. После этого commit restart не может доказать,
был ли transport, поэтому SUBMITTING и любой последующий non-terminal order исключаются из
automatic submission и передаются будущему M4.7 reconciliation.

Local SQLite integrity и external exchange reconciliation разделены. M4.6 local recovery
проверяет root/order digests, contiguous event versions, projection status, session lineage и
durable external IDs без Binance query. Missing/conflicting authority fail closed и не
восстанавливается молча из current projection.

M4.6 final crash/restart review подтвердил all-or-nothing PREPARED, attempt, ACK, ambiguity и
rejection transactions через deterministic failpoints. Исключение после успешного COMMIT
считается commit-ack ambiguity: решение принимается только по fresh SQLite state, не по in-memory
exception. Exact preparation/ACK/trade replay не создаёт новый event/version/counter. SQL
запрещает attempt ordinal > 1, event/trade UPDATE/DELETE и scoped client/trade collisions.
Filled order с неполным local trade set остаётся terminal exchange fact, но получает explicit
INCOMPLETE trade completeness и не считается settled.

### M4.7 external exchange reconciliation extension

Additive migration `003_exchange_reconciliation.sql` не изменяет migration 002.
`exchange_reconciliation_current` хранит digested REQUIRED/SAFE/FAILED/CONFLICT state;
`exchange_reconciliation_runs` — append-only cycle authority. SAFE evidence, current state,
control mirror и generation `N -> N+1` commit-ятся атомарно. Partial truthful order/trade facts
могут сохраниться раньше: crash оставляет generation прежней, а следующий cycle идемпотентно
проверяет durable truth. Trade settlement остаётся PENDING; Portfolio не изменяется.

Every successful invocation is a distinct cycle: generation advances exactly `N -> N+1` once
with its append-only run. Exact replay of the same run identity and canonical payload does not
advance again; conflicting reuse fails. Any number of REQUIRED/FAILED/CONFLICT runs remain at N.
Run/current/control-mirror writes share one transaction and deterministic failpoints cover every
write plus pre/post-COMMIT ambiguity. Recovery verifies current digest/version, latest-run digest,
root/session/account linkage, generation/outcome/time agreement and the control mirror.

Exchange trade-history coverage is explicit. SAFE requires a complete interval beginning no later
than root creation and ending no earlier than cycle start. A bounded/full page or missing horizon
is REQUIRED, never silently accepted. This generation proves exchange-truth scope only; settlement
and submission authority remain separate M4.8 gates.
M4.7 final reconciliation safety review пройден 2026-08-11.

## 2. Existing crash gap

M3.11 выполняет следующий flow только в памяти:

```text
CandleClosed
-> TradingDecisionCoordinator
-> DecisionPipelineResult
-> PaperTradingEngine
-> Portfolio.mark_to_market()
-> RiskDecision
-> PaperExecutionProvider
-> Fill
-> Portfolio.apply_fill()
-> observer
-> PaperTradingEngine dedup/result
-> coordinator dedup/result
```

Portfolio изменяется до observer и до обоих dedup registries. Portfolio, Fill registry,
paper dedup и coordinator dedup bounded и process-local. `PaperExecutionProvider`
stateless: повторное выполнение одного решения создаст новый execution/fill UUID.

### 2.1 Current crash matrix

| Точка | Уже изменено | Потеря при restart | Повтор / риск |
|---|---|---|---|
| A. До RiskDecision | Возможен committed только в памяти mark-to-market | valuation, cycle/decision state | Повтор стратегии может дать те же числа, но новые UUID; финансового Fill ещё нет |
| B. После RiskDecision, до execution | Mark и RiskDecision только в памяти | risk audit и valuation | Risk можно пересчитать при идентичных inputs, но UUID изменятся |
| C. После `FILLED`, до `apply_fill()` | Provider создал Fill object, Portfolio ещё прежний | Fill полностью теряется | Blind retry создаст Fill B; состояние выполнения неоднозначно без durable command |
| D. Во время `apply_fill()` | Aggregate использует staged mutation, но process может умереть | Нельзя доказать, применён Fill или нет | В текущей памяти crash уничтожает оба состояния; внешний audit отсутствует |
| E. После Portfolio commit, до paper result | Balances/position изменены только в памяти | Вся финансовая mutation | Retry после restart создаст и применит второй Fill |
| F. После paper result, до coordinator dedup | Portfolio и paper dedup изменены только в памяти | Оба исчезнут | Повтор event заново запускает pipeline и paper path |
| G. После shadow, до paper | Shadow side effect мог произойти | shadow delivery audit | Paper ещё можно выполнить; shadow может повториться |
| H. После paper, до coordinator result | Paper mutation завершена в памяти | Portfolio и coordinator terminal result | Повторный Fill после restart |
| I. После final result, до shutdown | Всё подтверждено только памятью/logs | Portfolio, Fill registry и dedup | Restart не знает, что cycle завершён |

Текущий `DEGRADED` корректно останавливает дальнейший paper processing внутри процесса,
но сам статус не переживает restart. Runtime logs не являются authoritative data.

## 3. Goals and non-goals

### Goals

- восстановить Portfolio и его valuation/drawdown history;
- восстановить все committed Fill и durable idempotency facts;
- находить незавершённые cycles и безопасно классифицировать их;
- продолжать новые decisions только после успешной reconciliation;
- сохранить объяснимую цепочку decision → risk → execution → fill → transition;
- fail closed в состоянии `RECOVERY_REQUIRED` при corruption/ambiguity;
- сохранить один synchronous writer и одинаковые business calculations.

### Non-goals

- live/testnet/private Binance execution;
- distributed transactions, broker, asyncio или background threads;
- durable storage полного Market Cache или 1000-candle snapshot в каждой записи;
- обещание exactly-once delivery наблюдателям;
- автоматическое исправление финансовых расхождений;
- реализация schema, migrations или adapter в рамках M3.12 design.

После реализации гарантируется durable at-most-one committed paper mutation на
stable decision/execution key при одном authoritative store. Термин «durable
exactly-once» не используется: crash до commit допускает retry, observer delivery
best-effort, а multi-store/live execution не входят в transaction.

## 4. Persistence architecture

Выбрана модель **append-only journal + periodic verified checkpoint**.

- Snapshot-only не объясняет историю, плохо обнаруживает partial write и не даёт
  durable Fill idempotency.
- Чистый journal является достаточным source of truth, но полный replay растёт без
  ограничений и удлиняет startup.
- Journal + checkpoint сохраняет auditability и ограничивает replay. Checkpoint
  никогда не конкурирует с journal: он действителен только при совпадающем journal
  sequence/hash и может быть отброшен с replay от genesis.

```text
TradingDecisionCoordinator
  -> DurablePaperTradingService / PaperCycleUnitOfWork
       -> deterministic RiskManager
       -> deterministic PaperExecutionProvider
       -> Portfolio transition calculation
       -> TradingPersistenceSession (transaction)
            -> TradingJournal
            -> PortfolioCheckpointStore
  -> observer after COMMIT
```

Persistence orchestration находится в application layer. Portfolio, RiskManager,
PaperExecutionProvider и TradingDecisionPipeline не выполняют SQL.

## 5. Ports

Минимальные будущие contracts (точные Python signatures утверждаются implementation
review):

```text
TradingPersistence
  open() -> StorageMetadata
  begin_cycle(stable_decision_key, durable_command_key) -> PaperCycleTransaction
  load_recovery_view(portfolio_id) -> RecoveryView
  acquire_writer(instance_id) -> WriterLease
  release_writer(lease) -> None
  close() -> None

PaperCycleTransaction
  claim_decision(decision_audit) -> ClaimResult(NEW | COMMITTED | IN_PROGRESS)
  append_risk(risk_audit) -> None
  append_execution(execution_audit) -> None
  append_fill(fill_record) -> None
  append_valuation(valuation_record) -> None
  write_portfolio_state(transition, checkpoint_policy) -> None
  mark_cycle_terminal(terminal_record) -> None
  commit() -> DurableCycleResult
  rollback() -> None

RecoveryCoordinator
  recover(storage, portfolio_config) -> RecoveryResult
```

Отдельные public `TradingJournal` и `PortfolioCheckpointStore` допустимы внутри
adapter, но application должна получать один `TradingPersistence`/transaction port:
иначе невозможно выразить общий atomic commit. `RecoveryCoordinator` читает только
recovery view и создаёт новый восстановленный Portfolio; он не торгует.

In-memory adapter реализует те же семантические contracts для unit tests, но явно
маркируется `durable=False` и не используется как доказательство restart durability.

Нормативный application API:

```text
with persistence.begin_cycle(stable_decision, optional_command) as tx:
    existing = tx.lookup_terminal_decision()
    tx.record_decision(...)
    tx.record_command/risk/execution/fill(...)
    tx.record_portfolio_transition(...)
    tx.set_current_portfolio(...)
    durable_result = tx.commit_terminal(...)

RecoveryCoordinator(persistence, portfolio_restorer).recover(
    expected_portfolio_id,
    expected_config_and_rules_fingerprints,
) -> RecoveryResult

RecoveryResult:
    status: CLEAN | RECOVERED | RECOVERY_REQUIRED
    restored_portfolio
    last_journal_seq
    checkpoint_version
    replayed_transition_count
    unresolved_cycles
    safe_diagnostics
```

Context manager автоматически rollback-ит uncommitted transaction. API выражает
business records/transition, а не raw SQL/table operations. `PortfolioRestorer`
вызывает будущий typed `Portfolio.restore(...)`; private aggregate fields не меняются.
Recovery не читает сеть/MarketProvider.

## 6. Storage model

Концептуальные relation groups:

- `schema_migrations` — применённые schema versions/checksums;
- `writer_lease` — advisory writer identity; safety дополнительно обеспечивают SQLite
  locking, per-cycle owner/version validation и UNIQUE constraints;
- `paper_cycles` — stable key, command key, lifecycle и terminal outcome;
- `decision_audit`, `signal_audit`, `intent_audit`, `risk_audit`;
- `execution_commands`, `execution_results`, `fills`;
- `portfolio_transitions` — authoritative ordered mutation/valuation journal;
- `portfolio_checkpoints` — verified materialized state;
- `observation_deliveries` — диагностический статус после business commit.

Journal sequence (`journal_seq`) — monotonically increasing database identity и
порядок authoritative transitions. Business ordering определяется также domain
timestamps; `persisted_at_ms` — только diagnostics.

Нельзя сохранять secrets, API keys, Python repr, arbitrary unversioned metadata,
traceback или полный candle history. Tracebacks остаются в logs.

## 7. Durable identities

Runtime UUID сохраняются для correlation/audit, но не являются restart idempotency:

| Identity | Назначение / constraint |
|---|---|
| `source_event_id`, `correlation_cycle_id`, `coordination_id`, `snapshot_id`, `pipeline_run_id`, `signal_id`, `risk_decision_id`, `execution_id`, `fill_id`, `paper_cycle_id` | UNIQUE там, где persisted как entity; correlation, не stable replay key |
| `intent_id` | UNIQUE audit identity; недостаточен как durable key, потому что при replay pipeline создаст новый UUID |
| `stable_decision_key` | UNIQUE boundary: стратегия + конфигурация уже обработала конкретную закрытую свечу |
| `decision_slot_key` | UNIQUE semantic slot без market payload digest; запрещает automatic second decision для той же candle/strategy/config при corrected history |
| `durable_command_key` | UNIQUE execution idempotency boundary; derives from stable decision key + approved Risk/instrument/execution semantics |
| `fill_id` | UNIQUE Portfolio mutation identity и conflict check |
| `(portfolio_id, portfolio_version)` | UNIQUE ordered transition boundary |
| `recovery_run_id` | Новый UUID только для audit одного startup recovery; не idempotency key |

Нормативное решение: `intent_id` не является durable command key. Для intent cycle:

```text
durable_command_key = SHA-256("paper-command/v1\n" + stable_decision_key +
                             canonical approved command semantics)
```

HOLD/planner-declined получают stable decision key и durable valuation transition,
но не execution command.

### 7.1 Stable decision identity

Canonical payload version `stable-decision/v1`:

```text
exchange = "binance-spot"
symbol
interval
candle_open_time_ms
candle_close_time_ms
latest_candle_fingerprint
strategy_input_fingerprint
strategy_name
strategy_version
strategy_config_fingerprint
planner_name
planner_version
planner_config_fingerprint
snapshot_policy_fingerprint
decision_schema_version
```

`latest_candle_fingerprint` включает canonical OHLCV Decimal strings и timestamps.
Этого одного недостаточно: текущая EMA вычисляет значения по **всей фактически
переданной `MarketSnapshot.closed_candles` sequence**, а не только по последней свече.
Поэтому `strategy_input_fingerprint` покрывает ordered tuple всех CandleSnapshot,
которые реально доступны Strategy: для каждой свечи open/close timestamps и OHLCV,
плюс data version и sequence length. Volatile delivery facts `snapshot.as_of_ms`,
`source_event_id`, correlation UUID и вычисленное `is_stale` не входят в restart key:
поздняя повторная доставка той же candle не должна создавать новый trade. Они
сохраняются в audit первого committed evaluation, а stale threshold/policy входит в
`snapshot_policy_fingerprint`. Если
будущая Strategy объявит более узкий exact input window и pipeline действительно
передаст только его, fingerprint может покрывать этот declared window; hashing
неиспользованных 1000 свечей не требуется. Fingerprint всегда соответствует
фактическому input, а не предполагаемому `required_history`.

Stage 3 не сохраняет полный OHLCV window в каждой decision row. Он сохраняет digest,
latest candle identity и input length. Digest доказывает совпадение с предъявленным
dataset, но сам не восстанавливает отсутствующие candles; полная независимая
реконструкция EMA потребует будущего durable OHLCV store. Recovery уже committed
decision не пересчитывает и не требует network read.

Дополнительно вычисляется `decision_slot_key` из exchange/stream, latest candle
open/close timestamps, strategy/planner identities/config fingerprints и decision
schema version, но **без** market-data fingerprint. Storage имеет UNIQUE slot. Тот же
slot + identical input digest возвращает existing terminal decision. Тот же slot с
исправленной earlier history/different input digest даёт typed data conflict и
`RECOVERY_REQUIRED`, а не автоматически торгует candle второй раз. Deliberate corrected
reprocessing требует новой audited Portfolio lineage.

Canonical encoding — UTF-8 JSON с sorted keys, no insignificant whitespace, explicit
`null`, JSON booleans, base-10 integers, canonical Decimal strings, stable enum values
и ordered arrays. Object repr и Python `hash()` запрещены. Каждый digest хранится как
lowercase SHA-256 hex вместе с algorithm/payload version (`decision-key-v1`,
`strategy-input-v1`, `config-v1`).

Strategy configuration fingerprint обязателен и включает EMA fast/slow windows и
любую decision-affecting Strategy option. Snapshot/staleness policy fingerprint
включает stale threshold/data version, потому что `is_stale` меняет Signal. Planner
fingerprint включает target exposure и все planning options. Risk не входит в
`stable_decision_key`: key заканчивается на Strategy/Planner decision boundary.

EMA 12/26 и EMA 20/50 имеют разные keys. Изменение code semantics требует нового
`strategy_version`; изменение config автоматически меняет fingerprint. Deliberate
reprocessing возможно только с новой declared lineage и новым portfolio ID. Обычный
runtime после config change обрабатывает только новые closed candles; он не торгует
исторические свечи автоматически. Backtest/replay остаётся отдельным mode.

### 7.2 Durable command identity

`durable_command_key` payload version `paper-command/v1` включает:

- `stable_decision_key`;
- OrderIntent action and requested target exposure;
- approved side, quantity, target exposure, reference/worst-case price and notional;
- Risk outcome (`APPROVED`/`MODIFIED`) and modification reasons;
- risk policy name/version and canonical full policy configuration fingerprint;
- instrument rules version and canonical rules fingerprint;
- paper execution provider name/version and fee/latency configuration fingerprint;
- command schema version.

Runtime `risk_decision_id`, `intent_id` и execution UUID сохраняются как audit
correlation, но не входят как restart identity. Два различающихся approved Risk
results или rules/fee configurations не могут получить один command key. Если один
`stable_decision_key` уже terminal committed, попытка предъявить другое command
payload является integrity conflict, а не вторым разрешённым trade.

HOLD, `PLANNER_DECLINED`, `RISK_REJECTED` и `NOT_EXECUTED` durable-recorded как
terminal decision outcomes с valuation transition, но Fill отсутствует. Для
`RISK_REJECTED` command row не создаётся; для `NOT_EXECUTED` хранится command/audit
authorization без Fill. Decision idempotency отделена от financial idempotency:
durable decision lookup может пропустить Strategy replay, а command/fill constraints
независимо запрещают повторную финансовую mutation.

## 8. Transaction boundary and write ordering

Один SQLite transaction охватывает весь authoritative paper cycle, включая valuation
для HOLD и trading mutation для intent:

```text
BEGIN IMMEDIATE
1. INSERT/claim stable_decision_key and immutable decision payload
2. persist signal/intent audit and valuation input
3. persist RiskDecision audit (если есть)
4. INSERT/claim durable_command_key for executable authorization
5. call deterministic PaperExecutionProvider and persist execution result/Fill
6. calculate and validate Portfolio transition on staged immutable state
7. append immutable journal entries and portfolio transition(s)
8. atomically upsert materialized portfolio_current with exact next version/digest
9. optionally write checkpoint referencing terminal journal_seq
10. append terminal cycle event and set materialized cycle index/outcome
COMMIT
11. synchronize/publish committed in-memory Portfolio state
12. call observer; persist diagnostic delivery status separately if configured
```

Application-level Unit of Work владеет ordering. `PaperExecutionProvider` остаётся
deterministic calculator и не знает о database. Implementation должна избегать
изменения authoritative in-memory aggregate до успешного DB commit: использовать
staged Portfolio clone/transition plan либо восстановимый pure transition. Если
commit успешен, а swap in-memory state неожиданно не выполнен, runtime немедленно
переходит в `RECOVERY_REQUIRED` и перечитывает committed state; повтор execution
запрещён.

Rollback означает отсутствие всех rows transaction. Retry снова claim-ит тот же key.
UNIQUE conflict читается как idempotency result: identical committed facts → return
stored result; различающиеся facts → corruption/conflict, fail closed.

Claim и financial mutation находятся в **одной** transaction. Отдельно committed
`IN_PROGRESS` claim запрещён: он превратил бы crash в зависший command. SQLite crash
после физической записи Fill row, но до COMMIT делает transaction невидимой/rolled
back; manual compensating rollback не нужен.

`PaperExecutionProvider.execute()` вызывается после command claim, пока Unit of Work
владеет transaction, но до authoritative commit. Если provider вернул Fill A в память
и process умер, никакой committed execution ещё нет. После restart operation может
вычислить Fill B с новым UUID; это допустимо, потому что A никогда не был durable.
Economics B обязана совпасть с canonical command/config. Только один eventually
committed Fill допускается constraint-ом `fills.durable_command_key UNIQUE`.

Crash после COMMIT и до observer/in-memory coordinator result возвращает stored
terminal outcome при повторном stable key. Risk, provider, Portfolio transition и
observer автоматически не повторяются. Missing observer delivery — отдельная
diagnostic concern.

### 8.1 Application integration and Portfolio staging

Не создаётся второй параллельный business engine. `PaperTradingEngine` сохраняет
существующее orchestration ordering и в M3.14 получает injected `PaperCycleUnitOfWork`
за `TradingPersistence` port. SQLite остаётся внутри adapter. Для раннего durable
lookup application также может inject-ить `DurableDecisionEvaluator` decorator на
существующем pipeline boundary: coordinator по-прежнему вызывает один `evaluate()`,
но decorator по stable key либо reconstruct-ит committed `DecisionPipelineResult`,
либо запускает real pipeline один раз. New decision окончательно durable commit-ится
в той же paper transaction; shadow-only mode не получает ложной financial durability.

SQL и recovery logic не входят в `TradingDecisionCoordinator`. Даже если future
runtime сначала пересчитает Strategy, financial correctness всё равно защищена
paper Unit of Work; preferred M3.14 path восстанавливает уже committed decision и не
пересчитывает Strategy для identical fingerprint.

Portfolio не мутируется до DB commit. Выбран typed staged model:

```text
PortfolioState (immutable authoritative input)
-> PortfolioTransitionCalculator.preview_valuation/preview_fill(...)
-> PortfolioTransition (before/after versions, snapshots, digest)
-> durable transaction COMMIT
-> Portfolio.apply_committed_transition(transition)
```

Future `Portfolio.restore(config, verified_state, recent_fills=())` factory и
`apply_committed_transition()` валидируют все aggregate invariants; private fields не
устанавливаются composition code. Existing direct `mark_to_market/apply_fill` may
remain for non-durable unit/offline paths, но порядок «mutate aggregate → write DB»
запрещён для durable runtime: DB failure не имеет доказуемого rollback.

Если DB COMMIT успешен, а in-memory synchronization падает, DB остаётся authority,
engine немедленно становится `RECOVERY_REQUIRED`; он reload-ит committed state и не
повторяет provider/execution. Новый process всегда исправляет этот gap через recovery.

## 9. Journal schema concept

Append-only означает immutable `journal_entries`, decision/command facts, fills и
portfolio transitions. Query indexes/materialized projections (`paper_cycle_current`,
`portfolio_current`, `observation_deliveries`) могут UPDATE-иться в той же transaction,
но не стирают audit history. Journal — authority; current tables — rebuildable caches.

Design-level schema (не SQL migration):

| Table | Purpose | Primary/UNIQUE constraints | Major foreign keys | Semantics |
|---|---|---|---|---|
| `schema_migrations` | schema history | PK `version`, UNIQUE checksum/name | — | append-only |
| `writer_lease` | advisory writer identity | singleton PK `database_role` | — | mutable advisory; не safety authority |
| `paper_decisions` | stable input and final decision audit | PK `stable_decision_key`; **UNIQUE `decision_slot_key`**; immutable payload digest | journal sequence | immutable after terminal commit |
| `paper_commands` | approved executable authorization | PK `durable_command_key`; one FK decision; UNIQUE decision for executable terminal outcome | stable decision | immutable command facts; materialized status may be separate |
| `fills` | committed full-fill economics | PK/UNIQUE `fill_id`; **UNIQUE `durable_command_key`** | command, decision | append-only; максимум один Fill на command |
| `portfolio_transitions` | valuation/fill state changes | PK `journal_seq`; UNIQUE `(portfolio_id, version_after)`; UNIQUE cycle+transition kind; check `version_after=version_before+1` | decision, optional fill | append-only authority |
| `portfolio_current` | fast current state | PK `portfolio_id`; UNIQUE `(portfolio_id, version)` | last transition seq | mutable materialization in same transaction |
| `portfolio_checkpoints` | replay acceleration | PK `checkpoint_id`; UNIQUE portfolio/version/journal seq/digest | terminal transition seq | append-only verified artifact |
| `journal_entries` | ordered cycle/audit events | PK monotonic `journal_seq`; UNIQUE record identity | decision/command/fill/transition | append-only authority |
| `paper_cycle_current` | terminal lookup/status index | PK `stable_decision_key`; UNIQUE cycle ID | latest journal event | mutable materialization |
| `observation_deliveries` | post-commit diagnostics | PK decision/cycle consumer; attempt count/status | terminal decision | non-authoritative mutable diagnostics |

`journal_seq` uses SQLite `INTEGER PRIMARY KEY` allocation inside the transaction;
replay orders only by this committed integer, never by timestamps. AUTOINCREMENT is
optional: rollback gaps and non-reuse are harmless, contiguous journal sequence is not
required. Portfolio versions, unlike journal sequence, must be gap-free per portfolio.

Duplicate prevention proof:

1. `paper_decisions.PK(stable_decision_key)` allows one committed input payload, while
   `UNIQUE(decision_slot_key)` blocks a corrected-input second trade in the same
   candle/strategy/config slot.
2. `paper_commands.PK(durable_command_key)` allows one approved command identity.
3. `fills.UNIQUE(durable_command_key)` prevents Fill B even if its `fill_id` differs;
   `fills.PK(fill_id)` also rejects conflicting Fill identity reuse.
4. `portfolio_transitions.UNIQUE(portfolio_id, version_after)` prevents a second
   transition at the same aggregate version, while transition→fill FK proves source.
5. `paper_cycle_current.PK(stable_decision_key)` and terminal journal entry prove the
   stored outcome returned on replay.
6. Все пять facts и `portfolio_current` update commit/rollback together. Therefore
   repeated CandleClosed can observe either no facts and safely retry, or the complete
   terminal chain; it cannot commit a second financial mutation.

Materialized paper cycle index имеет минимальные states:

- `RECEIVED` — stable key claimed внутри незавершённой transaction;
- `RISK_REJECTED` — terminal business rejection;
- `NOT_EXECUTED` — terminal deterministic execution outcome без Fill;
- `PORTFOLIO_COMMITTED` — terminal Fill/valuation mutation committed;
- `COMPLETED` — terminal no-intent/planner-declined valuation committed;
- `TECHNICAL_FAILED` — доказуемая failure до financial commit, diagnostics only;
- `RECOVERY_REQUIRED` — persisted quarantine/operator decision.

`DECIDED`, `EXECUTION_PREPARED` и `FILLED` являются audit record types внутри одной
transaction, но не должны оставаться visible committed lifecycle states. Иначе они
создают лишние ambiguous startup branches. Business rejection не смешивается с
technical failure.

Fill record сохраняет: fill/execution/command/source decision IDs, stream, side,
quantity, price, gross quote, fee, fee asset/rate, exact base/quote deltas,
`filled_at_ms`, dust marker, correlation IDs и model version. Все financial values —
canonical Decimal TEXT.

Decision audit хранит stable key и fingerprints, candle identity/fingerprint,
signal direction/reason, intent semantics, risk limits/outcome, execution facts,
portfolio before/after versions и record digests. Полный MarketSnapshot не копируется;
достаточны source candle fingerprint, snapshot ID/as-of и strategy input history
reference/fingerprint. До появления durable market-data store audit должен сохранять
минимальный strategy-input digest и параметры, честно отмечая невозможность полного
independent signal recompute из journal alone.

## 10. Portfolio checkpoints and reconstruction

Journal `portfolio_transitions` — authoritative. Checkpoint содержит:

- portfolio/config/rules identity and versions;
- balances, position quantity/cost basis/average entry;
- realised PnL, total fees, turnover, trade count;
- mark price, valuation timestamp and correlation;
- current/initial/peak equity, unrealised PnL, drawdown/max drawdown;
- Portfolio version and applied-fill count;
- terminal `journal_seq`, previous checkpoint reference and state digest.
- checkpoint/model/schema versions and diagnostic `created_at_ms`.

Derived fields сохраняются для быстрого restore, но всегда проверяются exact Decimal
invariants. Они не принимаются «на веру». Recovery загружает последний valid checkpoint
и replay-ит transitions after its `journal_seq`. При invalid checkpoint выполняется
replay от предыдущего valid checkpoint или genesis; если journal недостаточен или
digest mismatch — `RECOVERY_REQUIRED`.

Authority hierarchy exact:

```text
append-only journal/transitions = financial and audit authority
portfolio_current              = transactionally maintained materialized projection
portfolio_checkpoint           = verified replay acceleration artifact
```

При disagreement journal + deterministic reconciliation определяют valid result.
`portfolio_current` или checkpoint никогда не побеждают journal молча; если replay не
может доказать одно состояние, результат — `RECOVERY_REQUIRED`.

Applied Fill bounded set не является durable source. Все Fill IDs остаются UNIQUE в
journal навсегда; in-memory registry после recovery — только fast-path последних N.

Checkpoint считается verified только если canonical state digest валиден,
`portfolio_version`/`journal_seq` соответствуют terminal transition, config/rules
fingerprints ожидаемы и replay до следующего known state проходит exact invariants.
Stage 3 cadence закрыт: checkpoint каждые **100 committed effective Portfolio
transitions** (valuation и fill transitions, не trades) и на graceful shutdown только
если после последнего checkpoint есть новая transition. Setting:
`PORTFOLIO_CHECKPOINT_INTERVAL_TRANSITIONS=100`. Time-based cadence запрещён. Все
checkpoints сохраняются в Stage 3; rotation откладывается до измерений/отдельной policy.

Proof scenario BUY → HOLD → BUY → SELL должен дать exact equality balances, position,
cost basis, realised PnL, fees, turnover, equity, peak/drawdown/max drawdown, trade
count и version между uninterrupted и recovered runs.

## 11. Valuation and drawdown persistence

Каждый unique closed-candle paper decision вызывает `mark_to_market()` и может менять
version, peak equity и max drawdown даже без Fill. Поэтому **каждая effective valuation
mutation journaled** в той же cycle transaction. No-op valuation может быть записана
как cycle audit без нового portfolio transition/version.

Replay только Fill недостаточен и запрещён как recovery algorithm. Последовательность
valuation records восстанавливает mark price, timestamp, unrealised PnL, current/peak
equity и drawdown history. Последний mark не запрашивается из сети при startup
reconstruction. Новый market mark применяется отдельным новым cycle после recovery.

`PortfolioValuationTransition` хранит stable decision key/candle key, mark price,
`as_of_ms`, `version_before/version_after`, equity before/after, unrealised PnL,
peak equity, drawdown/max drawdown и state digests. Intent cycle commit-ит valuation и
optional Fill transition в **одной paper transaction**. Текущая aggregate semantics
может дать две последовательные versions (valuation, затем Fill); обе должны быть
append-only, gap-free и terminal atomically. HOLD содержит только valuation transition
и terminal decision. Exact no-op mark не создаёт новую Portfolio version.

Для каждого transition enforced:

```text
version_after = version_before + 1
UNIQUE(portfolio_id, version_after)
before_digest = previous after_digest
```

Recovery replay: checkpoint version N → N+1 → N+2 без gaps. Duplicate version,
regression или conflicting payload означает `RECOVERY_REQUIRED`.

## 12. Decimal serialization

SQLite `REAL` запрещён для финансовых значений. Используется canonical finite Decimal
TEXT plus validation:

- zero canonicalized to `"0"` (включая negative zero);
- non-zero — normalized base-10 notation без leading `+`; обычные значения используют
  plain notation, а pathological exponent, который потребовал бы строку длиннее 4096
  символов, — bounded canonical scientific notation;
- trailing zeros не имеют экономической identity и удаляются; instrument precision
  хранится отдельно в rules/version;
- NaN/Infinity запрещены;
- parser не зависит от global Decimal context и проверяет configured digit/scale
  safety limits до arithmetic;
- very small/large значения round-trip exact; никакого float intermediate.

Record digest вычисляется над canonical strings. Если presentation scale является
бизнес-фактом конкретного execution, он хранится отдельным integer scale field, а не
через неканонический Decimal text.

### 12.1 Timestamp policy

- market/candle, risk, execution и Fill timestamps — domain/exchange integer ms;
- valuation timestamp берётся из MarketSnapshot/exchange-time semantics;
- `persisted_at_ms`/`recovered_at_ms` — local wall-clock diagnostics, не business input;
- monotonic clock используется только duration metrics;
- recovery не решает, закрыта ли candle, и не пересчитывает Fill по local wall time.

## 13. Dedup and idempotency after restart

- Storage UNIQUE stable decision/command/fill/version constraints — authoritative.
- Coordinator event-id dedup остаётся bounded fast-path; `source_event_id` не durable,
  поскольку projector может создать другой UUID после restart.
- Paper Engine dedup также fast-path; durable claim выполняется до consumer mutation.
- Portfolio in-memory fill registry fast-path; durable Fill table предотвращает replay
  даже после eviction/restart.
- Identical committed retry возвращает reconstructed durable result без Risk,
  Execution, Portfolio mutation или observer replay.
- Same key с другим fingerprint/payload — typed conflict и `RECOVERY_REQUIRED`.

## 14. Recovery algorithm

Startup ordering нормативен:

1. Open persistence adapter without starting decision runtime.
2. Validate backend/path/runtime configuration and mandatory SQLite safety policy.
3. Validate schema version/migration checksums; refuse unsupported newer schema and
   apply only allowed transactional forward migrations.
4. Acquire/validate writer ownership conservatively; a second writer fails closed.
5. Run journal/database/FK integrity checks and start diagnostic `recovery_run_id`.
6. Locate newest checkpoint whose digest, journal sequence, portfolio/rules identity
   and invariants are valid; otherwise try an earlier checkpoint.
7. Restore immutable `PortfolioState` from checkpoint or configured initial state.
8. Replay ordered valuation/fill transitions after checkpoint (or genesis).
9. Rebuild and exact-verify materialized `portfolio_current` against replay result.
10. Verify decision → command → Fill → transition lineage, uniqueness and digests.
11. Reconcile balances, position/cost basis, fees, PnL, equity, peak/drawdown and
    gap-free Portfolio versions.
12. Inspect terminal lifecycle states and reject any visible incomplete/impossible
    financial cycle. Absent/rolled-back transaction requires no compensation.
13. Produce `CLEAN`, `RECOVERED` or `RECOVERY_REQUIRED` with safe diagnostics.
14. For CLEAN/RECOVERED only, construct validated Portfolio through typed restore API
    and optionally seed bounded recent-Fill fast-path entries.
15. Only CLEAN/RECOVERED allows TradingDecisionCoordinator subscription.
16. Polling starts only after recovery and subscription succeed.

Recovery result: `CLEAN`, `RECOVERED` or `RECOVERY_REQUIRED`. `CLEAN` означает новую
пустую DB либо valid current state без replay; `RECOVERED` — checkpoint/journal replay
или explicit clean uncommitted-crash takeover; `RECOVERY_REQUIRED` блокирует decision
runtime. `MIGRATION_REQUIRED` может быть внутренним adapter step, но не разрешает
торговлю. Нет automatic money repair или fallback к fresh Portfolio, когда persisted
state существует, но invalid.

## 15. Crash matrix with the durable design

| Crash point | Recovery action |
|---|---|
| A. Before Risk | Transaction absent/rolled back; safe retry same stable key |
| B. Risk computed but not committed | All audit rows rolled back; deterministic safe retry |
| C. Fill calculated/persisted before Portfolio write | Same transaction rolls back; command key retry produces/validates the same deterministic facts |
| D. During Portfolio state write | Atomic rollback; no visible Fill or transition; safe retry |
| E. After transaction COMMIT, before in-memory result | Recovery reads terminal cycle and committed transition; no execution retry |
| F. Paper terminal commit before coordinator dedup | Stable decision claim returns stored result; coordinator UUID dedup is irrelevant |
| G. Shadow delivered before paper | Shadow may be duplicated after crash; paper stable key remains safe. Observation exactly-once is not promised |
| H. Paper committed before coordinator result | Stored terminal paper cycle is returned; no second Fill |
| I. Final result before shutdown | Journal/checkpoint reconstructs state; in-memory final result loss does not affect financial mutation |
| After COMMIT, before observer | Business result remains committed; observer may be missing, but trading never replays because of it |

In pure paper mode Fill creation and Portfolio mutation are within one DB transaction,
so `Fill persisted but Portfolio not committed` cannot be a visible normal state.
Violation of this invariant means corruption, not a retryable lifecycle state.

## 16. Reconciliation

Exact checks include:

- gap-free Portfolio versions and strictly increasing committed journal sequences
  (journal sequence gaps from rollback are allowed);
- `portfolio_current.version` equals latest transition version and its digest;
- checkpoint version/sequence is not ahead of current and replay reaches exact current;
- unique immutable Fill payload per fill/command key;
- every Fill references exactly one durable command; every executable command has at
  most one Fill; terminal `EXECUTED` has its Fill transition;
- HOLD/PLANNER_DECLINED/RISK_REJECTED/NOT_EXECUTED outcomes have no Fill and their
  terminal audit is consistent with optional command semantics;
- exact before-state digest equals previous after-state digest;
- exact quote/base deltas, fees, cost basis and realised PnL reconciliation;
- position quantity equals base balance; full sell removes authoritative cost basis;
- trade/applied-fill counts equal committed transitions;
- valuation ordering and exact equity/peak/drawdown relations;
- checkpoint digest/sequence equals replayed journal state;
- valid lifecycle transitions and foreign keys.
- non-negative balances, exact position/base equality and valid equity/PnL/drawdown
  invariants for every replayed transition.

## 17. Corruption handling

Malformed Decimal, broken FK, duplicate conflict, impossible state/lifecycle, schema
mismatch or journal/checkpoint disagreement defaults to fail closed. The adapter does
not round, discard, synthesize or rewrite financial facts automatically. Operator may
restore a verified backup or run a future explicit forensic repair producing a new
audited lineage. Future live reconciliation will compare exchange-vs-local state via a
separate port; it is not part of this paper design.

## 18. Startup and shutdown lifecycle

Future startup:

```text
Settings -> Logging -> Persistence open/lock -> schema check/migration
-> Recovery -> verified Portfolio restore -> Risk/Execution/Paper construction
-> TradingDecisionCoordinator subscribe -> Polling start
```

No `CandleClosed` decision subscriber exists before recovery success. Historical
bootstrap events still must not trade.

Shutdown:

```text
stop polling -> unsubscribe coordinator -> finish/rollback active transaction
-> optional verified checkpoint -> release writer lease -> close persistence
-> close MarketProvider
```

Cleanup attempts all owned resources without masking the primary error. Failed
persistence flush/checkpoint does not delete the journal and is reported as degraded;
provider still closes. Composition root owns adapter, lease and close ordering.

## 19. Logging, audit and observer separation

Business transaction commits before observer. Observer failure may update a separate
diagnostic delivery row in a new small transaction, but never changes cycle outcome,
never rolls back Portfolio and never triggers trade replay. Missing delivery is
reported, not interpreted as missing business commit.

M3.13 включает минимальную `observation_deliveries` projection: consumer identity,
status (`NOT_ATTEMPTED|DELIVERED|FAILED`), attempt count и bounded safe last error.
Она обновляется после business commit отдельной transaction. Automatic observer replay
не входит в Stage 3; logging остаётся primary delivery mechanism. Даже отсутствующая
diagnostic row не означает отсутствующий Fill.

Journal stores typed/versioned business facts. Logs store compact IDs, lifecycle,
latency and traceback. Neither arbitrary metadata nor tracebacks are authoritative.

## 20. SQLite adapter policy

Proposed defaults for Stage 3 implementation:

- path: configurable project data directory, default `data/chappie-trader.sqlite3`;
  never logs/, cwd-dependent temp or source package;
- one composition-root-owned connection, synchronous single thread/writer;
- explicit `BEGIN IMMEDIATE` transaction ownership in adapter Unit of Work;
- WAL mode; `synchronous=FULL`; `foreign_keys=ON`; configurable busy timeout (default
  5000 ms); explicit transaction isolation;
- schema version 1 managed by `schema_migrations` with migration checksum;
- actual write safety relies on `BEGIN IMMEDIATE`, SQLite locking, stable UNIQUE keys
  and gap-free Portfolio versions. `writer_lease` is advisory identity, not a lock that
  can be expired by wall-clock heartbeat;
- startup `quick_check` normally and full `integrity_check` for recovery/backup tools;
- corruption or unsupported schema → no runtime start;
- adapter owns connection and closes it only after transaction/checkpoint completion.

WAL backup uses SQLite backup API or a coordinated checkpoint/online backup, never a
raw copy of only the main DB while WAL contains committed pages.

Stage 3 intentionally не добавляет cross-platform OS lock. Process A записывает
advisory writer identity при clean startup и удаляет её при clean shutdown. Process B,
увидев active lease, **не стартует decision runtime**, даже если сейчас получил SQLite
lock. Lease никогда не считается stale по wall time. После unclean shutdown takeover
разрешается только explicit recovery startup: новый process должен получить
`BEGIN IMMEDIATE`, подтвердить отсутствие другого active transaction, выполнить full
recovery/reconciliation и atomically заменить advisory identity. Если process A жив,
каждая cycle transaction обязана сначала проверить, что lease owner всё ещё равен её
instance ID и `portfolio_current` version/digest совпадает с in-memory state; иначе A
fail closed до Risk/Execution. Concurrent portfolio commits дополнительно блокируются
version/UNIQUE constraints. Read-only
diagnostic connection допустим. Второй trading writer никогда не ожидает молча и не
переходит в RUNNING.

### Settings proposal

Future settings (not added in M3.12 design):

```text
PERSISTENCE_ENABLED=false
PERSISTENCE_BACKEND=sqlite
SQLITE_PATH=data/chappie-trader.sqlite3
SQLITE_BUSY_TIMEOUT_MS=5000
PORTFOLIO_CHECKPOINT_INTERVAL_TRANSITIONS=100
RECOVERY_MODE=strict
```

After runtime integration, persistence should be mandatory for long-running paper
mode. Disabled persistence remains allowed only for explicit development/unit/offline
profiles and must log that restart durability is absent. Production-like paper profile
fails configuration if persistence is disabled.

## 21. Schema and model evolution

- DB schema and serialized model versions are separate concepts.
- `schema_migrations(version, name, checksum, applied_at_ms)` is monotonic.
- Only forward migrations are supported automatically; unsupported newer schema fails.
- Destructive migration requires verified backup and explicit operator action.
- Migration failure rolls back and prevents startup.
- Migrations are ordered, embedded SQL resources owned by SQLite adapter; no external
  framework is required for v1. Startup compares stored checksum for every applied
  version, refuses mismatch/newer DB, and applies each missing migration transactionally.
- Schema v1 is a create-schema migration. Backup is mandatory before any future
  destructive/rebuild migration; additive migrations still preserve transaction and
  checksum rules.
- `record_model_version` is stored for semantically versioned audit payloads:
  stable-decision key, Fill audit, Risk audit, transition and checkpoint.
- Plain normalized columns governed wholly by DB schema need no redundant per-row
  model version unless their semantics can change independently.

## 22. Backup, restore and retention

- Append-only trading/fill/transition journal is not automatically deleted.
- Journal, decisions, commands, fills и все checkpoints сохраняются indefinitely по
  умолчанию в Stage 3. Rotation начинается только после измерения размера и отдельного
  retention ADR; journal остаётся authoritative.
- Old coordination diagnostics may be archived only if stable decision, business audit
  and foreign-key lineage remain intact.
- VACUUM is explicit maintenance while trading is stopped; never an implicit startup
  action.
- Restore opens copied DB read-only first, performs `integrity_check`, validates schema,
  latest checkpoint digest and full recovery dry-run, then promotes it while runtime
  is stopped.
## 23. Security

- DB и backup paths используют least-privilege filesystem access;
- database не содержит secrets, API keys или private exchange credentials;
- sensitive paths и stored payload не попадают в arbitrary exception/log output;
- operator-supplied path нормализуется и не может молча указывать на source tree,
  logs или world-writable shared location;
- backup/restore проверяет права, integrity и ожидаемую portfolio identity;
- encryption-at-rest зависит от deployment/OS и выносится в отдельный operational
  decision до хранения чувствительных live данных; paper audit всё равно защищается
  filesystem ACL и backup policy.

## 24. Test strategy and crash injection

Implementation milestone requires unit, SQLite integration and full restart tests:

- empty DB/schema bootstrap and reopen;
- BUY, SELL, dust liquidation, HOLD/planner decline and risk rejection commits;
- valuation/peak/max drawdown restoration;
- checkpoint plus replay, invalid checkpoint fallback and journal corruption failure;
- duplicate stable decision/intent/command/Fill before and after restart;
- stable key determinism, Decimal extremes/negative zero and config fingerprint change;
- unsupported/newer schema, failed migration, FK/lifecycle conflict;
- second writer/second process lock behavior;
- observer failure after commit without replay;
- online backup/restore integrity smoke;
- architecture tests forbidding SQLite imports outside adapter/composition.

Failpoints injected through test fakes/dependency hooks, not production flags:
`BEFORE_TRANSACTION`, `AFTER_DECISION_CLAIM`, `AFTER_FILL_PERSIST`,
`AFTER_PORTFOLIO_STATE_WRITE`, `BEFORE_COMMIT`, `AFTER_COMMIT`, `BEFORE_OBSERVER`.
Every failpoint asserts DB visibility, Portfolio equality, retry classification and
absence of duplicate Fill.

Full restart scenario constructs entirely new adapter, Portfolio, Risk, Execution,
Paper and Coordinator objects, restores BUY → HOLD → BUY → SELL history, continues
with new events and compares exact economic state with uninterrupted execution.

## 25. Open questions

M3.12 review закрыл Stage 3 blockers: hash покрывает фактический strategy input,
checkpoint cadence равен 100 effective transitions, SQLite lock + advisory identity
используются без heartbeat/OS lock, observer diagnostics минимальны, migrations —
embedded ordered resources, а historical reprocessing требует new Portfolio lineage.

Остаются только non-blocking future decisions:

1. Добавлять ли durable OHLCV store для независимой реконструкции input, если одного
   fingerprint/audit digest станет недостаточно регуляторно.
2. Когда measured SQLite size оправдает checkpoint/diagnostic retention ADR.
3. Нужна ли platform-specific OS lock после появления нескольких operational hosts.
4. Какая encryption-at-rest policy потребуется до хранения live financial state.

Они не меняют journal authority, stable keys, transaction или fail-closed recovery.

## 26. Package boundaries and implementation milestones

Recommended layout:

```text
app/persistence/
  ports.py          # Protocols, no SQLite
  models.py         # immutable durable records/keys
  exceptions.py     # typed storage/recovery errors
  canonical.py      # canonical Decimal/JSON serialization and SHA-256 helper
  keys.py           # stable decision, slot, configuration and command identities
  recovery.py       # adapter-independent recovery/reconciliation
  sqlite.py         # only SQLite adapter/imports
  migrations/       # embedded ordered and checksummed SQL resources
```

`app.persistence` must not import Binance/polling/strategy implementations. Domain
packages must not import persistence. `application.py` is the composition root allowed
to select the concrete adapter.

### M3.13 implementation status

Изолированно реализованы immutable persistence records, runtime-checkable ports,
canonical finite Decimal/JSON encoding, versioned stable decision/slot/command keys,
SQLite schema v1 и transactional migration runner. `SQLiteTradingPersistence`
применяет WAL, `synchronous=FULL`, foreign keys, busy timeout и `BEGIN IMMEDIATE`;
financial transaction автоматически rollback-ится без явного terminal commit.

Schema обеспечивает PK/UNIQUE для stable decision, semantic decision slot, durable
command, Fill и `(portfolio_id, version_after)`. Journal защищён от UPDATE/DELETE
database triggers; `portfolio_current` и append-only checkpoints обновляются в той же
cycle transaction. RecoveryCoordinator проверяет database integrity, state digests,
gap-free version/digest chain и exact current projection; сомнительное состояние
возвращает `RECOVERY_REQUIRED` без usable PortfolioState. Online backup использует
`sqlite3.Connection.backup()`. Runtime, application startup, PaperTradingEngine и
TradingDecisionCoordinator намеренно не подключены до M3.14.

### M3.13 final safety review

Final review добавил authoritative `portfolio_roots`: initial PortfolioState version 0,
state digest и configuration fingerprint сохраняются явно и задают genesis digest для
полного journal replay. Новый arbitrary initial balance после появления lineage не
принимается. Missing `portfolio_current` может быть read-only reconstructed из root +
journal как `RECOVERED`; disagreement существующей projection остаётся
`RECOVERY_REQUIRED` и автоматически не исправляется.

Cycle transaction допускает no-op HOLD/PLANNER_DECLINED/RISK_REJECTED/NOT_EXECUTED без
ложного Portfolio version increment. Для effective valuation + Fill поддерживаются две
gap-free transitions в одной transaction (`N -> N+1 -> N+2`); `portfolio_current`
обязан совпадать с последней transition и journal sequence. `EXECUTED` требует command,
Fill и Portfolio transition; остальные outcomes применяют строгую command/Fill policy.

Checkpoint никогда не скрывает повреждение раннего journal: recovery валидирует полную
root-to-current version/digest chain, затем использует newest valid checkpoint только
для определения replay suffix. Corrupt checkpoints пропускаются с bounded diagnostic;
если все checkpoints invalid, полный authoritative journal replay от root допустим.

SQLite resources закрываются детерминированно. В частности, backup destination
использует explicit `closing()` (обычный sqlite context manager закрывает transaction,
но не connection), constructor закрывает connection при configuration/migration error,
а operations после close и backup внутри active financial transaction typed-rejected.
Canonical Decimal использует bounded scientific notation только для pathological
exponents; semantic Decimal scale не влияет на fingerprints.

Roadmap after M3.13 implementation:

- M3.12 — design (this document);
- M3.13 — implementation completed; final safety review passed;
- M3.14 — Runtime Recovery Integration and durable Paper Unit of Work;
- M3.15 — Crash/Restart Safety Review;
- M3.16 — Stage 3 Completion.

## 24. M3.14 runtime integration

M3.14 подключает `TradingPersistence` только в application/paper orchestration layer.
Production application требует persistence при `PAPER_TRADING_ENABLED=true`, открывает
SQLite с process-lifetime exclusive writer ownership, проверяет schema/integrity,
инициализирует authoritative `portfolio_roots` только для fresh database и выполняет
recovery до создания и подписки `TradingDecisionCoordinator`.

`Portfolio.restore()` валидирует полное восстановленное состояние без SQL dependency.
Pure `preview_mark_to_market()` и `preview_fill()` вычисляют immutable transitions без
мутации aggregate. Durable flow записывает decision, optional command/Fill, одну или
две effective Portfolio transitions, `portfolio_current` и terminal outcome в одной
transaction. Только COMMIT разрешает `apply_committed_transition()`; observer следует
после успешной memory synchronization.

Stable decision identity хеширует фактический ordered `MarketSnapshot.closed_candles`.
Terminal duplicate после restart возвращает typed durable replay без Risk/Execution.
Decision-slot correction, configuration mismatch, writer conflict, commit ambiguity и
post-commit sync failure блокируют paper path fail-closed; shadow isolation сохраняется.

Checkpoints остаются optimization: interval и shutdown checkpoints не входят в
authoritative financial transaction. Missing `portfolio_current` rebuild выполняется
явно только из verified journal head; conflicting projection автоматически не
ремонтируется. M3.15 сохраняет отдельный crash/restart torture-review scope.

## 25. M3.15 crash/restart safety review

M3.15 подтвердил двухсостоянийный crash contract: до COMMIT весь cycle rollback-ится,
после COMMIT terminal decision, optional command/Fill и Portfolio journal
восстанавливаются новым process без повторного Risk/Execution. Lost commit
acknowledgement переводит текущий paper engine в `DEGRADED`; restart определяет истину
только из SQLite.

Post-commit memory synchronization применяет весь valuation+Fill batch атомарно:
ошибка в поздней transition не оставляет aggregate в промежуточном состоянии. Observer
и checkpoint failures остаются non-financial. Полная matrix, corruption proofs и
ограничения backup freshness/database deletion перечислены в
`M3_15_CRASH_RESTART_REPORT.md`.

## 26. M3.16 Stage 3 completion

Controlled two-run public Binance data smoke использовал одну ignored SQLite database.
Run 1 начал с Portfolio/journal version `0/0`, durable-recorded новый shared HOLD cycle
как `1/1` и завершился cleanly. Run 2 восстановил exact `1/1` с `recovery=CLEAN`, затем
новый post-restart shared HOLD cycle продвинул состояние до `2/2` и также завершился
cleanly. Оба shared pipeline результата были доставлены shadow и paper consumers.

Smoke не создавал BUY/SELL, command или Fill: balances, trade count и `last_fill_id`
остались неизменны. Executable restart/dedup guarantees подтверждены automated
deterministic crash suites, а не real smoke. Сохраняются ограничения: нет durable OHLCV
store, external installation identity manifest, доказательства global backup freshness,
exactly-once observer delivery, live execution или exchange reconciliation. WAL/SHM
остаются SQLite-owned и не должны изменяться вручную при активной database.

M4.8 schema v4 adds an exchange-specific Portfolio root/current projection and append-only
settlement journal. Each normalized exchange trade has one durable settlement identity. A
quote-fee transition, Portfolio head update and session turnover update commit atomically before
memory synchronization; restart restores the committed head and suppresses replay. Unsupported
fee assets are recorded as blocked settlement facts and never converted heuristically. M4.8 final
safety review passed on 2026-08-12; M4.9 crash/restart/duplicate-order work is completed and its
final crash/restart safety review passed on 2026-08-13. M4.10 is next and not started.

The M4.8 completion pass validates every persisted settlement record against its digest and
exchange-trade identity, rebuilds the consecutive Portfolio version/head chain, and recomputes
session executed turnover from authoritative settled quote quantities. Any missing, corrupt or
split-brain link raises recovery-required behavior before readiness. A recovered PREPARED order
remains durable evidence only; startup never converts it into a network side effect.

Migration 004 also binds every settlement to its authorizing SAFE reconciliation run and positive
generation. Recovery verifies that linkage and settlement refuses financial application while
the current reconciliation state lacks durable complete-history evidence.

### Accepted M4.10 Phase 3 schema-v5 design

The Phase 3 offline review found that schema v4 cannot safely express a one-shot operator
ceremony. `execution_control_state` can durably clear the kill switch, but a crash after that clear
and before `begin_submission()` leaves no consumed attempt or other durable fact preventing restart
from minting a new process-local capability. Ordinary CLEARED state is therefore insufficient for
the first real smoke.

Accepted ADR-064 requires an additive `exchange_smoke_authorities` record bound to execution root,
account, session, control/reconciliation generations, operator run and economic-slot identities,
exact command, rules/configuration fingerprints and canonical risk-envelope/balance-scope digests.
The only committed state is CONSUMED. One `BEGIN IMMEDIATE` operation inserts the transaction-local
authority, creates the sole PREPARED order/event/reservation, links command and authority one-to-one,
marks CONSUMED and appends its audit event. There is no authority-only commit. The global switch
remains ACTIVE; commit returns only an in-memory exact-command claim, never durable capability.
Restart loses the claim and is recovery-only; it may reconcile the same identity but cannot restore
first-submit eligibility. Phase 4 reopens this exact lineage and settlement journal without a new
authority.

Conceptual constraints are: PK authority ID; UNIQUE execution-root/operator-run, Phase 3 economic
slot and consumed command; FKs to root/session/command; TESTNET/binance-spot and CONSUMED-linkage
CHECKs; canonical 64-character digests; append-only events; and UPDATE/DELETE denial. Recovery
verifies authority and event digests, lineage, expected generations, exact one-to-one command,
BUY-only envelope, timestamps and payload fingerprints. Any missing/impossible link is
`RECOVERY_REQUIRED`. Migration 005 remains additive and transactional; migrations 001–004 remain
byte-identical and a failed upgrade leaves v4 valid under the existing migration transaction.

The accepted design records a zero-base managed Portfolio root with an explicit `25 USDT` quote
allocation. Exchange balances remain observations, not Portfolio genesis: the pre-existing
`1 BTC` is external/unmanaged and cannot be imported, settled or sold. Existing Portfolio root
configuration can represent `0 BTC / 25 USDT`, so v5 adds authority/balance-scope evidence rather
than a second financial root. Migration 005 and the offline atomic authority API are now implemented
with BaseException crash proofs and recovery validation; the final adversarial safety review passed.
The Phase 3 one-shot operator harness is complete and its final offline operator safety review passed,
while execution remains unauthorized pending a separate explicit authorization.

Phase 4 reopens that exact schema-v5 database without reconstructing the Phase 3 claim. Its offline
recovery/settlement harness is complete and the final adversarial safety review passed; production
execution authorization remains pending. Its harness reconciles the sole order and authoritative
trades, records complete coverage only when facts
agree, and reuses atomic exactly-once settlement. Production execution remains unauthorized; ACK alone
is never treated as a fill.

### Schema-v6 zero-commission settlement resolution

ADR-065 accepts exact finite Decimal zero as asset-neutral without changing the authoritative trade.
Migration 006 is required because `exchange_trade_settlements` permits only one append-only row per
trade and the existing `BLOCKED_UNSUPPORTED_FEE` row cannot be updated, deleted or replaced. The
additive `exchange_trade_settlement_resolutions` journal conceptually stores a deterministic
resolution ID and fill ID, the unique prior blocked settlement ID, the full trade identity and command,
Portfolio/session/reconciliation lineage, `zero-commission-asset-neutral/v1`, reason code, version
before/after, before/after state and digests, timestamp, canonical record payload and digest. UPDATE and
DELETE triggers forbid mutation. UNIQUE constraints cover the blocked settlement, trade identity,
resolution/fill identity and `(portfolio_id, portfolio_version_after)`.

One `BEGIN IMMEDIATE` transaction revalidates the original block, exact `commission == Decimal(0)`,
complete SAFE coverage, current Portfolio digest/version and session generation; inserts the resolution,
advances Portfolio by the authoritative trade, adds authoritative quote quantity to turnover, advances
session generation and commits. No trade or old settlement row changes. The existing
`last_settlement_seq` may reference the original blocked row; recovery proves the resulting version
through its unique resolution link rather than interpreting that pointer as a successful historical
settlement. Pre-commit failures roll back every effect; post-commit acknowledgement loss discovers the
same resolution and returns it idempotently.

Recovery derives effective trade state as `PENDING`, `BLOCKED_UNSUPPORTED_FEE`, `SETTLED`, or
`RESOLVED_ZERO_COMMISSION`. It retains the historical block in audit output but excludes a valid linked
resolution from the unresolved-block count. Portfolio authority validation merges ordinary SETTLED
rows and resolution rows by `portfolio_version_after`, requires a contiguous version/digest chain and
recomputes session turnover. Any orphan, duplicate, digest mismatch or cross-table version collision is
`RECOVERY_REQUIRED`.

Migration 006 and the offline resolution service now implement this design. Migration 006 SHA-256 is
`7b6543b489fd9ca67435093e3e34f5fde48101803e65cd502ae5c1004af80f1d`; migrations 001–005 remain
byte-identical. Implementation status does not authorize production migration or remediation.
