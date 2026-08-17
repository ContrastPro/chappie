# Shadow Trading Decision Engine Design

## 1. Статус и область

- Статус: M2.5 design completed; M2.6 implementation completed.
- Final review: passed.
- Real Binance shadow smoke-run: passed, 2026-08-04.
- Дата: 2026-08-04.
- Основание: ADR-016, ADR-019–ADR-024, `MARKET_EVENTS_DESIGN.md` and the
  canonical [Trading decisions](docs/core/TRADING_DECISIONS.md) design.

Runtime evidence: реальный Binance Spot smoke-run `BTCUSDT 1m` завершён с
`handled=8`, `successful=8`, `hold=8`, `failed=0`, `observation_failures=0` и
`exit_code=0`. Feature flag остаётся выключенным по умолчанию. Shadow mode не
содержит Risk Manager, portfolio mutation, execution или order placement.

Документ определяет application-level сервис, который после подтверждённого
`CandleClosed` вычисляет торговое решение в shadow mode и делает результат
наблюдаемым, но не исполняет его. M2.5 зафиксировал design, а M2.6 реализовал Python
API, feature-flagged runtime wiring и Settings.

```text
CandleClosed
    -> ShadowTradingDecisionEngine
    -> MarketCacheReader.get_snapshot(...)
    -> MarketSnapshotFactory
    -> TradingDecisionPipeline
    -> DecisionPipelineResult
    -> ShadowDecisionRecord
    -> ShadowDecisionObserver
```

Engine не выполняет risk, execution, portfolio mutation, private Binance API,
paper/live orders, database persistence и не изменяет Market Cache.

## 2. Архитектурная роль и границы

`ShadowTradingDecisionEngine` — синхронный application service. Он владеет только
своей подпиской и bounded dedup state. Composition root создаёт dispatcher, cache,
pipeline, factories, observer и engine, а также управляет их общим lifecycle.

Engine:

- подписывается на `CandleClosed` через внешний `MarketEventDispatcher`;
- читает подтверждённое нормализованное состояние через read port;
- проверяет согласованность event/cache;
- строит `MarketSnapshot` без чтения wall clock;
- ровно один раз вызывает `TradingDecisionPipeline` для принятого event ID;
- формирует immutable audit record, вызывает observer и обновляет status;
- изолирует shadow failures от market ingestion и последующих handlers.

Он не размещается внутри Polling Engine, dispatcher, cache, Strategy, Intent
Planner или `TraderAgent` business logic. Market и trading domain не импортируют
shadow application service.

Будущий paper runtime не должен запускать Strategy или pipeline второй раз. M3.10
определяет общий `TradingDecisionCoordinator`, который создаёт один
`DecisionPipelineResult`, после чего shadow observer path и PaperTradingEngine получают
его как независимые consumers. Текущий Shadow engine не меняется в design milestone и
не может быть одновременно подписан с coordinator после runtime migration.

### 2.1 Migration to ShadowDecisionConsumer

M3.11 должен выделить `ShadowDecisionConsumer.consume(event, snapshot,
decision_result, coordination_id)`. Consumer сохраняет создание shadow record и
observer semantics, но не читает cache, не строит snapshot, не запускает pipeline,
не подписывается на dispatcher и не дублирует source-event dedup.

Event-level lifecycle/counters переходят coordinator. Consumer status ограничен
`handled_decisions`, `successful_records`, `observation_failures`, `last_record` и
`last_error`. Существующий engine может остаться compatibility wrapper только при
доказанной необходимости; composition root выбирает ровно один active decision path.

M3.11 реализует `ShadowDecisionConsumer` и общий pure event/cache consistency helper.
Application runtime больше не создаёт legacy engine при включённых decision features:
единственным subscriber становится TradingDecisionCoordinator. Legacy API сохранён
для standalone compatibility до отдельного решения после final safety review.

## 3. Рекомендуемый public API

```text
ShadowTradingDecisionEngine(
    cache_reader: MarketCacheReader,
    snapshot_factory: MarketSnapshotFactory,
    decision_pipeline: TradingDecisionPipeline,
    snapshot_id_factory: SnapshotIdFactory,
    shadow_run_id_factory: ShadowRunIdFactory,
    observer: ShadowDecisionObserver,
    *,
    history_limit: int,
    dedup_capacity: int,
    monotonic_clock: MonotonicClock | None = None,
)

start(dispatcher: MarketEventDispatcher) -> None
stop() -> None
handle_candle_closed(event: CandleClosed) -> None
status -> ShadowDecisionStatus
```

Решения lifecycle:

