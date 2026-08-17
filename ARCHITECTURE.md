# Chappie Trader Architecture

## MVP server contract

The active continuous Paper/Testnet MVP treats development execution state as
disposable. Its normal Testnet path uses exchange schema **v4** (durable order
lifecycle, reconciliation, authoritative settlement, and managed Portfolio).
Migrations v5–v10 support historical M4.10/Stage 4 experiments and are not
required by normal MVP startup.

Unexpected Testnet execution failures enter `SAFE_STOP`: no later market event
may submit an order in that process, and an operator must investigate or reset
the disposable database before a fresh-process restart. LIVE remains
structurally unavailable. Operational details are in
[`SERVER_MVP_RUNBOOK.md`](docs/operations/SERVER_MVP_RUNBOOK.md).

Historical M4.10/Stage 4 authority, recovery, disposition, and successor flows
are retained below as archive/audit material. They are not imported by the
normal continuous MVP composition and are not an operator recovery procedure.

## Design Principles

Canonical active design references are in [`docs/core/`](docs/README.md):
market system, trading decisions, decision runtime, Paper trading, Risk,
persistence, and exchange execution.

Chappie Trader развивается как финансовая подсистема будущей платформы Chappie. В документации и коммуникации система называется **Trading Agent** или **Autonomous Trading System**: её назначение включает анализ, управление риском, исполнение, учёт, аудит и восстановление состояния.

Архитектурные и продуктовые решения принимаются согласно следующим принципам:

1. **Безопасность важнее прибыльности.** Защита капитала, ограничение риска и предсказуемое поведение имеют приоритет над потенциальной доходностью.
2. **AI никогда не отправляет ордера напрямую.** Любое созданное AI торговое намерение проходит через типизированный доменный контракт, детерминированный Risk Manager и контролируемый execution-контур.
3. **Каждое решение объяснимо, воспроизводимо и имеет audit trail.** Должны быть доступны использованные входные данные, конфигурация, версия стратегии или модели, применённые проверки и итоговый результат.
4. **Бизнес-логика не зависит от конкретной биржи и инфраструктуры.** Стратегии, риск и правила портфеля не импортируют Binance API, HTTP-клиент, файловую систему или конкретную базу данных.
5. **Один pipeline используется во всех режимах.** Backtest, paper, testnet и live должны различаться адаптерами данных и исполнения, а не основными бизнес-правилами.
6. **Live trading запрещён по умолчанию.** Допуск к реальному капиталу возможен только после прохождения backtest, paper, testnet и отдельного эксплуатационного review.
7. **Простота предпочтительнее преждевременной распределённости.** Проект остаётся модульным монолитом, пока подтверждённые требования не потребуют иного.

## 1. Статус документа

- Статус: approved.
- Дата утверждения: 2026-08-03.
- Описываемая версия проекта: `0.1.0`.
- Назначение: зафиксировать текущую архитектуру, целевые границы системы и правила дальнейшего развития.
- Этот документ не означает, что описанные целевые компоненты уже реализованы.

## 2. Назначение проекта

Chappie Trader — финансовая подсистема будущей платформы Chappie и модульный Trading Agent, предназначенный для получения рыночных данных, формирования торговых решений, проверки риска, симуляции и исполнения сделок, ведения состояния портфеля и оценки результатов.

Ближайшая цель проекта — построить безопасный и воспроизводимый контур:

```text
Market Data -> Strategy -> Order Intent -> Risk -> Execution -> Portfolio
      |                                                        |
      +---------------- Analytics / Audit <--------------------+
```

AI-компоненты могут быть добавлены позднее как источник сигналов, классификатор рыночного режима или аналитический модуль. Они не должны обходить детерминированные ограничения риска и контур исполнения.

## 3. Текущее состояние

На текущем этапе проект является ранним модульным каркасом. Реализованы:

- загрузка настроек из окружения и `.env`;
- логирование в консоль и файл;
- синхронный клиент публичного REST API Binance;
- получение серверного времени и текущей цены;
- получение и преобразование Binance OHLCV-свечей;
- независимая проверка качества свечей и их последовательностей;
- bounded in-memory Market Cache с атомарным batch upsert и defensive snapshots;
- типизированные модели рыночных данных;
- внедрение рыночного провайдера в `TraderAgent`;
- базовое управление запуском и остановкой агента;
- один unit-тест жизненного цикла агента.

Пока не реализованы:

- постоянный цикл обработки рынка;
- Polling Engine и долгоживущий ingestion loop;
- торговые стратегии;
- риск-менеджмент;
- paper/live execution;
- позиции, портфель и PnL;
- хранение состояния;
- backtesting;
- AI/ML/LLM-компоненты.

Фактический запуск сейчас выполняет разовую проверку соединения с рынком: получает время и цену, после чего процесс завершает работу.

## 4. Текущая структура репозитория

```text
app/
  agent/          Координация жизненного цикла агента
  core/           Настройки и логирование
  database/       Зарезервировано для персистентности
  exchange_execution/ Infrastructure-neutral TESTNET order domain и ports
  execution/      Детерминированное paper execution
  market/         Модели, контракт и Binance-адаптер
  risk/           Зарезервировано для риск-менеджмента
  strategies/     Зарезервировано для торговых стратегий
  main.py         Точка сборки приложения
tests/            Автоматические тесты
data/             Локальные данные проекта
logs/             Локальные журналы работы
```

## 5. Текущие компоненты

### 5.1 Application entry point

`app/main.py` является composition root. Он загружает конфигурацию, настраивает логирование, создаёт конкретный рыночный адаптер, внедряет его в агента и освобождает ресурсы при завершении.

Точка входа должна оставаться местом сборки зависимостей. Доменные компоненты не должны самостоятельно создавать Binance-клиенты, подключения к базе данных или другие инфраструктурные зависимости.

### 5.2 Configuration

`app/core/config.py` использует Pydantic Settings и содержит параметры приложения, журналирования, режима торговли, биржи, символа и интервала.

Текущее ограничение: значения `trading_mode`, `exchange_name` и `default_interval` пока представлены произвольными строками и требуют более строгой валидации.

### 5.3 Logging

`app/core/logger.py` настраивает стандартный Python logging с выводом в консоль и файл.

В дальнейшем журналирование должно поддерживать структурированные события и позволять проследить путь торгового решения от рыночных данных до исполнения.

