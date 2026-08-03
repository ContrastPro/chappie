# Market Events Design Proposal

## 1. Статус и область

- Статус: proposed for M1.7 implementation review.
- Дата: 2026-08-03.
- Архитектурное основание: ADR-018.

Документ определяет синхронный типизированный механизм уведомления внутренних модулей об изменениях Market Cache:

```text
MarketProvider
    -> Polling Engine
    -> Validation
    -> Market Cache
    -> Market Event Projector
    -> Event Dispatcher
    -> Indicators / Strategies / Analytics / AI
```

Первая версия работает в одном процессе и потоке исполнения, с одним writer, без `asyncio`, message broker, persistence, стратегий, индикаторов, execution и live trading.

## 2. Базовая семантика

Market Cache остаётся источником текущего нормализованного состояния. Событие не является заменой cache snapshot и не гарантирует возможность восстановить состояние после пропуска доставки.

Market Events сообщают о двух разных классах фактов:

1. **Cache change events** — подтверждённое изменение или подтверждённый lifecycle-переход уже сохранённой свечи после успешной cache reconciliation.
2. **Ingestion diagnostic events** — обнаруженная проблема ingestion, которая может не изменить cache.

Failed validation, rejected batch и cache rollback не создают `CandleInserted`, `CandleUpdated`, `CandleClosed` или `CandleEvicted`.

## 3. Общий event envelope

Каждое событие является immutable value object и содержит:

- `event_id` — непрозрачный уникальный идентификатор;
- `occurred_at_ms` — явное wall-clock время создания события;
- `stream_key: MarketStreamKey`;
- `cycle_id` — correlation ID одного polling/replay cycle;
- `source` — типизированный источник, первоначально `REST_POLLING` или `REPLAY`;
- `reason` — типизированная причина конкретного события.

`event_id`, `occurred_at_ms` и `cycle_id` передаются через тестируемые factories/context. Event model не читает системное время и не создаёт случайный UUID скрыто внутри доменного компонента.

## 4. Immutable candle snapshot

События не содержат ссылку на mutable `Candle`, хранящуюся в cache. Используется отдельный immutable `CandleSnapshot` со значениями:

- `open_time_ms`, `close_time_ms`;
- `open`, `high`, `low`, `close`, `volume` как `Decimal`.

Snapshot создаётся defensive conversion после cache commit. Один handler не может изменить данные события для cache или последующих handlers.

## 5. Минимальный набор событий

### 5.1 Cache change events

#### `CandleInserted`

- Поля: общий envelope и `candle: CandleSnapshot`.
- Причина: `NEW_CANDLE` или `RECOVERY_BACKFILL`.
- Previous candle: не требуется.
- Создание: для каждого `INSERTED` в подробном cache mutation result.
- Дедупликация: producer не создаёт повтор для `UNCHANGED`; stable dedupe key включает тип, stream key, `open_time_ms` и fingerprint snapshot.

#### `CandleUpdated`

- Поля: envelope, `previous: CandleSnapshot`, `current: CandleSnapshot`.
- Причина: `OPEN_CANDLE_REFRESH`.
- Previous candle: обязательна для объяснимости и вычисления delta без дополнительного cache read.
- Создание: только для подтверждённого `UPDATED` последней незакрытой свечи.
- Дедупликация: key включает тип, stream key, `open_time_ms` и fingerprint новой версии. Exact duplicate не создаёт update event.

#### `CandleClosed`

- Поля: envelope и `candle: CandleSnapshot`.
- Причина: `TIME_CONFIRMED`, `NEXT_CANDLE_OBSERVED` или `RESUME_RECONCILIATION`.
- Previous candle: не требуется; закрывается сохранённая версия snapshot.
- Создание: когда Event Projector впервые подтверждает `observed_at_ms > close_time_ms` для свечи, ранее считавшейся открытой.
- Дедупликация: stable identity `(CandleClosed, stream_key, open_time_ms)` и in-memory closure watermark на поток.

#### `CandleEvicted`

- Поля: envelope и `candle: CandleSnapshot`.
- Причина: `CAPACITY_LIMIT`.
- Previous candle: не требуется, вытесненная свеча и есть previous state.
- Создание: для каждой явно удалённой cache mutation detail.
- Дедупликация: key включает cycle ID, stream key и `open_time_ms`, поскольку после явной повторной загрузки одна и та же свеча теоретически может быть вытеснена снова.

