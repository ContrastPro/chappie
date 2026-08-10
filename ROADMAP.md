# Chappie Trader Roadmap

## 1. Статус документа

- Статус: approved.
- Дата утверждения: 2026-08-03.
- Отправная точка: версия `0.1.0`, ранний инфраструктурный каркас.
- Принцип: следующий этап начинается после выполнения критериев готовности предыдущего.
- Даты намеренно не назначены до оценки объёма работ и утверждения архитектуры.

## 2. Цель дорожной карты

Последовательно превратить текущий health-check Binance в безопасного, тестируемого и наблюдаемого торгового агента. Приоритет — воспроизводимость и контроль риска, а не скорость выхода в live.

## 3. Общие принципы развития

- Paper mode остаётся режимом по умолчанию.
- Live execution не добавляется до готовности risk, persistence, reconciliation и monitoring.
- Каждая стратегия сначала проверяется на исторических данных, затем в paper/shadow режиме.
- AI не получает прямого доступа к исполнению и не обходит Risk Manager.
- Новые адаптеры должны проходить общие контрактные тесты.
- Все торговые решения должны быть воспроизводимыми и аудируемыми.
- Изменение архитектурных границ отражается в `ARCHITECTURE.md`.

## Этап 1. Надёжный слой рыночных данных

### Цель

Получить полноценный и проверенный источник исторических и текущих рыночных данных.

### Планируемые результаты

Завершённая основа Stage 1 включает контракт `MarketProvider`, Binance Spot адаптер для `BTCUSDT`, получение Binance OHLCV, нормализованные модели и независимую проверку качества свечей. Оставшаяся работа разделена на следующие milestones.

#### M1.5 Market Cache — completed

- Утвердить минимальный контракт Market Cache и ключ потока `symbol + interval`.
- Реализовать bounded in-memory хранение нормализованных проверенных свечей.
- Зафиксировать вставку, duplicate, out-of-order и обновление незакрытой последней свечи.
- Реализовать чтение последних `N` свечей и последней закрытой свечи.
- Добавить типизированные ошибки и unit-тесты контракта.
- Не включать сеть, polling, persistence или торговую логику.

#### M1.6 Polling Engine — completed

- Реализовать первый синхронный REST polling loop для `BTCUSDT`.
- Ограничить использование `MarketProvider` ingestion-слоем.
- Передавать полученные свечи через Validation в Market Cache.
- Добавить timeout policy, retry/backoff, rate-limit handling и graceful shutdown.
- `asyncio` и WebSocket не вводить без отдельного ADR.

#### M1.7 Market Events — completed

- Реализовать минимальные cache-события `CandleInserted`, `CandleUpdated`, `CandleClosed` и `CandleEvicted`.
- Отделить диагностические `MarketGapDetected` и `MarketStreamFailed` от событий изменения cache.
- Публиковать cache-события только после успешной валидации и атомарного обновления Market Cache.
- Добавить синхронный in-process Event Dispatcher с aggregate delivery result.
- Обеспечить детерминированный порядок, immutable payload и однократное `CandleClosed` в пределах lifecycle процесса.
- Расширить cache mutation result подробными изменениями без удаления текущих счётчиков.
- Не включать стратегии, торговые решения или execution.

#### M1.8 Stage 1 completion — completed

- Завершить contract, unit и integration tests market pipeline.
- Проверить обработку ошибочных payload, пропусков, повторов, rate limits и временных сбоев.
- Настроить lint, type check и CI для обязательных проверок.
- Подтвердить устойчивую работу ingestion → validation → cache → events без необработанных исключений.
- Актуализировать документацию форматов данных, гарантий и эксплуатационных ограничений.

### Критерии готовности

- Все реализации проходят единый набор контрактных тестов.
- Ошибочные и неполные ответы Binance обрабатываются предсказуемо.
- Тестовый набор не требует доступа к интернету.
- Внутренние потребители получают рыночные данные только через Market Cache.
- Backtest, paper, testnet и live имеют общий интерфейс чтения Market Cache.
- Агент способен устойчиво получать данные заданное тестовое время без необработанных исключений.
- Документация описывает формат данных и гарантии провайдера.

### Не входит в этап