### 5.4 Market domain and provider port

`app/market/models.py` содержит модели `ServerTime`, `TickerPrice` и `Candle`. Цены и объёмы представлены через `Decimal`, временные значения преобразуются в timezone-aware UTC.

`app/market/provider.py` определяет структурный порт `MarketProvider` с операциями:

- `get_server_time()`;
- `get_ticker_price()`;
- `get_candles()`;
- `close()`.

Контракт позволяет заменять Binance на fake, replay или другой биржевой адаптер без изменения агента.

### 5.5 Binance adapter

`app/market/binance_provider.py` реализует синхронный HTTP-адаптер с обработкой сетевых, HTTP- и JSON-ошибок и преобразует Binance OHLCV в нормализованные модели `Candle`.

### 5.6 Trader agent

`app/agent/trader.py` сейчас выполняет роль простого application coordinator. Он выводит параметры запуска, запрашивает время и текущую цену и хранит признак `is_running`.

По мере развития агент не должен превращаться в единый класс со всей логикой. Его задача — координировать специализированные компоненты и управлять жизненным циклом процесса.

## 6. Архитектурный стиль

Целевой стиль — модульный монолит с портами и адаптерами.

- Доменная логика не зависит от Binance, HTTP, файлов или конкретной базы данных.
- Внешние системы подключаются через явно определённые интерфейсы.
- Paper trading, backtesting и live trading используют общие доменные модели и максимально общий pipeline.
- Компоненты остаются в одном развёртываемом приложении, пока реальные требования не оправдают распределённую архитектуру.

Микросервисное разделение на текущем этапе не планируется.

## 7. Целевая модульная схема

Целевой read path рыночных данных:

```text
MarketProvider
      |
      v
 Polling Engine
      |
      v
  Validation
      |
      v
 Market Cache
      |
      v
 Market Events
      |
      +------> CandleClosed -> Shadow Trading Decision Engine
      |                       -> Snapshot Factory
      |                       -> Trading Decision Pipeline
      |                       -> Shadow Decision Observer
      |
      +------> Indicators
      +------> Strategies
      +------> Analytics
      +------> AI
```

Ни один внутренний потребитель за пределами market ingestion не обращается напрямую к `MarketProvider`.

```text
                         +-------------------+
                         | Configuration     |
                         +---------+---------+
                                   |
+---------------+       +---------v---------+       +----------------+
| Market Adapter+------>| Market Data       +------>| Strategy       |
| REST/WS/Replay|       | Pipeline          |       | Engine         |
+---------------+       +-------------------+       +--------+-------+
                                                               |
                                                      +--------v-------+
                                                      | Order Intent   |
                                                      +--------+-------+
                                                               |
                                                      +--------v-------+
                                                      | Risk Manager   |
                                                      +--------+-------+
                                                               |
                                                      +--------v-------+
                                                      | Execution Port |
                                                      +---+---------+--+
                                                          |         |
                                                    +-----v--+  +---v------+
                                                    | Paper  |  | Exchange |
                                                    +-----+--+  +---+------+
                                                          |         |
                                                      +---v---------v---+
                                                      | Portfolio       |
                                                      | and Accounting  |
                                                      +--------+--------+
                                                               |
                                  +----------------------------v--------+
                                  | Persistence / Audit / Analytics     |
                                  +-------------------------------------+
```

## 8. Целевые границы модулей

### `market`

Отвечает за получение, нормализацию и проверку рыночных данных. Не содержит торговых решений.

Внутри market-модуля обязанности разделены следующим образом:

- `MarketProvider` получает внешние данные и преобразует payload в нормализованные модели;
- ingestion управляет вызовами provider и передаёт данные на проверку;
- validation проверяет значения, порядок, непрерывность и временные диапазоны;
- Market Cache ограниченно хранит уже нормализованные и проверенные данные и предоставляет единый read interface внутренним потребителям.

#### Market Cache

Market Cache хранит public candle data во внутреннем lifecycle entry. Переход
`OPEN -> CLOSED_CONFIRMED` коммитится атомарно с возможным финальным OHLCV update;
после подтверждения отличающиеся версии запрещены. Lifecycle metadata не выходит
через public snapshots, но additive mutation details передаются Event Projector.

Market Cache является единственным внутренним источником рыночных данных для индикаторов, стратегий, risk, analytics и будущих AI-модулей. Backtest, paper, testnet и live используют одинаковый интерфейс чтения; различается только ingestion source.

Market Cache отвечает за:

- адресацию потоков по ключу `symbol + interval`;
- ограниченное in-memory хранение свечей каждого потока;
- детерминированную вставку, идемпотентные повторы и обновление текущей незакрытой свечи;
- отклонение конфликтующих, out-of-order и нарушающих локальную последовательность записей;
- чтение последних `N` свечей и последней закрытой свечи;
- защиту внутреннего состояния от изменения вызывающей стороной.

Market Cache не отвечает за:

- сеть, REST, WebSocket, retries, rate limits или polling;
- преобразование payload конкретной биржи;
- торговые сигналы, индикаторы, решения или risk rules;
- database persistence и восстановление после рестарта;
- особенности Binance или другого провайдера.

Детальные market-cache инварианты и исторические вопросы собраны в
`docs/core/MARKET_SYSTEM.md`.

#### Polling Engine

Polling Engine — application/infrastructure orchestration component первого market ingestion процесса. Он связывает инфраструктурный порт, независимую проверку данных и внутреннее хранилище:

```text
MarketProvider -> Candle Validation -> Market Cache
```

Polling Engine:

- получает нормализованные свечи через `MarketProvider`;
- передаёт их существующему validation-компоненту до изменения cache;
- записывает только успешно проверенные данные в Market Cache;
- управляет cadence, retries/backoff и кооперативной остановкой;
- является единственным writer Market Cache в первой синхронной версии;
- передаёт явное время наблюдения в validation и cache;
- различает provider, validation и cache errors в логах, метриках и результате цикла.

Polling Engine не содержит торговой логики, индикаторов, стратегии, risk или execution. Стратегии и другие внутренние потребители не вызывают его и не используют `MarketProvider` напрямую: они читают состояние только через Market Cache.