### 5.2 Ingestion diagnostic events

#### `MarketGapDetected`

- Поля: envelope, ожидаемый и полученный `open_time_ms`, последний подтверждённый `open_time_ms`, phase (`REGULAR` или `RECOVERY`).
- Candle snapshot: не обязателен; событие описывает границу gap, а не новый state.
- Причина: `INTERVAL_GAP`, `OVERLAP` или `RECOVERY_DISCONNECTED`.
- Создание: при типизированном gap до recovery и при неудачной recovery.
- Дедупликация: cycle ID + phase + временная граница.

#### `MarketStreamFailed`

- Поля: envelope, error category, безопасный error code, terminal flag и consecutive failure count.
- Candle snapshot: не требуется.
- Причина: `PROVIDER`, `VALIDATION`, `CACHE_INTEGRITY`, `GAP_RECOVERY` или `DELIVERY`.
- Создание: при failed polling cycle; terminal flag отражает переход engine в `FAILED`.
- Дедупликация: cycle ID + error category + terminal flag.

Другие события в M1.7 не добавляются: для них пока нет явного потребителя или уникальной семантики.

## 6. Порядок cache events

События формируются из подробного результата одного атомарного cache commit в детерминированном порядке входных изменений.

Для каждой свечи:

1. Сначала публикуется `CandleClosed` для ранее открытой предыдущей свечи, если новая свеча подтверждает её закрытие.
2. Затем публикуется `CandleInserted` или `CandleUpdated` текущей свечи.
3. После mutation events публикуются связанные `CandleEvicted` в порядке фактического удаления.

Exact duplicate не создаёт `Inserted` или `Updated`, но успешная idempotent reconciliation может создать ранее не объявленный `CandleClosed`.

## 7. Точная семантика CandleClosed

### Основное правило

Свеча считается закрытой при `observed_at_ms > close_time_ms`. `CandleClosed` создаётся ровно один раз для `(stream_key, open_time_ms)` в пределах lifecycle одного Event Projector.

Projector хранит для каждого потока in-memory `last_announced_closed_open_time_ms`. Строгий порядок свечей позволяет использовать один watermark вместо растущего набора IDs. Свеча с `open_time_ms` не выше watermark не может создать повторный `CandleClosed`, даже если была вытеснена и позднее появилась при backfill. Watermark относится к delivery projection, а не к Market Cache, поэтому cache не получает event-логику.

### Previous open, затем exact duplicate

Если предыдущий cycle сохранил свечу при `observed_at_ms <= close_time_ms`, а следующий успешный cache upsert вернул `UNCHANGED` при `observed_at_ms > close_time_ms`, projector создаёт `CandleClosed(reason=TIME_CONFIRMED)`. Изменение OHLCV для этого не требуется.

### Появилась следующая свеча

Перед `CandleInserted` новой свечи projector проверяет предыдущую. Если её закрытие ещё не объявлено и новая свеча открылась после её `close_time_ms`, сначала создаётся `CandleClosed(reason=NEXT_CANDLE_OBSERVED)`.

### Engine был остановлен во время закрытия

После возобновления первый успешный cycle выполняет reconciliation сохранённого cache snapshot с текущим `observed_at_ms`. Все свечи новее closure watermark, которые теперь закрыты, получают `CandleClosed(reason=RESUME_RECONCILIATION)` до событий новых изменений.

### Bootstrap

Исторические свечи, впервые появившиеся при начальном bootstrap уже закрытыми, регистрируются как baseline closed без генерации ретроспективного `CandleClosed`. Это предотвращает event storm. Незакрытая последняя bootstrap-свеча отслеживается и позднее создаёт одно событие закрытия.

### Граница гарантии

Однократность гарантируется в пределах lifecycle одного процесса и Event Projector. После process crash closure watermark теряется. Глобальная exactly-once delivery невозможна без durable event log и consumer offsets; они откладываются до persistence/audit.

## 8. Event Projector

Projector получает:

- `MarketStreamKey`;
- подробный cache mutation result;
- immutable post-commit cache snapshot;
- `observed_at_ms`;
- cycle context с ID и source;
- признак bootstrap/resume/recovery.

Он возвращает ordered immutable tuple `MarketEvent`. Projector не публикует события самостоятельно и не меняет cache.

Projector отвечает за:

- преобразование mutation details в cache events;
- per-stream closure watermark и семантику `CandleClosed`;
- event IDs через injected factory;
- deterministic ordering и producer-side deduplication.

## 9. Необходимые подробности Market Cache result

Текущих counters и tuple `CandleUpsertResult` недостаточно: они не связывают результат с конкретной свечой и не содержат previous/evicted state.

Для M1.7 предлагается аддитивно расширить `BatchUpsertResult` полем:

```text
changes: tuple[MarketCacheChange, ...]
```

`MarketCacheChange` содержит:

- stream key;
- result: `INSERTED`, `UPDATED` или `UNCHANGED`;
- `previous: Candle | None` как defensive copy;
- `current: Candle` как defensive copy;
- `evicted: tuple[Candle, ...]` из defensive copies;
- порядковый index внутри batch.

Существующие `results` и `evicted_count` сохраняются для обратной совместимости и `PollCycleResult`. Возвращённые `Candle` не раскрывают внутренние ссылки cache, а Event Projector немедленно преобразует их в immutable `CandleSnapshot`. Таким образом, Market Cache не зависит от event-моделей. Single `upsert_candle()` может продолжать возвращать `CandleUpsertResult`; M1.7 ingestion использует атомарный batch API.

Cache не создаёт `MarketEvent`: он только сообщает факты commit. Event Projector преобразует их после успешного возврата операции.

## 10. Взаимодействие с PollCycleResult

События нельзя восстанавливать по агрегированным полям `inserted/updated/unchanged/evicted`. Polling Engine использует `BatchUpsertResult.changes`, передаёт их projector и публикует ordered events.

`PollCycleResult` предлагается аддитивно расширить:

- `cycle_id`;
- `published_event_count`;
- `delivery_failure_count`;
- optional `delivery_result` или его безопасное summary.

Ошибка handler не превращает успешный cache commit в failed ingestion и не запускает повторный provider request. Cycle помечается как `SUCCESS_WITH_DELIVERY_ERRORS` либо сохраняет `SUCCESS` с отдельной delivery category; точный вариант остаётся вопросом implementation review. Consecutive ingestion failure counter не увеличивается.

## 11. Минимальный Event Dispatcher contract

Рекомендуемый API:

- `subscribe(event_type, handler) -> SubscriptionToken`;
- `unsubscribe(token) -> bool`;
- `publish(event) -> EventDeliveryResult`.

### Subscription

- Разрешено несколько handlers одного event type.
- Handler базового `MarketEvent` получает все market events.
- Handler конкретного типа получает экземпляры этого типа.
- Общий порядок вызова соответствует порядку успешных `subscribe`, независимо от уровня базового/конкретного типа.
- Token immutable и уникален в пределах dispatcher.
- `unsubscribe` идемпотентен: возвращает `True` только при фактическом удалении.

### Publish

- Перед доставкой dispatcher фиксирует snapshot подходящих subscriptions.
- Изменения подписок во время publish влияют только на следующую публикацию.
- Один и тот же event object логически доставляется всем handlers, но его поля и вложенные snapshots immutable.
- Recursive `publish` запрещён в M1.7 типизированной ошибкой, чтобы не создавать неочевидный порядок и бесконечную рекурсию.
- Dispatcher не выполняет deduplication: producer/Event Projector отвечает за уникальность событий.

### Thread safety and lifecycle

Dispatcher не thread-safe и рассчитан на один синхронный поток. Он не создаёт threads/tasks и не имеет внешних ресурсов, поэтому `close()` не нужен. Composition root владеет dispatcher и передаёт его ingestion orchestration.

## 12. Handler error policy

Утверждаем **collect-and-continue**:

1. Dispatcher вызывает каждый handler из subscription snapshot.
2. Исключение handler перехватывается и сохраняется как `EventHandlerDeliveryError` с event ID, subscription token и безопасным handler identity.
3. Остальные handlers обязательно вызываются.
4. После завершения возвращается immutable `EventDeliveryResult` с attempted, succeeded и typed failures.
5. Уже подтверждённый cache commit не откатывается.
6. Polling Engine логирует/метризует delivery failure и отражает его в cycle result, но не повторяет ingestion cycle автоматически.

Fail-fast отвергнут, потому что один handler скрыл бы события от остальных. Isolate-and-log без aggregate result отвергнут, потому что Polling Engine не смог бы классифицировать деградацию доставки.