- Формирование торговых сигналов.
- Отправка ордеров.
- Live trading.

## Этап 2. Доменное ядро и baseline-стратегия

Статус этапа: **completed** (2026-08-04).

Подтверждение: реализованы EMA baseline, Intent Planner, Trading Decision Pipeline и
Shadow Trading Decision Engine. Реальный Binance smoke-run сформировал восемь
объяснимых HOLD decisions без failures, execution или ордеров.

### Цель

Сформировать независимую от биржи модель торгового решения и первую простую детерминированную стратегию.

### Планируемые результаты

- Утверждены модели `MarketSnapshot`, `Signal`, `OrderIntent` и `RiskDecision`.
- Определён интерфейс стратегии.
- Реализована простая детерминированная EMA crossover strategy без дополнительных фильтров как прозрачный baseline benchmark.
- Параметры стратегии типизированы и валидируются.
- Сигналы содержат время, причину, версию стратегии и входные параметры.
- Trading pipeline подключён к runtime только через feature-flagged shadow flow и
  не связан с risk, portfolio или execution.
- Добавлены unit-тесты сигналов и граничных случаев стратегии.

#### M2.1 Trading Domain Model — completed

- Утверждены immutable `MarketSnapshot`, `Signal`, `OrderIntent`, `RiskDecision`,
  границы Strategy и target-exposure sizing contract.

#### M2.2 Trading Domain Models and EMA Baseline — completed

- Реализованы и протестированы доменные модели, Snapshot Factory и
  детерминированная closed-candle-only EMA crossover strategy.

#### M2.3 Trading Decision Pipeline — design

Статус: **completed**. Design реализован и подтверждён этапом M2.4.

- Спроектировать синхронную stateless orchestration `MarketSnapshot -> Strategy -> Signal -> Intent Planner -> OrderIntent`.
- Зафиксировать immutable pipeline/planner results, cross-model invariants, error policy и audit correlation.
- Утвердить первый Spot long-only target-exposure planner без portfolio, risk и execution.
- Подготовить deterministic testing strategy и минимальный implementation plan M2.4.

#### M2.4 Intent Planner and Trading Decision Pipeline — completed

- Реализованы типизированный Spot long-only `IntentPlanner` и синхронный stateless `TradingDecisionPipeline`.
- Зафиксирован детерминированный поток `MarketSnapshot -> Strategy -> Signal -> IntentPlanResult -> OrderIntent | None`.
- Добавлены cross-model invariants, типизированные ошибки boundary и внедряемые фабрики идентификаторов.
- Добавлены unit- и integration-тесты, включая совместную работу с EMA baseline strategy.

#### M2.5 Shadow Trading Decision Engine — design

Статус: **completed**.

- Спроектировать application service, запускающий shadow evaluation по подтверждённому `CandleClosed`.
- Зафиксировать cache read port, event/cache consistency, immutable shadow record и observer boundary.
- Определить lifecycle подписки, bounded in-memory dedup, failure isolation, status и безопасное logging.
- Подготовить feature-flagged runtime wiring proposal без risk, execution, portfolio и persistence.
- Подготовить детерминированный test plan и минимальную implementation scope M2.6.

M2.5 не подключает trading pipeline к runtime и не завершает Stage 2. Реализация и
shadow smoke-run выполняются отдельными этапами после review design-документа.

#### M2.6 Shadow Trading Decision Engine — implementation

Статус: **completed**.

Bootstrap-to-regular cache lifecycle исправлен и покрыт offline regression tests.
Реальный Binance shadow smoke-run 2026-08-04 успешно подтвердил получение
`CandleClosed` в runtime и формирование восьми успешных объяснимых HOLD decisions
без failures, execution или ордеров.

- Реализовать синхронный application service, запускаемый только `CandleClosed`.
- Подключить bounded in-memory dedup, immutable decision record и logging observer.
- Подключать shadow flow composition root-ом только при feature flag; не добавлять
  risk, execution, portfolio mutation или orders.
- Сохранить изоляцию shadow failures от market ingestion.

### Критерии готовности

