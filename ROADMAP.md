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
  recovery. Контракт зафиксирован в
  [`docs/core/PERSISTENCE.md`](docs/core/PERSISTENCE.md).

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

Статус: **ACTIVE MVP — Paper/Testnet v4 continuous runtime; LIVE unavailable**.

The active MVP starts with a fresh disposable Paper or Testnet database and uses the normal durable
order, reconciliation, settlement, and Portfolio flow. Any unexpected Testnet execution failure enters
`SAFE_STOP`; it never retries in-process and the developer investigates or resets the disposable DB
before a fresh-process restart. The operator procedure is
[`SERVER_MVP_RUNBOOK.md`](docs/operations/SERVER_MVP_RUNBOOK.md).

M4.10/Stage 4 one-shot authorities, recovery, disposition, successor-generation work, and migrations
v5–v10 are retained as **historical experiment evidence**. They are not prerequisites for fresh MVP
startup and do not define MVP readiness. Their explicit historical test suites remain available for
forensic regression work; they are not normal server operations.

### Цель

Расширить проверенный paper-контур эксплуатационными risk controls, durable
recovery и изолированным testnet execution до отдельного допуска live.

#### M4.1 Exchange Execution Architecture Design

Статус: **completed** (2026-08-09).

- Спроектированы отдельный TESTNET execution contour, durable multi-state order
  lifecycle, pre-submit client identity, partial-fill accounting boundary,
  reconciliation gate, kill switch и fail-closed LIVE isolation.
- ADR-055–ADR-062 и исходный M4.1 design относятся к
  [историческому Stage 4](docs/historical/stage4/README.md). Production code,
  private API и migration SQL в этом milestone не добавлялись.

#### M4.2 Final Design and Safety Review

Статус: **completed — final safety review passed** (2026-08-09).

- Закрыты open questions M4.1; проверены official Binance Spot/Testnet contracts,
  transport/exchange-truth boundary, ambiguous cases A–F, accounting, crash matrix,
  additive migration и LIVE isolation. ADR-055–ADR-062 приняты.

#### M4.3 Exchange Order Domain and Ports

Статус: **completed; final domain safety review passed**.

- Реализованы immutable command/order/trade/event/control/session models, pure lifecycle,
  deterministic client order ID, aggregation/settlement helpers, pending exposure,
  process-local capability и adapter-independent gateway/account-reader ports.
- Не добавлены private API, signing, migration v2, runtime wiring, reconciliation service,
  Risk hardening, Portfolio extension, cancellation command или LIVE capability.
- Final review подтвердил ACK-not-Fill, exhaustive lifecycle/reconciliation matrix,
  immutable scoped identity, idempotent exact trade duplicates, fail-closed conflicts,
  explicit partial-fill remaining exposure и generation-bound process capability.

#### M4.4 Risk Hardening

Статус: **completed; final safety review passed** (2026-08-10).

- Добавлен pure additive exchange-risk gate с deterministic session/pending exposure
  limits, reconciled account context и отдельным submission-safety result без I/O,
  clock или hidden state внутри Risk.
- Реализованы conservative one-order/one-inflight policy, remaining-exposure accounting,
  session submitted/turnover/count/failure limits, drawdown gate и generation-bound
  reconciliation/kill-switch/capability checks. Realised-loss limit отложен до появления
  однозначного session baseline.
- Final review подтвердил exact BUY/SELL boundaries, partial-fill и multi-record
  aggregation до conservative hard gate, projected position, ordered reasons,
  kill-switch/capability generations, Decimal determinism и полную изоляцию от I/O.

#### M4.5 Binance Testnet Adapter

Статус: **completed; final adapter safety review passed** (2026-08-11).

- Реализован изолированный synchronous Binance Spot Testnet adapter с strict host
  allowlist, injected timestamp/client/identity resolver, deterministic HMAC signing,
  explicit timeouts и без automatic retries.
- MockTransport tests покрывают MARKET ACK, ambiguous submit, query/trades/balances/
  open orders/exchangeInfo, auth/rate-limit errors, secret redaction и resource ownership.
- Runtime wiring, credentials/settings, authenticated smoke, persistence и LIVE support
  не добавлены.

- Реализовать изолированный private Testnet adapter, signing/redaction, endpoint
  allowlist и contract tests. LIVE остаётся unsupported.

#### M4.6 Durable Order Lifecycle

Статус: **completed; final crash/restart safety review passed** (2026-08-11).

- Реализованы additive schema v2, pre-submit command/attempt transactions, append-only
  order events/trades, current projection integrity, durable session counters и kill switch.
- Только PREPARED допускает первый submit; SUBMITTING и последующие non-terminal states
  после restart требуют reconciliation. Trade truth сохраняется независимо от settlement;
  Portfolio settlement отложен.
- Final review подтвердил exact-once attempt/counters, rollback на каждой transaction
  boundary, commit-ack ambiguity, ACK/ambiguous/rejection duplicate storms, immutable external
  IDs, corruption detection, Decimal-context independence и v1 -> v2 compatibility.

#### M4.7 Exchange Reconciliation

Статус: **completed; final reconciliation safety review passed** (2026-08-11).

- Реализован adapter-independent `ExchangeReconciler`: stable-client-ID query, authoritative
  order advancement, trade ingestion, ownership checks, balance/rules validation и fail-closed
  typed outcomes. Schema v3 advances generation only for a complete SAFE cycle.
- Portfolio settlement, runtime wiring, credentials, authenticated smoke, cancellation и LIVE
  остаются вне M4.7.