## 13. Типизированные ошибки и результаты

Минимальные типы:

- `MarketEventError` — базовая ошибка event layer;
- `EventSubscriptionError` — неверный event type или handler;
- `EventSubscriptionTokenError` — невалидный token, если token пришёл из другого dispatcher;
- `RecursiveEventPublishError` — запрещённый nested publish;
- `EventHandlerDeliveryError` — один handler завершился исключением;
- `EventDeliveryResult` — immutable aggregate результата publish.

`EventDeliveryResult` содержит event ID, attempted count, succeeded count и ordered tuple failures. Handler failures представлены как данные результата, а не выбрасываются после доставки.

## 14. Дедупликация

- Dispatcher не хранит глобальный список event IDs.
- Cache mutation events создаются только из одного успешного commit result.
- Exact duplicate не создаёт mutation event.
- `CandleUpdated` dedupe использует fingerprint новой immutable версии.
- `CandleClosed` использует per-stream closure watermark и stable candle identity.
- Diagnostic events используют cycle-scoped identity.
- Повторный вызов `publish()` с тем же событием повторно вызовет handlers; caller не должен публиковать один event дважды.

Durable dedupe между рестартами откладывается до persistent event log.

## 15. Diagnostic events and Polling Engine

Polling Engine создаёт diagnostic events напрямую через отдельную factory после того, как ошибка классифицирована:

- `MarketGapDetected` — перед recovery и при recovery failure;
- `MarketStreamFailed` — после failed cycle или terminal transition.

Diagnostic event не требует успешного cache commit, но явно маркируется ingestion category и не смешивается с cache change events.

Ошибка доставки diagnostic event также собирается dispatcher и логируется; она не запускает recursive `MarketStreamFailed` о самой себе.

## 16. Reproducible tests

Тесты M1.7 не используют сеть, реальное время или случайные IDs:

- deterministic event ID factory;
- явные `occurred_at_ms` и cycle IDs;
- реальный InMemoryMarketCache и Event Projector;
- handlers, записывающие порядок вызовов;
- handlers, изменяющие subscriptions во время publish;
- handler, выбрасывающий исключение между двумя успешными handlers;
- попытка recursive publish;
- exact duplicate после границы закрытия;
- next-candle closure;
- stop/resume reconciliation;
- recovery, eviction и failed cache batch.

Обязательно проверяется, что handler не может изменить cache или payload следующего handler.

## 17. Открытые вопросы

1. Сохранить `PollCycleOutcome.SUCCESS` с delivery summary или добавить `SUCCESS_WITH_DELIVERY_ERRORS`.
2. Нужен ли общий handler базового `MarketEvent` в минимальной реализации или его можно отложить; рекомендация — поддержать сразу.
3. Должен ли initial bootstrap создавать `CandleInserted` для всей истории или одно агрегированное bootstrap event; минимальный proposal оставляет per-candle inserts.
4. Достаточен ли per-stream closure watermark для будущего replay с произвольным seek; для строго последовательного M1.7 он достаточен, replay seek потребует отдельной session identity.
5. Нужно ли включать полный immutable snapshot в `CandleEvicted` или достаточно identity; рекомендация — полный snapshot для analytics/debugging.
6. Должен ли terminal delivery failure влиять на health status Polling Engine или иметь отдельный event-delivery health indicator.
7. Нужен ли bounded publisher-level dedupe cache до появления persistence.
8. Как event consumers выполняют первоначальный bootstrap своего состояния: читают Market Cache перед subscribe или обрабатывают исторические `CandleInserted`.

## 18. Рекомендуемая минимальная реализация M1.7

- Immutable event envelope и `CandleSnapshot`.
- Шесть событий: четыре cache change и два diagnostic.
- Аддитивный `MarketCacheChange` в `BatchUpsertResult`.
- Stateful in-memory Event Projector с bounded per-stream closure watermark.
- Один synchronous in-process Event Dispatcher.
- API `subscribe`, `unsubscribe`, `publish`.
- Snapshot subscriptions, deterministic handler order, recursive publish forbidden.
- Collect-and-continue с immutable aggregate delivery result.
- Интеграция в Polling Engine только после успешного cache commit; diagnostic path отдельно.
- Никаких `asyncio`, broker, persistence, strategy, execution или live-компонентов.