- Стратегия не зависит от Binance, HTTP или базы данных.
- Одинаковые входные данные и конфигурация дают одинаковый результат.
- Нет прямого вызова execution из стратегии.
- Поведение стратегии покрыто тестами на заранее определённых сценариях.
- Для каждого сигнала можно объяснить использованные данные и правило решения.

### Не входит в этап

- Оптимизация доходности.
- AI-сигналы.
- Реальные ордера.

## Этап 3. Backtesting и paper trading

Статус: **completed** (2026-08-09).

### Цель

Создать воспроизводимую среду оценки стратегии до допуска к биржевому исполнению.

#### M3.1 Risk, Portfolio and Paper Execution Core — design

Статус: **completed** (2026-08-04).

- Спроектировать обязательный Risk Manager, Portfolio accounting и детерминированный
  paper execution для Binance Spot `BTCUSDT`, long-only.
- Зафиксировать контракты, Decimal sizing/rounding, fee/slippage, idempotency,
  ownership, audit boundaries и будущую persistence.
- Не подключать private API, testnet, live trading или реальный капитал.

#### M3.2 Portfolio Accounting Core

Статус: **completed** (2026-08-04).

- Реализованы immutable `InstrumentRules`, `Fill`, `Position`, `PortfolioConfig` и
  `PortfolioSnapshot`, а также single-stream Portfolio aggregate.
- Подтверждены deterministic Decimal accounting, exact reconciliation, atomic fill
  application, bounded idempotency, абсолютный drawdown и архитектурная изоляция.
- Runtime wiring, Risk Manager и PaperExecutionProvider не подключены.

#### M3.3 Risk Manager

Статус: **design completed** (2026-08-05).

- Спроектирован обязательный synchronous deterministic risk gate, преобразующий
  `OrderIntent + PortfolioSnapshot + MarketSnapshot` в `RiskDecision`.
- Зафиксированы ordered gates, portfolio-aware target/delta sizing, InstrumentRules,
  APPROVED/MODIFIED/REJECTED, long-only и dust semantics.
- Реализация, runtime wiring, fee calculation и execution не входят в M3.3.

#### M3.4 Risk Manager implementation

Статус: **completed** (2026-08-05).

- Реализованы immutable risk policy/sizing models, ordered checks и типизированные
  APPROVED/MODIFIED/REJECTED decisions.
- Реализованы long-only target/delta sizing, deterministic caps, worst-case BUY
  budget и полный EXIT с явной paper-only dust liquidation semantics.
- Final safety review пройден: подтверждены exact sizing, cap/minimum ordering,
  affordability boundaries, full exit, dust semantics и statelessness.
- Runtime wiring, Portfolio mutation, execution и persistence не добавлены.

#### M3.5 Paper Execution Provider

Статус: **completed** (2026-08-05).

- Спроектировать synchronous stateless immediate-fill adapter без сети и private API.
- Использовать существующий immutable `Fill`, explicit Decimal fee/quote rounding,
  injected IDs и decision-derived logical time.
- Runtime wiring не входит в design или implementation provider-а.

#### M3.6 Paper Execution Provider implementation

Статус: **completed** (2026-08-05).

- Реализован и проверен provider offline, включая RiskDecision -> Fill -> Portfolio.
- Final safety review подтвердил fee envelope, deterministic timestamps, full-fill,
  REJECTED и dust compatibility semantics.
- Runtime, PaperTradingEngine, persistence и decision-level dedup не добавлены.

#### M3.7 Paper Trading Orchestration

Статус: **completed** (2026-08-05).

- Спроектировать application-level PaperTradingEngine, continuous mark-to-market,
  bounded process-local dedup, observer и failure atomicity.
- Не подключать runtime и не запускать Strategy повторно.

#### M3.8 Paper Trading Engine design review

Статус: **completed; final architecture approved for implementation** (2026-08-05).

- Утверждены lifecycle, composition root, ownership, sequence, atomicity, dedup,
  observer/result/status contracts, logging и recovery assumptions.
- Существующие API Decision Pipeline, Risk, Paper Execution и Portfolio не меняются.

#### M3.9 Paper Trading Engine implementation

Статус: **completed; final safety review passed** (2026-08-05).

- Реализованы synchronous in-memory orchestration, bounded dedup, observer isolation,
  DEGRADED safety boundary и полный offline HOLD -> BUY -> HOLD -> SELL flow.