- engine самостоятельно подписывает свой handler и хранит `SubscriptionToken`;
- dispatcher передаётся в `start`, но не принадлежит engine;
- первый `start` переводит `STOPPED -> RUNNING` и создаёт ровно одну подписку;
- повторный `start` отклоняется `ShadowDecisionLifecycleError`;
- `stop` отписывает token, переводит `RUNNING -> STOPPED` и идемпотентен;
- после `stop` dispatcher не вызывает handler; прямой вызов handler в `STOPPED`
  отклоняется typed lifecycle error и полезен для тестов;
- engine не закрывает dispatcher, cache, pipeline, observer или их ресурсы.

## 4. Market Cache read port

Engine не зависит от `InMemoryMarketCache`. Минимальный structural Protocol
располагается рядом с shadow application service, например в
`app/trading/shadow.py`, чтобы market-модуль не зависел от application:

```text
MarketCacheReader(Protocol):
    get_snapshot(
        key: MarketStreamKey,
        limit: int | None = None,
    ) -> tuple[Candle, ...]
```

Это подмножество существующего публичного cache API, а не новый competing cache
contract. `InMemoryMarketCache` удовлетворяет Protocol структурно. Engine всегда
передаёт `history_limit`; default proposal — 1000. Limit должен быть не меньше
`Strategy.required_history` и не больше cache capacity. Возвращаемые данные должны
быть defensive; engine не сохраняет и не изменяет их.

## 5. Snapshot creation и event consistency

Для принятого `CandleClosed`:

1. Использовать `event.stream_key` и прочитать до `history_limit` свечей.
2. Проверить, что snapshot не пуст и содержит event candle.
3. Найти свечу по `open_time_ms` и потребовать точное совпадение финальных
   `close_time_ms`, OHLCV с immutable event snapshot.
4. Потребовать `event.occurred_at_ms > event.candle.close_time_ms`.
5. Разрешить более новые закрытые свечи и одну текущую открытую свечу, если cache
   успел продвинуться; event candle при этом обязана оставаться в прочитанном окне.
6. Создать snapshot ID внедрённой фабрикой.
7. Вызвать `MarketSnapshotFactory.create` с:
   `as_of_ms=event.occurred_at_ms`, `source_event_id=event.event_id`,
   `correlation_cycle_id=event.correlation_cycle_id`.

Пустой cache, отсутствие event candle, несовпадающий финальный snapshot и
закрытие на неверной временной границе — `ShadowEventCacheMismatchError`. Ошибка
чтения port — `CACHE_READ`; ошибка фабрики snapshot — `SNAPSHOT_BUILD`.

Недостаточная, но согласованная история не является engine failure. Snapshot
передаётся Strategy, которая возвращает объяснимый HOLD по существующему contract.
Baseline читает только `closed_candles`; `latest_open_candle` на решение не влияет.

Если `history_limit` настолько мал, что event candle уже вытеснена из окна, это
configuration/consistency failure, а не основание искать данные через provider.

## 6. ShadowDecisionRecord

Wrapper нужен как production application model: он отделяет результат вычисления
от факта наблюдения и создаёт стабильную границу для logging, metrics и будущей
persistence без изменения `DecisionPipelineResult`.

```text
ShadowDecisionRecord (frozen, slots)
    shadow_run_id: str
    source_event_id: str
    correlation_cycle_id: str
    stream_key: MarketStreamKey
    evaluated_at_ms: int
    snapshot_id: str | None
    pipeline_run_id: str | None
    signal_id: str | None
    signal_direction: SignalDirection | None
    signal_reason_code: str | None
    intent_id: str | None
    intent_action: OrderIntentAction | None
    target_exposure: Decimal | None
    pipeline_outcome: PipelineOutcome | None
    strategy_name: str | None
    strategy_version: str | None
    planner_name: str | None
    planner_version: str | None
    status: ShadowDecisionRecordStatus
    failure_category: ShadowDecisionFailureCategory | None
    failure_message: str | None
    observation_status: ShadowObservationStatus
```

`status = DECISION_COMPLETED | DECISION_FAILED | DUPLICATE_IGNORED`.
`observation_status = NOT_ATTEMPTED | OBSERVED | OBSERVATION_FAILED`.
Completed record требует pipeline fields; failed record сохраняет доступный prefix
audit chain и typed category. Failure message безопасная и краткая, без payload,
snapshot history или secrets. Record, nested values и metadata неизменяемы.

Audit chain:

```text
source_event_id -> snapshot_id -> shadow_run_id
                -> pipeline_run_id -> signal_id -> intent_id | None
```