`MarketProvider` создаётся и закрывается composition root. Polling Engine использует внедрённый provider, но не владеет его ресурсом и не закрывает его самостоятельно. Детальный lifecycle, state machine и retry policy собраны в `docs/core/MARKET_SYSTEM.md`.

#### Market Events

Market Cache является источником текущего нормализованного состояния. Market Events не заменяют cache и не используются для чтения истории: они сообщают внутренним потребителям о подтверждённых изменениях этого состояния.

Cache-событие создаётся только после успешной атомарной записи или успешной идемпотентной reconciliation, подтвердившей закрытие свечи. Failed validation, rejected cache batch и rollback не создают cache-событий. Диагностические ingestion-события имеют отдельную категорию и не утверждают, что cache изменился.

Event Dispatcher синхронно доставляет immutable события в порядке подтверждённых изменений. Он:

- не содержит торговых решений;
- не предоставляет изменяемые ссылки на внутренние `Candle` cache;
- не зависит от Binance, HTTP, persistence или execution;
- вызывает все подходящие handlers и возвращает aggregate delivery result;
- не откатывает cache при ошибке handler;
- не вызывается стратегиями для получения данных: стратегии продолжают читать Market Cache.

Polling Engine после успешного cache commit передаёт подробный mutation result в Market Event Projector, а затем публикует созданные события через dispatcher. Счётчиков `PollCycleResult` и `BatchUpsertResult` недостаточно для восстановления событий: ingestion должен получить типизированные сведения о конкретных вставленных, обновлённых и вытесненных свечах.

Минимальный event model, точная семантика `CandleClosed` и delivery policy собраны в `docs/core/MARKET_SYSTEM.md`.

#### Shadow Trading Decision Engine

M2.6 реализует этот компонент отдельным пакетом `app.shadow`; подключение в
composition root условно и по умолчанию выключено.

Первое runtime-подключение торгового pipeline выполняется только в shadow mode и
требует feature flag. После подтверждённого cache commit целевой поток имеет вид:

```text
CandleClosed
    -> ShadowTradingDecisionEngine
    -> MarketCacheReader
    -> MarketSnapshotFactory
    -> TradingDecisionPipeline
    -> ShadowDecisionRecord
    -> ShadowDecisionObserver
```

Shadow Engine является application service. Он владеет только собственной
подпиской на `CandleClosed` и bounded in-memory dedup state. Он читает defensive
snapshot Market Cache через структурный read port, использует event market time и
correlation для создания `MarketSnapshot`, вызывает один Trading Decision Pipeline
и передаёт immutable record observer-у.

Engine не изменяет Market Cache, не находится внутри Polling Engine, dispatcher,
Strategy или Intent Planner и не выполняет risk, portfolio mutation, execution,
paper/live orders, private Binance API или persistence. Его ожидаемые и неожиданные
`Exception` изолируются от market ingestion; `BaseException` не поглощается.
Composition root создаёт и связывает engine, cache reader, pipeline, factories,
dispatcher и observer, но engine не закрывает эти зависимости.

Первая версия использует synchronous in-process delivery, один thread, один engine
на Strategy/Pipeline и logging observer. Bootstrap history не replay-ится: первое
shadow decision появляется только после нового `CandleClosed`. Детальный lifecycle,
consistency checks, dedup и error policy описаны в
[`Decision runtime`](docs/core/DECISION_RUNTIME.md).

#### Shared Decision Runtime Coordination

M3.10 проектирует `TradingDecisionCoordinator` как application/runtime orchestration
boundary и единственного будущего decision-path subscriber на `CandleClosed`:

```text
CandleClosed
    -> TradingDecisionCoordinator
    -> committed Market Cache snapshot
    -> one MarketSnapshot
    -> TradingDecisionPipeline.evaluate() exactly once
    -> one DecisionPipelineResult
        -> optional ShadowDecisionConsumer
        -> optional PaperTradingEngine
```

Coordinator не является частью trading domain: он не содержит Strategy, Intent,
Risk, Execution или Portfolio formulas. Он владеет только своей subscription,
bounded process-local event dedup и operational status; dispatcher, cache, pipeline,
consumers и resources принадлежат composition root.

При включённых shadow и paper оба consumer получают те же immutable object instances.
Обычная ошибка одного consumer не препятствует другому и не останавливает ingestion.
Если PaperTradingEngine уже `DEGRADED`, paper path не вызывается, но shared decision и
shadow analysis продолжаются. Текущий ShadowTradingDecisionEngine не должен быть
одновременно подписан с coordinator; migration выделяет consumer без cache read и
pipeline evaluation. Полный контракт описан в [`Decision runtime`](docs/core/DECISION_RUNTIME.md).

M3.11 реализует этот flow в `app.coordination`. Application composition создаёт
coordinator только если включён shadow или paper, подписывает только его как decision
handler и передаёт один shared result в consumers. Paper-only mode создаёт единые
InstrumentRules для Portfolio, RiskManager и PaperExecutionProvider; никакие private
Binance endpoints не используются. Implementation и final safety review завершены
2026-08-08; ADR-043–047 приняты.

### `strategies`

Преобразует рыночный контекст в типизированный сигнал или намерение. Не отправляет ордера и не меняет портфель напрямую.

### `risk`

Принимает `OrderIntent`, immutable market context и Portfolio snapshot, после чего
разрешает, ограничивает или отклоняет намерение. Этот этап обязателен для любого
paper, testnet и live execution. Execution получает только параметры, разрешённые
Risk Manager, а не исходный intent напрямую.

Risk Manager является synchronous pure domain service: он не читает Market Cache,
не вызывает Portfolio aggregate, не создаёт Order/Fill и не использует I/O или
системное время. Application layer передаёт ему согласованные immutable
`OrderIntent`, `PortfolioSnapshot` и `MarketSnapshot`; результатом является только
immutable `RiskDecision`.

M4.4 добавляет второй pure слой `app.risk.exchange`, не изменяя Stage 3 `RiskManager`.
Он принимает готовый `RiskDecision`, согласованные `PortfolioSnapshot`, reconciled
account state, `PendingExposure`, `ExecutionSession` и immutable exchange policy.
Financial assessment отдельно от submission authorization: обычное превышение лимита
возвращает ordered typed reasons, а не exception. Submission safety дополнительно
требует TESTNET, cleared kill switch, SAFE reconciliation, совпадающие generations,
реальный process-local `SubmissionCapability` и safe pending control. Этот слой также
не выполняет I/O, не читает clock/SQLite и не создаёт order command.
M4.4 final safety review пройден: aggregation остаётся математически полной для
нескольких immutable records, а conservative one-order/one-inflight запрет применяется
после aggregation; mixed-side remaining quantities передаются явно. Submission сверяет
также generation самого kill switch и отклоняет cross-session assessment.