- Runtime events, shared coordinator и persistence не подключены.

#### M3.10 Shared Decision Runtime Coordination Design

- Статус: **completed; design review completed** (2026-08-08).
- Спроектирован `TradingDecisionCoordinator`, создающий один MarketSnapshot и один
  shared DecisionPipelineResult без повторного Strategy run.
- Зафиксированы feature matrix, shadow migration, consumer isolation, bounded
  event-level dedup и paper DEGRADED behavior.
- Реализация и runtime wiring вынесены в M3.11.

#### M3.11 Shared decision runtime coordination implementation

Статус: **completed; final safety review passed** (2026-08-08).

- Реализованы coordinator, ShadowDecisionConsumer, feature-flagged composition,
  process-local dedup и paper runtime graph без private API/live execution.

#### M3.12 Persistence, Audit and Recovery

Статус: **completed; final design review passed**
(2026-08-08).

- Спроектированы authoritative append-only journal, verified Portfolio checkpoints,
  stable decision/execution keys, единая durable paper transaction и fail-closed
  recovery. Контракт зафиксирован в `PERSISTENCE_RECOVERY_DESIGN.md`.

#### M3.13 Persistence Core and SQLite adapter

Статус: **completed; final safety review passed** (2026-08-09).

- Реализованы canonical durable keys/records, persistence ports, journal/checkpoint
  core, migration v1, SQLite adapter, transaction atomicity, backup и isolated
  fail-closed recovery/reconciliation без runtime wiring.
- Final review подтвердил explicit Portfolio root, crash rollback на всех pre-commit
  boundaries, restart dedup, strict terminal lineage, checkpoint fallback, bounded
  Decimal canonicalization и deterministic SQLite resource lifecycle.

#### M3.14 Runtime Recovery Integration

Статус: **completed; final safety review passed** (2026-08-09).

- Подключены durable Paper Cycle transaction, startup recovery, Portfolio restore,
  restart-stable decision/command identity, commit-before-memory synchronization,
  checkpoint lifecycle и composition ordering до подписки coordinator.

#### M3.15 Crash and Restart Safety Review

Статус: **completed** (2026-08-09).

- Подтверждены failpoints до/после commit, lost commit acknowledgement, exact restart
  reconstruction, corruption handling, locking, atomic post-commit memory sync и
  отсутствие duplicate Fill. Evidence: `M3_15_CRASH_RESTART_REPORT.md`.

#### M3.16 Stage 3 completion

Статус: **completed** (2026-08-09).

- Automated audit и controlled two-run public-data restart smoke на одной SQLite DB
  подтвердили durable runtime continuity, shared shadow/paper decision delivery и
  progression Portfolio/journal `0 -> 1 -> 2` без duplicate financial mutation.
- Smoke наблюдал только HOLD; BUY/SELL restart safety подтверждена deterministic
  automated suites, а не real public-data execution.

### Планируемые результаты

- Реализован replay исторических свечей.
- Определена модель симулированного времени.
- Реализован paper execution adapter.
- Учтены комиссии, spread, slippage и правила округления.
- Добавлены модели `Order`, `Fill`, `Position` и `Portfolio`.
- Рассчитываются realised/unrealised PnL, drawdown, turnover и базовые risk-adjusted metrics.
- Конфигурация, версия стратегии и результаты каждого прогона сохраняются.
- Начальное хранение состояния, audit trail и результатов реализовано в SQLite через persistence port.
- Предусмотрено разделение train/in-sample и validation/out-of-sample периодов.
- Добавлены проверки против look-ahead bias.

### Критерии готовности

- Backtest с одинаковыми данными и конфигурацией полностью воспроизводим.
- Баланс, позиции, комиссии и PnL сходятся на контрольных сценариях.
- Один pipeline стратегии используется в backtest и paper mode.
- Результаты можно сравнивать между версиями стратегии.
- Отчёт явно показывает допущения симуляции.

### Не входит в этап

- Оптимизация параметров на всей истории без out-of-sample проверки.
- Реальное биржевое исполнение.

## Этап 4. Risk hardening и биржевое execution

