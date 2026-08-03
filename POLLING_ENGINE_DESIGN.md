# REST Polling Engine Design Proposal

## 1. Статус и область

- Статус: proposed for M1.6 implementation review.
- Дата: 2026-08-03.
- Архитектурное основание: ADR-017.

Документ определяет первый долгоживущий market ingestion процесс:

```text
MarketProvider -> Candle Validation -> Market Cache
```

Первая версия ограничена Binance Spot, `BTCUSDT`, одним interval из конфигурации, одним синхронным процессом и одним writer. В неё не входят WebSocket, `asyncio`, стратегии, execution, database persistence и live trading.

## 2. Ответственность и границы

Polling Engine отвечает за:

- расписание REST-опросов;
- bootstrap и последующие запросы свечей;
- передачу явного времени наблюдения в Validation и Market Cache;
- классификацию provider, validation и cache errors;
- ограниченные retries/backoff временных provider errors;
- попытку восстановления gap через повторный backfill;
- кооперативный graceful stop;
- operational logging, status и метрики ingestion.

Polling Engine не отвечает за:

- преобразование сырого Binance payload — это обязанность адаптера;
- правила качества свечей — это обязанность Validation;
- хранение и read semantics — это обязанность Market Cache;
- индикаторы, сигналы, торговые решения, risk и execution;
- persistence и восстановление состояния после рестарта;
- управление реальным капиталом.

Стратегии и другие потребители не вызывают Polling Engine напрямую. Их единственный read path рыночных данных — Market Cache.

## 3. Рекомендуемый публичный контракт

Минимальный blocking API:

- `run_once() -> PollCycleResult` — выполнить один ingestion cycle, включая допустимые retries, но без ожидания следующего cadence;
- `run_forever() -> None` — выполнить bootstrap и блокирующий цикл до stop или fatal error;
- `request_stop() -> None` — идемпотентно запросить кооперативную остановку;
- `status -> PollingStatus` — immutable snapshot текущего состояния.

Отдельный `start()` не рекомендуется в M1.6: он подразумевает владение thread/task lifecycle, которого в однопроцессной синхронной версии нет. Composition root непосредственно вызывает блокирующий `run_forever()`.

`run_once()` нужен как самостоятельная операция для детерминированных unit/integration tests и ручной диагностики. Он не закрывает provider и не запускает стратегию.

### PollCycleResult

Типизированный результат одного цикла должен минимально содержать:

- stream key;
- observed time;
- число полученных и проверенных свечей;
- `BatchUpsertResult` Market Cache;
- число provider attempts;
- продолжительность цикла по monotonic clock;
- признак bootstrap/recovery/regular cycle.

Ошибки не маскируются под успешный результат.

## 4. Зависимости конструктора

Минимальные зависимости:

- `MarketProvider`;
- `InMemoryMarketCache` или минимальный cache port;
- один immutable `MarketStreamKey` (`BTCUSDT` + interval из настроек);
- callable существующей candle sequence validation;
- `Clock` с wall time в миллисекундах и monotonic time для расписания;
- stop-aware `Sleeper`;
- immutable `PollingPolicy`;
- logger или logging facade.

Provider и cache передаются через dependency injection. Engine не создаёт Binance adapter и не выбирает торговый инструмент самостоятельно.

### Clock and sleeper

Абстракции обязательны для M1.6:

- wall clock предоставляет `now_ms()` для Validation и Market Cache;
- monotonic clock используется для cadence, duration и backoff;
- sleeper ожидает до deadline и может немедленно завершиться после `request_stop()`.

Это исключает реальные sleep и системное время из unit-тестов. Доменные компоненты продолжают получать время только явно.

## 5. Lifecycle

### `run_once()`

1. Проверяет, что stop не запрошен и engine не находится в несовместимом состоянии.
2. Определяет regular, bootstrap или recovery request.
3. Получает свечи через `MarketProvider`.
4. Фиксирует единый `observed_at_ms` после получения ответа.
5. Проверяет последовательность существующим Validation с разрешённой незакрытой последней свечой.
6. Атомарно передаёт batch в Market Cache с тем же `observed_at_ms`.
7. Возвращает `PollCycleResult` и обновляет status/metrics.