M4.5 добавляет infrastructure-only `app.infrastructure.binance` adapter для Binance
Spot Testnet. Он реализует существующие exchange gateway/account-reader ports через
injected `httpx.Client`, timestamp provider и read-only durable identity resolver.
Signing, private payload normalization и error mapping не проникают в domain/Risk/
Portfolio/Persistence. Adapter принимает только `https://testnet.binance.vision`, не
делает retries или sleeps, не владеет injected client и не подключён к runtime. M4.5
final adapter safety review пройден 2026-08-11: redirect forwarding запрещён, signed
representation совпадает с передаваемыми bytes, ambiguous submit не повторяется, а
adapter остаётся пригоден для read reconciliation. Client proxy/TLS lifecycle принадлежит
composition; synchronous adapter не заявляет thread safety.

M4.6 добавляет отдельные schema-v2 `SQLiteExchangePersistence` и
`DurableExchangeOrderLifecycle` без runtime composition. PREPARED/event commit предшествует
любому gateway side effect; submission attempt, SUBMITTING projection/event и session counters
атомарны. После SUBMITTING restart всегда требует reconciliation. Append-only exchange trades
сохраняются независимо от Portfolio settlement, а durable kill switch стартует ACTIVE и меняет
monotonic control generation только с reason/audit identity.

M4.6 final crash/restart safety review пройден 2026-08-11. Fresh durable state является
единственным арбитром после SQLite commit ambiguity; process exception не разрешает retry.
Attempt ordinal ограничен единицей, replay ACK/result idempotent, а root/event/projection/session
corruption блокирует local recovery. M4.7 остаётся владельцем external reconciliation.

M4.7 реализует `ExchangeReconciler` над `ExchangeLifecyclePersistence` и read-only
`ExchangeAccountReader`, без concrete adapter, Portfolio/Risk runtime или scheduler. Truthful
order/trade facts фиксируются отдельно; schema-v3 SAFE run и generation `N -> N+1` атомарны.
REQUIRED/FAILED/CONFLICT сохраняют generation `N`. Local recovery и external reconciliation —
независимые состояния. Kill switch остаётся ACTIVE, capability не выдаётся, а trade settlement
остаётся PENDING до M4.8.

`SAFE reconciliation` здесь означает только полный проверенный exchange-truth snapshot в
заявленном history horizon. Он не равен Portfolio settlement, submission readiness, cleared kill
switch, Risk approval или capability. M4.7 result всегда сообщает `submission_ready = false`.
M4.8 обязан завершить settlement и заново связать reconciliation/control generations перед
выдачей process-local capability. Account-wide trade horizon является явным контрактом reader;
неполное bounded окно блокирует SAFE.
M4.7 final reconciliation safety review пройден 2026-08-11; M4.8 остаётся следующим milestone,
а Stage 4 — in progress.

```text
OrderIntent + PortfolioSnapshot + MarketSnapshot
    -> Risk Manager
    -> APPROVED | MODIFIED | REJECTED RiskDecision
    -> future Execution
```

Target exposure преобразуется в target position через Portfolio equity и latest
closed reference price, затем в delta order с применением InstrumentRules. Полный
контракт, порядок gates, dust и fee boundary описаны в
[`Risk`](docs/core/RISK.md).

### `execution`

Преобразует разрешённое RiskDecision намерение в `Order`/`Fill`. Первый adapter —
детерминированный synchronous paper execution без сети; он не изменяет Portfolio.
Testnet и live adapters требуют отдельных design review и допуска.

Первая версия получает только готовый `RiskDecision`, immutable execution config и
`InstrumentRules`; она не читает Market Cache и не знает Risk Policy. `APPROVED` и
`MODIFIED` исполняются сразу одним существующим immutable `Fill` по reference price,
с нулевым slippage и explicit Decimal fee. Время fill выводится из decision timestamp
и configured deterministic latency без clock, sleep или random. Подробный контракт
описан в [`Paper trading`](docs/core/PAPER_TRADING.md).

M4.1–M4.2 сохраняют paper provider без изменений и утверждают отдельный TESTNET contract:
stateful `ExchangeExecutionCoordinator` работает через side-effect
`ExchangeOrderGateway` и read-only `ExchangeAccountReader`. Order command/client ID
commit-ятся до submission; acknowledgement не считается Fill; только durable
exchange-reported trades создают Portfolio transitions. Один order допускает несколько
partial Fill. Binance signing, errors и payloads остаются infrastructure-only.

M4.3 реализует эту границу в `app/exchange_execution` без network/persistence/runtime
wiring. Пакет содержит immutable command/order/trade/event models, approved lifecycle,
transport ambiguity и query-result types, deterministic `ctt1t-` client identity,
trade/pending-exposure aggregation, settlement classification, durable session/kill-
switch models, opaque process-local submission capability и runtime-checkable submit/
account-reader ports. Ни один port не создаёт durable identity; LIVE не представлен как
поддерживаемая execution environment.
M4.3 final domain safety review подтвердил: ACK не создаёт Fill/Trade, ambiguous
transport всегда ведёт к reconciliation, exact duplicate trade идемпотентен, conflicting
duplicate fail-closed, partial-fill reservation хранит original/executed/remaining, а
submission требует реальный non-serializable generation-bound capability вместе со
всеми control/Risk gates.

### `portfolio`

Является единственным авторитетным источником balances, позиции, cost basis,
комиссий, realised/unrealised PnL и экспозиции. Он атомарно применяет подтверждённые
fills, но не содержит стратегию, risk policy или execution logic.

### `database`

Содержит адаптеры хранения состояния, событий и результатов экспериментов. Доменная логика не должна зависеть от конкретной СУБД.

### `backtesting`

Целевой модуль, которого пока нет. Управляет replay исторических данных и симуляцией времени, используя те же стратегии, риск и учёт портфеля.

### `agent`