Статус: **in progress — final design approved, implementation not started**.

### Цель

Расширить проверенный paper-контур эксплуатационными risk controls, durable
recovery и изолированным testnet execution до отдельного допуска live.

#### M4.1 Exchange Execution Architecture Design

Статус: **completed** (2026-08-09).

- Спроектированы отдельный TESTNET execution contour, durable multi-state order
  lifecycle, pre-submit client identity, partial-fill accounting boundary,
  reconciliation gate, kill switch и fail-closed LIVE isolation.
- ADR-055–ADR-062 и полный контракт зафиксированы в
  `EXCHANGE_EXECUTION_DESIGN.md`. Production code, private API и migration SQL не
  добавлены.

#### M4.2 Final Design and Safety Review

Статус: **completed — final safety review passed** (2026-08-09).

- Закрыты open questions M4.1; проверены official Binance Spot/Testnet contracts,
  transport/exchange-truth boundary, ambiguous cases A–F, accounting, crash matrix,
  additive migration и LIVE isolation. ADR-055–ADR-062 приняты.

#### M4.3 Exchange Order Domain and Ports

Статус: **next**.

- Реализовать immutable order/trade/control models и adapter-independent gateway/query
  ports в точном соответствии с утверждённым M4.2 contract.

#### M4.4 Risk Hardening

- Добавить deterministic session/pending exposure limits и runtime safety controls без
  I/O внутри RiskManager.

#### M4.5 Binance Testnet Adapter

- Реализовать изолированный private Testnet adapter, signing/redaction, endpoint
  allowlist и contract tests. LIVE остаётся unsupported.

#### M4.6 Durable Order Lifecycle

- Реализовать conceptual schema v2, pre-submit command/attempt transactions, durable
  order events, exchange trades и Portfolio settlement.

#### M4.7 Exchange Reconciliation

- Реализовать startup/runtime reconciliation, cursors, balance/open-order comparison и
  fail-closed conflict handling.

#### M4.8 Runtime Testnet Integration

- Подключить TESTNET consumer после recovery/reconciliation gate; сохранить shared
  decision path и независимый shadow mode.

#### M4.9 Crash/Restart/Duplicate-Order Safety Review

- Проверить ambiguous submit, partial fills, cancellations, writer exclusion, lost
  acknowledgements и exact-once Portfolio application per exchange trade.

#### M4.10 Controlled Binance Testnet Smoke

- После отдельного разрешения выполнить bounded BUY/SELL/restart/cancellation smoke
  только на Testnet с проверкой endpoint и отсутствия secret leakage.

#### M4.11 Stage 4 Completion

- Свести automated и controlled Testnet evidence. Завершение Stage 4 не разрешает LIVE;
  live-readiness остаётся отдельным будущим review.

### Планируемые результаты

- Укреплён порт `ExecutionProvider` для stateful exchange lifecycle.
- Расширен централизованный Risk Manager поверх проверенного Stage 3 core.
- К базовым лимитам добавлены session loss, просадка и kill switch.
- Реализованы kill switch и запрет торговли на устаревших данных.
- Добавлены идемпотентные client order IDs и защита от повторной отправки.
- Определена state machine ордера.
- Реализованы persistence и audit trail для decisions, orders и fills.
- Реализовано восстановление и reconciliation состояния.
- Подключён Binance testnet adapter.
- Проведён paper/shadow run, достаточный для сбора требуемой выборки наблюдений.
- Собрано достаточное для допуска количество наблюдений по утверждённым критериям качества.

### Критерии готовности

- Ни один ордер не может обойти Risk Manager.
- Повторная обработка одного события не создаёт повторную сделку.
- После рестарта состояние восстанавливается и сверяется с источником исполнения.
- Сценарии timeout, partial fill, reject и disconnect покрыты тестами.
- Kill switch проверен автоматическим сценарием.
- Testnet используется без доступа к реальному капиталу.

### Условия допуска к следующему этапу

- Нет необъяснимых расхождений портфеля и исполнения в согласованной выборке наблюдений.
- Отсутствуют критические ошибки, определённые критериями допуска.
- Метрики риска и исполнения собираются автоматически.
- Процедура аварийной остановки документирована и проверена.