### `run_forever()`

1. Переходит из `STOPPED` в `STARTING`.
2. Выполняет bootstrap cycle.
3. После успеха переходит в `RUNNING`.
4. Вычисляет следующий monotonic deadline.
5. Ожидает deadline через stop-aware sleeper.
6. Выполняет regular cycle и повторяет цикл.
7. При stop request переходит в `STOPPING`, завершает текущую безопасную операцию и затем в `STOPPED`.
8. При fatal error переходит в `FAILED` и возвращает управление composition root через типизированное исключение.

### `request_stop()`

Операция идемпотентна. Она устанавливает stop flag и прерывает только ожидание sleeper. Уже начатый синхронный HTTP request не отменяется; его ограничивает timeout provider. После ответа engine не начинает новый цикл.

## 6. State machine

Предлагаемые состояния:

```text
STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED
              |          |
              |          +-> BACKING_OFF -> RUNNING
              |                    |
              +--------------------+-> FAILED
RUNNING ------------------------------> FAILED
BACKING_OFF -- stop request ----------> STOPPING
```

### Обоснование состояний

- `STOPPED` — однозначное начальное и чистое конечное состояние; цикл не выполняется.
- `STARTING` — bootstrap отличается размером запроса и может завершиться до появления полноценного cache.
- `RUNNING` — bootstrap завершён, выполняется regular cadence.
- `BACKING_OFF` — engine жив, но временно не опрашивает provider из-за retry policy; состояние важно для health/metrics.
- `STOPPING` — stop принят, новые циклы запрещены, завершается текущая синхронная операция.
- `FAILED` — обнаружена невосстановимая ошибка; автоматическое продолжение небезопасно.

Все состояния необходимы для различения operational health. Убирать `STARTING` или `BACKING_OFF` не рекомендуется: внешне одинаковое «не обновляет cache» имеет принципиально разные причины.

Минимальный `PollingStatus` должен быть immutable и содержать state, stream key, время последнего успешного цикла, время последней попытки, число последовательных provider failures и краткую классификацию последней ошибки без секретов или полного payload.

## 7. Polling cadence and drift

Cadence задаётся `PollingPolicy` и не выводится неявно из длительности свечи. Рекомендуемое начальное значение для `1m` — **5 секунд**, чтобы обновлять текущую незакрытую свечу. Значение должно быть положительным и конфигурируемым.

Расписание строится по monotonic deadlines:

1. После bootstrap фиксируется `next_deadline = monotonic_now + cadence`.
2. После каждого scheduled tick выполняется `next_deadline += cadence`, а не `now + cadence`.
3. Если запрос завершился после одного или нескольких deadlines, пропущенные deadlines перескакиваются до первого будущего значения.
4. Engine не выполняет burst из нескольких запросов, пытаясь «догнать» пропущенные ticks.

Так длительность HTTP-запросов не накапливает drift, а корректировки wall clock не ломают расписание.

## 8. Bootstrap and regular requests

### Первый запуск

Рекомендуемый bootstrap limit — `min(cache.capacity, provider_max_limit)`, то есть **1000** при стандартной конфигурации. Полученный batch полностью валидируется до атомарного upsert.

Если provider вернул меньше свечей, это допустимо при непрерывной и корректной последовательности. Пустой ответ не считается успешным bootstrap.

### Последующие циклы

Regular request получает последние **2 свечи**:

- последнюю закрытую или закрывающуюся свечу для идемпотентной сверки;
- текущую незакрытую свечу для insert/update.

Exact duplicate даёт `UNCHANGED`. Незакрытая последняя свеча обновляется по правилам Market Cache. После перехода к следующему interval предыдущая свеча становится immutable.

## 9. Незакрытая свеча