Координирует pipeline и жизненный цикл. Не дублирует расчёты стратегии, риска, исполнения или портфеля.

### `paper`

Application-level paper orchestration module. Получает готовые
`DecisionPipelineResult + MarketSnapshot`, оценивает принадлежащий ему Portfolio на
каждой закрытой свече, затем координирует обязательный Risk, Paper Execution и Fill
apply. Он не содержит Strategy, sizing, fee или accounting formulas, не читает Market
Cache и не знает Binance. Контракт описан в [`Paper trading`](docs/core/PAPER_TRADING.md).

PaperTradingEngine является синхронным single-writer application service без
start/stop lifecycle, background thread или сети. Composition root владеет
Portfolio, RiskManager, PaperExecutionProvider, durable Paper store, factories и
observer; engine владеет bounded process-local dedup и operational status, а durable
commit предшествует синхронизации in-memory Portfolio. После неоднозначной ошибки
на границе execution/Portfolio экземпляр становится degraded и не продолжает работу
до внешней проверки и пересоздания.

## 9. Базовые доменные сущности

Перед реализацией торгового контура необходимо утвердить минимальный словарь:

- `MarketEvent` или `MarketSnapshot` — нормализованное состояние рынка;
- `Signal` — результат анализа стратегии;
- `OrderIntent` — желаемое торговое действие до проверки риска;
- `RiskDecision` — разрешение, ограничение или отклонение;
- `Order` — запрос на исполнение с устойчивым идентификатором;
- `Fill` — подтверждённое исполнение;
- `Position` — состояние по инструменту;
- `Portfolio` — агрегированное состояние капитала и экспозиции;
- `PerformanceSnapshot` — показатели результата в определённый момент.

Точные Stage 3 поля и границы paper-контура описаны в
[`Paper trading`](docs/core/PAPER_TRADING.md) и
[`Risk`](docs/core/RISK.md).
Portfolio, Risk и Paper Execution реализованы;
финальная архитектура orchestration утверждена в M3.8, а synchronous in-memory
PaperTradingEngine реализован и прошёл final safety review в M3.9 без runtime wiring.

## 10. Основной поток данных

1. Market ingestion получает данные через `MarketProvider` биржи или replay-источника.
2. Polling Engine координирует получение и передаёт явное время наблюдения.
3. Provider преобразует внешний payload в нормализованные модели.
4. Validation проверяет значения и последовательность данных.
5. Проверенные данные атомарно помещаются в Market Cache.
6. После успешного commit Market Event Projector создаёт события подтверждённых изменений.
7. Event Dispatcher синхронно доставляет события подписанным внутренним модулям.
8. При включённом decision runtime `CandleClosed` передаётся единственному `TradingDecisionCoordinator`, который проверяет committed Market Cache state и строит один `MarketSnapshot`.
9. Coordinator ровно один раз вызывает `TradingDecisionPipeline`; Strategy формирует immutable `Signal`, а deterministic Intent Planner формирует максимум один `OrderIntent`.
10. Один `DecisionPipelineResult` передаётся теми же object instances в независимо включённые shadow и paper consumers.
11. ShadowDecisionConsumer формирует immutable record и передаёт его observer-у без risk и execution.
12. PaperTradingEngine согласует Portfolio valuation с тем же MarketSnapshot и передаёт `OrderIntent`, immutable Portfolio snapshot и MarketSnapshot обязательному Risk Manager.
13. Execution симулирует только параметры, разрешённые `RiskDecision`, и создаёт immutable `Order`/`Fill`.
14. Portfolio атомарно и идемпотентно применяет Fill; execution не меняет его напрямую.
15. PaperTradingEngine по утверждённому M3.8 контракту формирует единый immutable cycle result и compact
    observer record; HOLD и declined cycles обновляют только valuation.
16. M3.13 реализовал persistence core, а M3.14 подключил boundary к paper runtime:
    decision claim, valuation, risk authorization, execution, Fill и Portfolio
    transition записываются в одной authoritative transaction до memory synchronization.

Для paper-only dust liquidation Portfolio допускает исключение из minimum quantity/
notional только для SELL всей текущей позиции с явным immutable Fill marker. Это не
ослабляет quantity step, price tick, gross/fee reconciliation или запрет short.

Ни одна стратегия, включая AI-стратегию, не должна напрямую обращаться к live execution.

## 11. Режимы работы

Предполагаются четыре режима:

- `backtest` — replay исторических данных и симуляция исполнения;
- `paper` — текущий рынок с локальной симуляцией исполнения;
- `testnet` — реальные API-вызовы в тестовой среде биржи;
- `live` — реальное исполнение с капиталом.

Переход между режимами должен менять инфраструктурные адаптеры, но не бизнес-правила
стратегии и риска. PAPER и TESTNET являются взаимоисключающими financial consumers;
shadow может сопровождать любой из них.

Initial Stage 4 implementation должна unconditionally отклонять LIVE до создания
private client. Будущий live-режим потребует отдельной build/config capability,
credential namespace, endpoint allowlist, явного подтверждения и live-readiness review;
одного ordinary mode flag недостаточно.

## 12. Надёжность и безопасность

Обязательные инварианты целевой системы:

- каждое намерение проходит через Risk Manager;
- денежные расчёты используют `Decimal` с правилами округления инструмента;
- ордера получают идемпотентные идентификаторы;
- повторная доставка события не приводит к повторной сделке;
- состояние восстанавливается и сверяется с биржей после перезапуска;
- устаревшие или некорректные рыночные данные не используются для торговли;
- секреты не записываются в логи и не хранятся в репозитории;
- предусмотрен kill switch;
- лимиты позиции, дневного убытка и просадки применяются независимо от стратегии;
- каждое решение имеет audit trail с версией стратегии и конфигурации.

### Durable paper persistence boundary

M3.12 design, M3.13 persistence core и M3.14 runtime integration задают поток:

```text
CandleClosed
-> TradingDecisionCoordinator
-> one DecisionPipelineResult
-> Paper Cycle Unit of Work
     -> deterministic Risk / Paper Execution / Portfolio transition
     -> append-only Trading Journal
     -> verified Portfolio Checkpoint
-> COMMIT
-> observer
```