- M4.7 SAFE доказывает exchange truth в полном заявленном history horizon, но явно не означает
  Portfolio settlement или submission readiness; эти дополнительные gates принадлежат M4.8.

#### M4.8 Runtime Testnet Integration

Статус: **completed; final safety review passed** (2026-08-12).

- Добавлены schema v4, exact-once settlement exchange trades в managed Portfolio, ordered
  recovery/reconciliation/settlement startup и fail-closed readiness capability, привязанная к
  account/session/reconciliation/control lineage.
- TESTNET включается только явной конфигурацией и allowlisted endpoint; LIVE отвергается.
- Automated validation использует только fake readers/gateways и локальную SQLite, без private
  network calls или реальных orders. Completion pass добавил opaque single-use final
  authorization, durable TOCTOU revalidation, shared Testnet consumer delivery и полную
  settlement recovery lineage validation; final acceptance review passed.
- Production composition поддерживает recovery-only startup: persistence/reconciliation/settlement
  остаются доступны при `BLOCKED`, но capability, application service, Testnet consumer и
  Testnet-only coordinator отсутствуют. Fresh restart является единственным переходом в `READY`.
  ADR-063 принят по результатам M4.8 final acceptance review.
- Full-graph recovery evidence recreates production `build_application()` graphs across lost ACK,
  FILLED/PARTIALLY_FILLED settlement, repeated NOT_FOUND and lost ambiguity-persistence cases;
  remote submissions and durable attempts remain exactly one.

#### M4.9 Crash/Restart/Duplicate-Order Safety Review

Статус: **completed; final crash/restart safety review passed** (2026-08-13).

- Проверить ambiguous submit, partial fills, cancellations, writer exclusion, lost
  acknowledgements и exact-once Portfolio application per exchange trade.
- Deterministic offline torture extended production recovery to twenty NOT_FOUND restarts,
  later FOUND resolution, 100 same-process and ten cross-process duplicate decisions, twenty
  RECOVERY_ONLY/READY restarts, and fixed-seed crash schedules. No production safety defect was
  found; M4.10 controlled Testnet smoke design is prepared and authorization is pending.

#### M4.10 Controlled Binance Testnet Smoke

Статус: **completed — PASS** (2026-08-14). One-shot BUY, SAFE reconciliation,
schema-v6 remediation and immutable fresh-process recovery proof passed.

- По отдельным разрешениям выполнить read-only authentication, `/api/v3/order/test`, bounded
  BUY/SELL и restart/reconciliation smoke только на Spot Testnet. Cancellation остаётся
  unsupported. План не разрешает credentials, network или order до отдельной phase authorization.
- Central logging suppression and an isolated MockTransport-proven `/api/v3/order/test` adapter
  contract are implemented. Neither capability performs an authenticated action by itself.
- Phase 0's bounded-reader blocker is resolved by `python -m app.tools.testnet_phase1`: a separate
  authorization-token-gated, fixed-host GET-only harness with a hard five-request budget, narrow
  read facade, sanitized evidence and no persistence/submission composition. Phase 1 retry #1
  passed with exactly four GETs and no side effects; its authorization did not carry into Phase 2.
- `python -m app.tools.testnet_phase2` requires `M4_10_PHASE2_AUTHORIZED=YES` and permits exactly
  one fixed-host `POST /api/v3/order/test`. Its reviewed Phase 2-only BUY quantity is
  `0.00016000` BTC based on the public reference price `63849.22000000`; all numeric checks use
  `Decimal`. Phase 2 passed with exactly one `/api/v3/order/test` POST and no financial semantics.
- ADR-064 accepts the zero-BTC/`25 USDT` managed subledger and BUY-only durable authority consumed
  atomically with the sole PREPARED command while the global switch remains ACTIVE. Its in-memory
  exact-command claim is lost on restart. Schema-v5/runtime implementation and crash proofs are
  complete and the final adversarial review passed; Phase 3 execution is not authorized.

#### M4.11 Stage 4 Completion

Статус: **completed — STAGE_4_BLOCKED** (2026-08-14).

- Automated и controlled Testnet evidence сведены в `M4_11_STAGE_4_COMPLETION_REPORT.md`.
  Все проверенные критерии пройдены, кроме принятого требования controlled Testnet SELL
  с persisted exchange trade: durable evidence содержит ровно один BUY и ни одного SELL.
- Следующая работа — отдельное, ещё не начатое и не авторизованное закрытие controlled
  managed-balance SELL evidence. Stage 5 не начат. Завершение Stage 4 в любом случае не
  разрешит continuous Testnet или LIVE и не будет доказательством profitability.
- Offline architecture selects an exact full-managed-position exit under fresh rules, a distinct
  additive schema-v7 one-shot authority and same-database recovery. See
  `STAGE_4_MANAGED_SELL_CLOSURE_DESIGN.md`; ADR-066 is accepted, migration 007 is implemented
  offline. The first authorized invocation is now recovery-only after stopping at PREPARED with zero
  attempts; schema-v8 recovery/disposition implementation exists offline, while production recovery
  remains unauthorized.

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
- M4.8 completed; final safety review passed. Production Testnet composition,
  authoritative execution flow and durable reconciliation coverage horizon are implemented.
  M4.9 completed; final crash/restart safety review passed. M4.10 security preflight is
  completed and its final preflight safety review passed; Phase 1 authenticated read-only PASS,
  Phase 2 signed order-test PASS, one-shot BUY/settlement/recovery PASS and M4.10 completed.
  M4.11 evidence aggregation completed with Stage 4 BLOCKED solely on missing accepted
  controlled-SELL evidence; Stage 5 has not started.
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