## Этап 5. Production readiness и контролируемое развитие AI

### Цель

Подготовить агента к автономной эксплуатации и создать безопасную основу для экспериментальных AI-компонентов.

### Планируемые результаты

- Реализованы supervisor/event loop и graceful shutdown.
- Добавлены health/readiness checks.
- Введены структурированные логи, метрики, dashboards и alerts.
- Настроены резервное копирование и восстановление состояния.
- Определены SLO для данных, исполнения и восстановления.
- Добавлено версионирование стратегий, моделей и конфигураций.
- AI-модуль подключается только через утверждённый интерфейс стратегии или аналитики.
- Первая роль AI ограничена аналитикой, объяснением и классификацией рыночного режима в shadow mode без влияния на execution.
- AI-решения содержат версию модели, входные данные и audit metadata.
- Проводится shadow-сравнение AI и baseline без влияния на исполнение.
- Определён процесс model validation, rollback и обнаружения деградации.

### Критерии готовности

- Агент переживает контролируемый рестарт без потери или дублирования операций.
- Критические состояния автоматически обнаруживаются и поднимают alert.
- AI-модуль невозможно подключить в обход Risk Manager.
- AI демонстрирует улучшение относительно baseline на out-of-sample и shadow-периодах.
- Live mode остаётся отдельно управляемой возможностью, а не автоматическим продолжением paper mode.

### Отдельный шлюз допуска к live

Live trading может обсуждаться только после отдельного review. Минимальные условия:

- пройден согласованный период paper и testnet эксплуатации;
- установлены жёсткие лимиты капитала и убытка;
- проверены kill switch, reconciliation и alerts;
- подготовлена процедура ручного вмешательства;
- назначены ответственные за эксплуатацию;
- явно утверждены биржа, инструменты и максимальный капитал;
- выполнен отдельный security review секретов и API-разрешений.

## 4. Сквозные направления

Эти работы сопровождают каждый этап, а не откладываются до конца.

### Качество

- Ruff и статическая проверка типов.
- Unit, contract и integration tests.
- Контроль покрытия критической логики.
- CI для каждого изменения.

### Документация

- Инструкции запуска и конфигурации.
- Architecture Decision Records для значимых решений.
- Описание доменных моделей и контрактов.
- Runbooks для сбоев и восстановления.

### Безопасность

- Минимальные разрешения API-ключей.
- Разделение testnet и live credentials.
- Исключение секретов из логов и репозитория.
- Регулярная проверка зависимостей.

### Наблюдаемость

- Correlation ID для каждого торгового решения.
- Метрики свежести данных, риска, ордеров и портфеля.
- Audit trail от сигнала до fill.

## 5. Метрики прогресса

Прогресс оценивается не количеством написанного кода, а готовыми возможностями:

- доля портов, покрытых контрактными тестами;
- воспроизводимость backtest;
- число необработанных ошибок за paper-run;
- расхождение локального и биржевого состояния;
- задержка и свежесть рыночных данных;
- доля решений, имеющих полный audit trail;
- максимальная наблюдаемая просадка относительно заданного лимита;
- время обнаружения и восстановления после контролируемого сбоя.

## 6. Утверждённые решения

1. Первый рынок — Binance Spot.
2. Первый инструмент эксплуатации — `BTCUSDT`.
3. Первый market loop — синхронный REST polling.
4. `asyncio` и WebSocket отложены до отдельного ADR.
5. Первая baseline-стратегия — чистая детерминированная EMA crossover strategy без ATR/volatility filter; правила пересечения и параметры зафиксированы на Stage 2.
6. Источник исторических данных — Binance OHLCV.
7. Начальное хранилище — SQLite за persistence port.
8. Допуск к testnet и live определяется отдельными критериями качества, достаточным количеством наблюдений и отсутствием критических ошибок, а не только календарной продолжительностью.
9. Live trading остаётся долгосрочной целью и требует отдельного review и допуска.
10. Первая роль AI — аналитика, объяснение и классификация рыночного режима в shadow mode без влияния на execution.

Детали реализации, численные пороги и критерии допуска уточняются внутри соответствующих этапов и фиксируются отдельными ADR до начала зависимых работ.