Journal является authoritative source для финансовых mutations; checkpoint — только
проверяемая recovery optimization. Durable stable decision key строится независимо от
runtime UUID и включает fingerprint фактического ordered Strategy input window,
strategy/planner versions и config fingerprints. Отдельный command key включает
approved Risk, policy/rules и execution configuration. Decision claim, command, Fill,
valuation, Portfolio transition/current projection и terminal cycle commit-ятся в
одной application-level transaction. Domain, Risk, Portfolio, Strategy и Paper
Execution не зависят от SQLite.

Реализованный `app.persistence` разделён на SQLite-independent canonical/models/keys/
ports/recovery core и единственный SQLite adapter с embedded checksummed migration.
Schema v1 запрещает financial `REAL`, обеспечивает stable decision/slot/command/Fill/
Portfolio-version uniqueness, append-only journal и materialized `portfolio_current`.
`BEGIN IMMEDIATE`, WAL, FULL synchronous mode и foreign keys задают single-writer
financial boundary. Recovery возвращает только CLEAN, RECOVERED или fail-closed
RECOVERY_REQUIRED. Application восстанавливает Portfolio и только затем подписывает
coordinator; paper runtime без persistence запрещён конфигурацией.

M3.13 final safety review подтвердил explicit Portfolio genesis root (version 0 +
configuration fingerprint), full root-to-current journal validation, no-op terminal
cycles без fake version increment и несколько gap-free effective transitions в одной
cycle transaction. Materialized `portfolio_current` может быть read-only reconstructed
при отсутствии, но существующее disagreement не ремонтируется автоматически.
Checkpoint остаётся только optimization и не позволяет пропустить corrupt historical
journal. SQLite connection lifecycle детерминирован; backup запрещён во время active
financial transaction и использует online backup с явно закрываемым destination.

Portfolio не зависит от persistence и предоставляет additive restore/preview/committed
transition API. Preview не мутирует aggregate. Durable store зависит только от
`TradingPersistence` port, строит restart-stable decision/command keys и атомарно
фиксирует terminal cycle. COMMIT предшествует in-memory swap и observer. Commit failure
не меняет memory; post-commit sync failure блокирует paper path как `DEGRADED`.

После restart bounded coordinator/paper/fill caches остаются только fast-path. Startup
обязан открыть storage, проверить schema/integrity, восстановить Portfolio через
checkpoint + journal replay и завершить reconciliation до подписки coordinator.
Неоднозначное или повреждённое financial state приводит к `RECOVERY_REQUIRED`; fallback
к пустому Portfolio и автоматическое «исправление» денег запрещены. Observer вызывается
после business commit и не может инициировать повторный Fill. Подробный контракт:
[`docs/core/PERSISTENCE.md`](docs/core/PERSISTENCE.md); historical ADR-048–054
material is archived with the Stage 4 evidence.

## 13. Тестовая стратегия

Тестирование должно строиться слоями:

- unit-тесты доменных моделей, стратегий и правил риска;
- контрактные тесты всех реализаций портов;
- тесты HTTP-адаптеров на имитированных ответах;
- интеграционные тесты paper execution и persistence;
- воспроизводимые backtest-сценарии;
- ограниченные smoke-тесты Binance testnet;
- длительный shadow/paper run до допуска live-режима.

Тесты не должны зависеть от публичного Binance API по умолчанию.

## 14. Наблюдаемость

Минимально необходимы:

- структурированные журналы;
- идентификатор торгового решения, проходящий через весь pipeline;
- latency и error rate рыночного провайдера;
- возраст последних рыночных данных;
- текущая позиция и экспозиция;
- realised/unrealised PnL;
- отклонения Risk Manager;
- состояние соединений и event loop;
- алерты по потере данных, превышению риска и рассинхронизации состояния.

## 15. Правила зависимостей

Целевое направление зависимостей:

```text
Infrastructure adapters -> Application services -> Domain
```

- Домен не импортирует `httpx`, Binance SDK, Pydantic Settings или конкретную БД.
- Адаптеры преобразуют внешние форматы в доменные модели на границе системы.
- `main.py` выбирает и связывает конкретные реализации.
- Импорты между стратегией, риском и execution не должны создавать циклических зависимостей.
- Общие модели выделяются по смыслу, а не в универсальный модуль `utils`.

## 16. Архитектурные решения

Для первых этапов развития утверждены следующие ограничения:

1. Первый рынок — **Binance Spot**.
2. Первый инструмент эксплуатации — **BTCUSDT**. Поддержка нескольких инструментов не входит в начальный контур.
3. Первый постоянный market loop использует **синхронный REST polling**.
4. Переход на `asyncio` и WebSocket отложен до отдельного ADR, основанного на измеримых требованиях к задержке, пропускной способности и числу инструментов.
5. Первая baseline-стратегия — простая детерминированная **EMA trend strategy с фильтром волатильности**. Точные правила сигнала, периоды EMA, метрика волатильности и пороговые значения определяются и утверждаются на Stage 2.
6. Источник исторических данных — **Binance OHLCV**.
7. Начальное хранилище состояния, audit trail и результатов экспериментов — **SQLite** за абстрактным persistence port.
8. Допуск к testnet и live определяется отдельными критериями качества, достаточным количеством наблюдений и отсутствием критических ошибок. Календарная продолжительность сама по себе не является критерием допуска.
9. Live trading остаётся долгосрочной целью, но требует отдельного review и явного допуска.
10. Первая роль AI ограничена аналитикой, объяснением и классификацией рыночного режима в **shadow mode**. AI не влияет на execution и не отправляет ордера.

На последующих design review остаётся определить внутреннее размещение доменных сущностей, точные правила baseline-стратегии, формат локального набора OHLCV, модель симулированного времени, схему SQLite и численные пороги качества. Эти детали не меняют утверждённые архитектурные направления.

M3.15 завершил crash/restart review durable paper boundary. Financial cycle имеет только
rollback-before-COMMIT или authoritative-commit-after-COMMIT; post-commit memory sync
атомарен для полного transition batch. Новый process восстанавливает Portfolio из root +
journal/checkpoint и suppress-ит duplicate Risk/Execution через stable durable identity.
M3.16 завершил Stage 3 controlled two-run public-data restart smoke на одной SQLite DB:
recovery восстановил exact durable head, новые shared shadow/paper HOLD decisions
продвинули Portfolio/journal `0 -> 1 -> 2`, оба shutdown завершились с exit code 0.
Real BUY/SELL в smoke не происходил; executable restart behavior и duplicate-Fill
защита подтверждены deterministic automated suites. Private exchange execution
отсутствует.