`shadow_run_id` не заменяет pipeline run ID. `SnapshotIdFactory` и
`ShadowRunIdFactory` находятся рядом с engine; production реализации используют
UUID, tests — deterministic sequences. Empty, whitespace и non-string outputs,
а также factory exceptions преобразуются в typed shadow factory errors.

## 7. Observer port и logging adapter

```text
ShadowDecisionObserver(Protocol):
    on_decision(record: ShadowDecisionRecord) -> None
```

Первая реализация — `LoggingShadowDecisionObserver`. Engine не зависит от logging
или filesystem напрямую и не владеет lifecycle observer. Позже без изменения
engine могут появиться JSONL, database, metrics и UI observers.

Logging observer пишет один компактный structured entry на record:

- shadow/source/correlation/snapshot/pipeline/signal/intent IDs;
- symbol, interval, direction, reason, action, Decimal exposure;
- strategy/planner identity, outcome, record и failure category.

Не логируются полная история свечей, snapshot/metadata, Binance payload и secrets.
HOLD логируется наравне с intent, поскольку отсутствие действия является
объяснимым торговым решением. Expected HOLD не создаёт console stack trace.

## 8. Error isolation и observer failure

Handler никогда не откатывает cache и не превращает shadow processing failure в
market ingestion failure.

Политика:

- ожидаемые typed ошибки cache/consistency/snapshot/pipeline/factory перехватываются,
  превращаются в `DECISION_FAILED`, передаются observer и не пробрасываются;
- неожиданные `Exception` из engine dependencies изолируются как `INTERNAL_ERROR`,
  логируются application logger с traceback, status обновляется, handler возвращает;
- `KeyboardInterrupt`, `SystemExit` и другие `BaseException` не перехватываются;
- handler failure поэтому обычно не создаёт dispatcher delivery degradation и не
  влияет на polling retry/recovery;
- следующий event обрабатывается после любой isolated failure.

Observer вызывается ровно один раз и без retry. Его expected или unexpected
`Exception` не запускает pipeline повторно и не выходит из handler: semantic
decision остаётся `DECISION_COMPLETED`, а observation становится
`OBSERVATION_FAILED`; status увеличивает observer failures и сохраняет
`OBSERVER_FAILURE`. Traceback пишется безопасно. `BaseException` не перехватывается.

Failure categories:

```text
DUPLICATE_EVENT
CACHE_READ
EVENT_CACHE_MISMATCH
SNAPSHOT_BUILD
PIPELINE_EVALUATION
OBSERVER_FAILURE
INTERNAL_ERROR
```

Duplicate является диагностической категорией, но не failed decision.

## 9. Bounded deduplication

Dedup входит в минимальную M2.6 implementation:

- key — exact `source_event_id`;
- capacity configurable, default 10 000, строго положительная;
- insertion-ordered bounded `set + deque` или эквивалент без silent eviction;
- accepted event ID резервируется до cache read, чтобы failure также не был
  повторно вычислен случайной повторной доставкой;
- exact duplicate не вызывает ID factories, cache, snapshot, pipeline или observer
  повторно; status увеличивает `ignored_duplicates`;
- одинаковая свеча с другим event ID обрабатывается отдельно;
- при превышении capacity явно удаляется самый старый ID;
- state single-threaded, in-memory и сбрасывается после restart.

Это защита shadow observation, а не durable delivery и не execution idempotency.
Duplicate record наружу не публикуется: первичный record уже был observer-у доставлен
или его observation failure явно отражён в status. Duplicate можно логировать на
debug level без создания второго shadow record.

## 10. Status и lifecycle

```text
ShadowDecisionLifecycle = STOPPED | RUNNING

ShadowDecisionStatus (frozen, slots)
    lifecycle
    subscribed
    handled_events
    successful_decisions
    hold_decisions
    intent_decisions
    failed_decisions
    observer_failures
    ignored_duplicates
    last_event_id
    last_snapshot_id
    last_pipeline_run_id
    last_signal_direction
    last_error_category
    last_error_message
    last_evaluation_duration_ms
    last_observer_duration_ms
```

Отдельный decision failure не останавливает engine, поэтому `FAILED` lifecycle не
нужен. `handled_events` считает только новые принятые event IDs; duplicates имеют
отдельный счётчик. HOLD и INTENT_CREATED входят в successful decisions.

Durations измеряются только injected monotonic clock и не участвуют в domain time,
snapshot или record timestamps. В минимальной версии clock опционален: без него
duration fields равны `None`. Wall clock запрещён.

## 11. Bootstrap, threading и performance

Bootstrap не публикует исторические `CandleClosed`, поэтому engine не выполняет
исторический replay. Первое решение появляется только после нового подтверждённого
закрытия. Backtest/replay получает отдельный orchestration mechanism.