Validation вызывается с разрешённой незакрытой последней свечой. Один `observed_at_ms`, полученный после provider response, используется и Validation, и cache.

Engine не меняет OHLCV и не определяет самостоятельно правила update. Cache решает, является ли свеча открытой, и применяет monotonic update constraints. Конфликт закрытой свечи не исправляется polling-слоем автоматически.

## 10. Failure recovery and retry policy

### Классификация ошибок

Ошибки сохраняют границы происхождения:

- **Provider errors** — timeout, connection, HTTP status, invalid external response;
- **Validation errors** — некорректные значения, порядок, gap, overlap или interval;
- **Cache errors** — conflict, forbidden update, out-of-order, gap или invalid batch;
- **Lifecycle/configuration errors** — неверная policy или незаконный вызов API.

Логи и метрики всегда содержат категорию. Engine может оборачивать ошибку в operation-level exception, но обязан сохранять исходную причину через exception chaining.

### Retryable provider errors

Временными считаются timeout, connection errors, HTTP `408`, `429` и `5xx`. Рекомендуемая policy одного цикла:

- максимум 3 provider attempts, включая первый;
- exponential delays: 1, 2 секунды;
- общий cap одного backoff — 30 секунд;
- без jitter в первой версии для воспроизводимости;
- каждый wait прерывается stop request.

После исчерпания attempts цикл считается неуспешным. `run_forever()` сохраняет cache без изменений, увеличивает consecutive failure count и планирует следующий recovery cycle с capped exponential backoff: 5, 10, 20, 30 секунд.

Успешный цикл сбрасывает consecutive failure count и возвращает regular cadence.

### HTTP 429

Если provider error предоставляет корректный `Retry-After`, используется `min(Retry-After, 30 seconds)`. Иначе применяется обычный exponential delay. Для реализации потребуется сохранить status code и безопасную retry metadata в типизированной provider error; polling не должен разбирать текст исключения.

HTTP `4xx`, кроме `408` и `429`, по умолчанию fatal: вероятны неверный symbol, interval или request configuration.

## 11. Gap recovery

Gap не заполняется синтетическими свечами и не игнорируется.

1. Regular batch, обнаруживший gap, не изменяет cache.
2. Engine переключает следующий запрос в recovery mode.
3. Recovery запрашивает расширенное окно до `min(cache.capacity, provider_max_limit)` последних свечей.
4. Весь recovery batch проверяется Validation и атомарно применяется к cache.
5. Если последовательность всё ещё не соединяется с доступной историей cache, engine переходит в `FAILED` с отдельной gap classification.

M1.6 не очищает cache и не выполняет скрытый reset для преодоления gap. Политика полного re-bootstrap после несовместимой истории является открытым вопросом.

## 12. Cache conflicts

- `UNCHANGED`, `INSERTED` и допустимый `UPDATED` являются нормальными результатами.
- Conflicting duplicate, forbidden closed-candle update и invalid open-candle update являются data-integrity errors.
- Такие ошибки не retry-ятся тем же payload и переводят engine в `FAILED`.
- Полный контекст stream key, open time и error category логируется без записи сырого payload.

Engine не изменяет свечу, чтобы заставить cache принять её.

## 13. Graceful shutdown and resource ownership

`request_stop()` должен завершать backoff/cadence wait немедленно. Текущий HTTP request ограничен provider timeout; после его завершения новый request не начинается. Cache остаётся в последнем атомарно согласованном состоянии.

Composition root владеет `MarketProvider` и закрывает его в `finally` после выхода из `run_forever()`. Polling Engine не вызывает `provider.close()`, поскольку зависимость передана извне и может использоваться для диагностики до окончательного shutdown.

В первой версии один engine обслуживает ровно один `MarketStreamKey`. Один engine на несколько streams не допускается: это потребует политики fairness, независимого backoff и нового lifecycle design.

## 14. Logging and metrics

### Structured logging

Минимальные поля:

- engine state;
- symbol и interval;
- cycle type: bootstrap, regular или recovery;
- attempt number;
- requested/received/inserted/updated/unchanged/evicted counts;
- observed time и cycle duration;
- error category и безопасный error code;
- consecutive failure count и выбранный backoff.

API keys и сырой response payload не логируются.

### Metrics

Минимальные counters/gauges/histograms:

- poll attempts и successful cycles;
- provider/validation/cache errors по категории;
- retry и HTTP 429 counts;
- cycle duration и provider latency;
- candles received/inserted/updated/unchanged/evicted;
- current consecutive failures;
- age последней успешно обработанной свечи;
- current engine state и backoff duration.

Конкретная metrics backend не входит в M1.6; достаточно типизированного metrics port или тестируемого no-op implementation.

## 15. Reproducible testing

Unit-тесты не используют сеть, `sleep()` или реальные часы:

- fake `MarketProvider` возвращает сценарии или выбрасывает типизированные ошибки;
- fake wall/monotonic clock управляется тестом;
- fake stop-aware sleeper записывает deadlines и продвигает monotonic time;
- реальный Validation проверяет подготовленные свечи;
- реальный InMemoryMarketCache проверяет итоговое состояние;
- fake logger/metrics собирают наблюдаемые события;
- stop request инициируется fake sleeper на заданном ожидании.

Нужно проверить bootstrap, regular update, no drift, skipped deadlines, retry exhaustion, `Retry-After`, gap recovery, cache conflict, stop во время cadence и backoff, fatal transition и неизменность cache после каждого failed cycle.

## 16. Типизированные результаты и ошибки

Предлагаемая минимальная operation-level иерархия:

- `PollingEngineError` — базовая ошибка orchestration;
- `PollingEngineConfigurationError` — неверная policy или stream configuration;
- `PollingEngineStateError` — незаконный lifecycle call;
- `PollingProviderError` — исчерпаны retries provider;
- `PollingValidationError` — batch отклонён Validation;
- `PollingCacheError` — cache отклонил проверенный batch;
- `PollingGapRecoveryError` — recovery не восстановил непрерывность.

Каждая ошибка сохраняет исходную типизированную причину. Provider error contract при реализации должен различать status code и retry metadata без анализа строк сообщений.

## 17. Открытые вопросы

1. Нужен ли отдельный cache port уже в M1.6 или достаточно прямой зависимости от `InMemoryMarketCache` до появления второй реализации.
2. Какой cadence утвердить для interval, отличных от `1m`; текущая рекомендация 5 секунд относится к первому `BTCUSDT/1m` потоку.
3. Считать ли пустой regular response retryable provider/data error и сколько последовательных пустых ответов допустимо.
4. Разрешать ли автоматический полный re-bootstrap при gap, который невозможно соединить с текущим cache.
5. Сколько последовательных исчерпанных retry cycles переводят engine из `BACKING_OFF` в `FAILED`.
6. Нужен ли jitter перед эксплуатацией нескольких экземпляров, чтобы избежать synchronized polling.
7. Как представить metrics port до выбора observability backend.
8. Должен ли `run_once()` менять глобальный state или возвращать только результат при вызове из `STOPPED`.
9. Нужен ли отдельный health status для stale data помимо state machine.

## 18. Рекомендуемая минимальная реализация M1.6

- Один blocking `SynchronousRestPollingEngine` для одного `MarketStreamKey`.
- API: `run_once`, `run_forever`, `request_stop`, immutable `status`.
- Immutable `PollingPolicy` с cadence, bootstrap/regular limits, retry attempts и caps.
- Injected provider, cache, clock, sleeper, validator, logger и no-op metrics port.
- Bootstrap limit 1000, regular limit 2.
- Monotonic deadline scheduling без accumulated drift.
- Три provider attempts с delays 1 и 2 секунды; отдельный capped recovery backoff.
- Один атомарный cache batch на успешный cycle.
- Fatal stop на validation/cache integrity errors после одной контролируемой gap recovery.
- Composition root закрывает provider в `finally`.
- Полностью детерминированные unit-тесты без сети и реального времени.