M4.1 design и M4.2 final safety review отделяют exchange execution от immediate-fill paper semantics. Первый
target — только Binance Spot Testnet: отдельный stateful order coordinator сохраняет
Risk-approved command и deterministic client order ID до network side effect, а
exchange-reported partial trades применяются к Portfolio отдельными durable,
idempotent transitions. Ambiguous submission запрещает blind retry и требует query по
stable client ID. Startup reconciliation и durable kill switch gate-ят новые
submissions. PAPER, TESTNET и LIVE являются разными capabilities; initial Stage 4
unconditionally отвергает LIVE до отдельного live-readiness review. Transport outcome
не является exchange truth; после ambiguous submit automatic resubmit запрещён, а
отсутствие strong proof-of-absence требует fail-closed operator resolution. Initial
scope — MARKET BUY/SELL `BTCUSDT`, base quantity, один non-terminal order, без cancel.
Полный утверждённый M4.1/Stage 4 design и ADR-055–ADR-062 —
[исторический материал Stage 4](docs/historical/stage4/README.md). Текущий
непрерывный Testnet MVP contract находится в
[Exchange execution](docs/core/EXCHANGE_EXECUTION.md).

## 17. Future Chappie Integration

В долгосрочной архитектуре Chappie Trader не является всей платформой Chappie и не должен поглощать её общие обязанности. Он входит в финансовый домен как специализированная автономная торговая система:

```text
Chappie
  -> Finance
       -> Chappie Trader
```

- **Chappie** координирует возможности платформы и пользовательское взаимодействие.
- **Finance** объединяет финансовые функции, политики доступа и агрегированное финансовое состояние.
- **Chappie Trader** отвечает за рыночные данные, стратегии, торговый риск, исполнение, позиции и аудит торговых решений.

Будущая интеграция должна выполняться через явные application ports и события. Платформа сможет запрашивать состояние, аналитику и контролируемые операции, но не должна обходить внутренний Risk Manager или напрямую управлять биржевым адаптером.

## M4.8 runtime Testnet integration

M4.8 is completed; final safety review passed on 2026-08-12. Schema v4 adds an
append-only exchange-trade settlement journal and a durable managed-Portfolio head. Startup is
strictly ordered: local recovery, read-only exchange reconciliation, quote-fee settlement,
current control reload, then readiness evaluation. Portfolio memory is synchronized only after
the atomic SQLite commit. Any non-quote commission remains authoritative exchange truth but
blocks settlement and new authority.

Submission authority is opaque and process-local, and binds TESTNET environment, account,
session, reconciliation generation and control generation. Any failed gate produces no
capability. LIVE remains structurally rejected. M4.9 is completed and its final crash/restart
safety review passed; M4.10 Phase 1 and Phase 2 PASS, while Phase 3 schema v5 and its final
adversarial review are complete but execution is unauthorized; Stage 4 remains in progress.

The M4.8 completion pass removes the raw public `submit_prepared(key)` gateway path. A synchronous
final gate rereads order, session, reconciliation/control generations, kill switch, settlement,
pending orders/attempts, rules, Risk context and authorization freshness before issuing one
opaque, single-use authorization for the exact durable/client identity. The coordinator delivers
its existing snapshot and decision objects to the Testnet consumer after shadow/paper processing;
it does not subscribe separately or reevaluate Strategy. Recovered PREPARED commands are never
submitted by startup and require a fresh explicit gate invocation.
Production Testnet composition is dormant in paper/shadow modes. In explicit Testnet mode the
single shared coordinator delivers the same `MarketSnapshot` and `DecisionPipelineResult` objects
to a concrete application service. That service performs financial Risk, exchange Risk, stable
durable identity, PREPARED persistence and the final pre-submit gate without rerunning Strategy or
Pipeline. Durable reconciliation coverage, not merely SAFE status, gates chronological settlement.
The durable order store uniquely owns each decision-slot key: runtime UUID changes are ignored for
economic identity, while corrected historical candle input fails closed as a slot conflict.

Production startup distinguishes runtime availability from submission readiness. After successful
local recovery, a blocked Testnet startup remains available as a recovery-only graph containing
persistence, managed Portfolio, private read adapter, reconciler and settlement. It contains no
submission capability, application service or Testnet consumer; Testnet-only startup therefore has
no decision coordinator. A shadow consumer may continue on the shared coordinator without Testnet
delivery. Kill-switch, reconciliation and non-terminal-order blockers are recoverable states;
corrupt/incompatible durable state remains fatal. Recovery-only never promotes in-process: a fresh
startup must rerun recovery, reconciliation, settlement and control checks before minting a new
process-local capability. This contract is recorded by accepted ADR-063. M4.9
is completed and its final crash/restart safety review passed; M4.10 Phase 1 authenticated
read-only and Phase 2 signed order-test PASS; Phase 3 schema v5 and its final adversarial review are
complete but execution is unauthorized, and Stage 4 remains in progress.

## M4.9 crash/restart torture review

M4.9 is completed and its final crash/restart safety review passed. It introduces
no exchange feature or new authority. File-backed, socket-guarded production graphs prove that
pre-COMMIT submission failures roll back atomically, while every post-COMMIT uncertainty remains
`SUBMITTING`/`RECONCILIATION_REQUIRED` and can only query the immutable client order ID. Repeated
NOT_FOUND, later FOUND, process dedup eviction and new runtime UUIDs cannot create another durable
command, attempt or gateway call. Settlement chronology, exact-once financial application,
unsupported-fee blocking, writer/WAL recovery and process-local capability invariants remain
green. Detailed evidence is in `M4_9_CRASH_RESTART_REPORT.md`; Stage 4 remains in progress.

## M4.10 controlled Testnet smoke authorization