Первая версия — один process, thread и synchronous dispatcher; engine не
thread-safe. Один event полностью обрабатывается до следующего. Очередь, worker,
asyncio и WebSocket не добавляются. Чтение до 1000 свечей и EMA O(n) один раз в
минуту для BTCUSDT не требует оптимизации. Если latency начнёт мешать ingestion,
очередь потребует отдельного ADR.

## 12. Configuration proposal

```text
SHADOW_DECISIONS_ENABLED=false
SHADOW_HISTORY_LIMIT=1000
SHADOW_DEDUP_CAPACITY=10000
SNAPSHOT_STALE_AFTER_MS=120000
EMA_FAST_WINDOW=12
EMA_SLOW_WINDOW=26
LONG_TARGET_EXPOSURE=1
```

Validation:

- history/dedup/stale limits положительны;
- history limit не больше cache capacity и не меньше Strategy required history;
- EMA fast >= 2, slow > fast;
- target exposure — finite `Decimal`, `0 < value <= 1`;
- feature flag не изменяет `TRADING_MODE=paper` и не разрешает execution/live.

Default `false` сохраняется до отдельного successful shadow smoke-run. При disabled
composition root не создаёт subscription. При enabled он собирает EMA Strategy,
LongOnlyIntentPlanner, TradingDecisionPipeline, SnapshotFactory, logging observer и
shadow engine, затем вызывает `start(dispatcher)`. Shutdown вызывает `engine.stop()`
до освобождения dispatcher/provider. Наличие intent никогда не вызывает execution.

## 13. Deterministic test plan для M2.6

Lifecycle:

- start создаёт одну подписку; duplicate start typed error;
- stop отписывает, повторный stop безопасен, после stop обработки нет;
- direct handler call детерминированно тестируется;
- engine не закрывает dispatcher/cache/pipeline/observer.

Success и consistency:

- CandleClosed → snapshot → HOLD record;
- bullish → BUY/ENTER_LONG, bearish → SELL/EXIT_LONG;
- audit correlation chain и `event.occurred_at_ms` сохраняются;
- event candle точно совпадает с defensive cache snapshot;
- следующая open candle допустима и не участвует в EMA;
- short history даёт HOLD, а не engine failure.

Failures и recovery:

- cache read error, empty/missing/mismatched candle;
- invalid close boundary, snapshot/factory/pipeline errors;
- observer error сохраняет completed decision и failed observation;
- unexpected Exception из каждой dependency изолируется с traceback;
- `BaseException` не поглощается; следующий event после обычной ошибки успешен.

Dedup:

- duplicate event ID ignored до factories/cache/pipeline/observer;
- capacity eviction явная и deterministic;
- другой event ID для той же свечи обрабатывается;
- failed first processing также защищено от повторного вычисления.

Runtime и boundaries:

- disabled setting не создаёт subscription, enabled создаёт;
- logging compact и не содержит history/payload/secrets;
- market не импортирует shadow; shadow не импортирует Binance, HTTP, database,
  execution или risk; trading domain не импортирует application;
- полностью offline integration от `CandleClosed` до observed record без production
  execution wiring.

## 14. Минимальный implementation plan M2.6

1. Добавить typed shadow exceptions, enums, immutable record/status и два ID Protocol.
2. Добавить `MarketCacheReader` и `ShadowDecisionObserver` Protocol без изменения
   существующего cache API.
3. Реализовать synchronous engine lifecycle, consistency checks и bounded dedup.
4. Реализовать `LoggingShadowDecisionObserver` с safe compact fields.
5. Добавить Settings и feature-flagged composition wiring; default оставить false.
6. Добавить unit, boundary и offline integration tests; runtime execution/risk не
   подключать.
7. Провести final review и отдельный shadow smoke-run до изменения default.

## 15. Открытые вопросы после первой версии

- Следует ли вынести `MarketCacheReader` в общий market contracts module после
  появления второго consumer?
- Достаточна ли dedup capacity 10 000 для будущих нескольких потоков?
- Нужен ли отдельный observer status history помимо последних счётчиков?
- Нужно ли обязательно собирать duration metrics в M2.6 или отложить clock?
- Один engine на Strategy или будущий coordinator для нескольких pipelines?
- Как конфигурировать несколько symbols/intervals без shared mutable state?
- Когда добавить JSONL/database observation persistence?
- Нужна ли durable dedup после restart вместе с persistence?
- Следует ли сохранять абсолютно все HOLD records? Для первой версии — да.
- Где проходит будущая граница между shadow record и durable decision history?