M4.10 security preflight is completed, its final safety review passed, and Phase 1 authenticated
read-only evidence passed. The dedicated `python -m app.tools.testnet_phase2` harness completed with
PASS after exactly one signed test-endpoint POST and no financial semantics.
`M4_10_TESTNET_SMOKE_PLAN.md` separates
offline, authenticated-read, `/order/test`, one-order and restart phases; each requires independent
operator approval. Central logging suppresses private transport tracing even under global DEBUG;
the adapter's isolated order-test method has no durable or financial semantics. The separate
`app.tools.testnet_phase1` one-shot harness resolves the Phase 0 read-harness gap: its narrow facade
exposes four reads, its transport permits only the fixed Testnet host and GET whitelist under a
five-request ceiling, and it imports neither persistence nor trader, submission, order-test or
capability paths. It writes sanitized evidence only and does not inspect or change the kill switch.
Audited kill-switch and one-order commands remain prerequisites for later phases. No further
credential use, network request or order is currently authorized. Stage 4 remains in progress.
The Phase 1 run made exactly four fixed-host GETs, observed zero open orders/trades, normalized
BTCUSDT rules and BTC/USDT balances, and produced no durable or financial mutation. Its consumed
authorization grants no POST authority.
The Phase 2 process requires `M4_10_PHASE2_AUTHORIZED=YES`; its transport permits exactly one
fixed-host `POST /api/v3/order/test` and rejects every other method, path, authority and any second
request before transport. A narrow facade exposes only `test_market_order`; the graph imports no
persistence, Portfolio, durable lifecycle, reconciliation, settlement or normal runtime. The
reviewed Phase 2-only BUY quantity `0.00016000` BTC is checked with `Decimal` against the observed
price `63849.22000000`, `stepSize=0.00001000`, `minQty=0.00001000` and `minNotional=5.00000000`.
Phase 2 created no order, trade, Fill, durable lifecycle or balance mutation. Phase 3 execution
remains blocked and unauthorized while its operator implementation is absent. The
pre-existing Testnet 1 BTC cannot be silently imported into the zero-base managed Portfolio.

The Phase 3 final design review accepts ADR-064. Its managed subledger starts at zero BTC with an
explicit `25 USDT` allocation; the exchange's pre-existing
BTC remains external and cannot satisfy SELL Risk or settlement. A new durable one-shot authority
is inserted and consumed in the same transaction that creates the sole PREPARED command. The global
kill switch remains ACTIVE; the committed transaction returns only an in-memory claim that can mint
an exact-authority/command capability for the existing final gate. Crash loses that capability, so
restart remains ACTIVE/recovery-only and preserves the same command for reconciliation without
first-submit eligibility. Schema-v5 authority implementation and offline crash proofs are complete
and the final adversarial safety review passed. The isolated one-shot operator harness is complete
and its final offline operator safety review passed, while Phase 3 execution authorization remains pending.

Phase 3 subsequently reached ACK with its sole durable identity; ACK remains non-settlement evidence.
Phase 4 is a distinct fresh-process, same-database GET-only recovery graph. Its harness and authorized
production reconciliation run completed safely. It derives
the sole durable identity, validates normalized order/trade and external-balance facts, and reuses
exactly-once settlement.
It has no submission capability or POST path; authorization did not carry into remediation or proof.

ADR-065 schema v6 and its final offline safety review are complete. The separate
`app.tools.testnet_phase4_remediate_zero_fee` composition is persistence-only: it imports no Binance or
HTTP component, reads no credentials, fixes the production database/evidence paths, and requires
`M4_10_PHASE4_REMEDIATION_AUTHORIZED=YES` before writable database acquisition. It validates the exact
known durable Phase 3/4 shape and delegates the only mutation to the append-only exactly-once settlement
service. The harness passed final offline safety review and the separately authorized remediation
completed with globally synchronized durable truth. The final
`app.tools.testnet_m410_recovery_proof` composition is observation-only: it uses immutable read-only
SQLite, reconstructs every durable digest and identity from the fixed database, enforces unchanged
before/after checksum and creates exclusive sanitized evidence only after closing the database. The
separately authorized production proof passed, so M4.10 is COMPLETED — PASS. M4.11 final
aggregation completed with `STAGE_4_BLOCKED`: all evaluated safety/recovery criteria pass, but the
accepted controlled Testnet BUY-and-SELL criterion still lacks a persisted SELL trade. The next
managed-balance SELL evidence is still absent and unauthorized; Stage 5 has not started.
The fresh-process proof harness final offline adversarial safety review passed.

The offline SELL-closure schema/domain/persistence boundary is implemented and does not change that verdict. It keeps
raw exchange BTC outside Risk availability, selects only an exact full managed-position exit under
fresh MARKET rules, and uses an additive schema-v7 authority with an ACTIVE-switch, process-local
single-use claim. Schema-v5 BUY authority is not reused or weakened. ADR-066 was accepted after final
design review; its offline implementation, tests and final adversarial schema review are complete.
The one-shot operator is also implemented offline: exact authorization precedes credential/DB/transport
access, two bounded five-GET snapshots surround authority creation, and the existing gate commits
SUBMITTING before the sole allowed POST. Its final offline adversarial review passed with exact
durable-BUY/exchange-history provenance and atomic PASS-last evidence after owned resources close.
No production migration, network action, SELL execution or Stage 5 work is authorized. See
`STAGE_4_MANAGED_SELL_CLOSURE_DESIGN.md`.

Schema v8 implements the offline SELL recovery/disposition boundary for the observed PREPARED,
zero-attempt state. It stores one canonical sanitized recovery-required event and at most one final
disposition event under append-only triggers. This boundary does not contact Binance and does not
grant a recovery submission claim. The existing SELL identity is immutable, normal operator retry
remains forbidden, and production schema-v8 migration/recovery execution require separate approval.

ADR-067 is accepted and ADR-068's offline continuation implementation defines a same-identity operation
only after audited exchange-absence review and complete durable revalidation. Its proposed recovery
claim is process-local and non-reconstructable; the schema-v8 decision event is never a capability.
Unknown exchange state selects disposition. See `STAGE_4_SELL_RECOVERY_DISPOSITION_DESIGN.md`.

ADR-069 implements the alternative terminal local path for the observed unsubmitted SELL: an exact
schema-v9, append-only disposition can transition only the authority-bound, zero-attempt `PREPARED`
order to `DISPOSED_NO_EXECUTION`. It preserves the authority/command/client/slot and
Portfolio/session lineage, releases only the local terminal reservation projection, and has no exchange,
balance, fee, turnover, PnL, settlement, or submission-counter effect. The distinct disposition
authorization is offline-only; production disposition and any later newly-authorized SELL remain blocked.
