# Trading Domain Model

## 1. Статус и область

- Статус: approved for M2.2 implementation.
- Дата: 2026-08-04.
- Основание: `VISION.md`, `ARCHITECTURE.md`, `DECISIONS.md`, `ROADMAP.md` и завершённый Stage 1.

Документ проектирует доменные типы и границы Stage 2. Он не вводит execution,
paper broker, Risk Manager, реальные ордера, portfolio accounting, persistence или
AI. Все описанные модели независимы от Binance, HTTP, `MarketProvider`, Market
Cache, polling, базы данных и системных часов.

## 2. Целевой pipeline и словарь

```text
Market Cache / Market Events
    -> Snapshot Factory
    -> MarketSnapshot
    -> Strategy
    -> Signal
    -> Intent Planner
    -> OrderIntent
    -> Risk Manager
    -> RiskDecision
    -> ExecutionProvider
```

В M2.1 фиксируются только модели и контракты. Названия имеют разную семантику:

- `MarketSnapshot` — воспроизводимый вход одного решения;
- `Signal` — объяснимый результат анализа, но не разрешение на сделку;
- `OrderIntent` — намерение изменить экспозицию до проверки риска;
- `RiskDecision` — неизменяемый результат обязательной risk-проверки;
- `Order` — будущая команда конкретному execution adapter, не `OrderIntent`;
- `Fill` — будущий факт исполнения, не `RiskDecision`.

Один `decision_correlation_id` связывает snapshot, signal, intent и risk decision.
Собственные IDs объектов создаются injectable factories. Production factory может
использовать UUID, тесты — последовательные детерминированные значения.

## 3. Общие правила моделей

- Модели immutable и не раскрывают изменяемые ссылки.
- Финансовые значения используют только `Decimal`; `NaN`, infinity и неявный
  `float` запрещены.
- Временные значения — целые Unix milliseconds. Они передаются извне.
- Market time приходит из exchange-time pipeline. Домен не читает local wall clock.
- Enum сериализуются стабильными строковыми значениями.
- Версии являются непустыми стабильными строками, например `ema-crossover/1.0.0`.
- Explanation содержит стабильный reason code и человекочитаемые параметры, но не
  заменяет структурированные входы и версию алгоритма.
- Произвольные mutable `dict`, клиенты, callbacks, logger, cache, provider, ORM и
  секреты внутри моделей запрещены.
- Extension metadata в первой версии не входит в обязательный контракт. Если она
  понадобится, используется canonical immutable tuple типизированных пар, а не
  произвольный словарь.

## 4. MarketSnapshot

### Назначение

Immutable defensive snapshot данных, передаваемый стратегии ровно для одного
решения. Его создаёт application-level `MarketSnapshotFactory`: она читает Market
Cache после подтверждённого market event, но возвращает доменную копию, а не cache
tuple или ссылку на cache.

### Минимальный контракт

| Поле | Тип | Обязательность | Семантика |
|---|---|---:|---|
| `snapshot_id` | `str` | да | ID воспроизводимого входа |
| `decision_correlation_id` | `str` | да | Сквозная корреляция решения |
| `stream_key` | `MarketStreamKey` | да | Нормализованные symbol + interval |
| `as_of_ms` | `int` | да | Зафиксированное exchange time |
| `closed_candles` | `tuple[CandleSnapshot, ...]` | да | Строго упорядоченная закрытая история |
| `latest_candle` | `CandleSnapshot | None` | да | Последняя candle, включая открытую |
| `source_event_id` | `str | None` | да | Событие-триггер, если snapshot event-driven |
| `correlation_cycle_id` | `str | None` | да | Poll/replay cycle происхождения |
| `data_version` | `int` | да | Монотонная версия stream snapshot |
| `staleness_ms` | `int` | да | Возраст последней ожидаемой market update |
| `is_stale` | `bool` | да | Результат явной freshness policy |

`latest_closed_candle` не хранится отдельным полем: это последний элемент
`closed_candles` либо `None`. Так исключается возможность противоречивого
дублирования. `staleness_ms` вычисляет factory по утверждённой policy; `is_stale`
должен равняться сравнению этого значения с порогом policy.

### Инварианты

- `snapshot_id`, `decision_correlation_id` и `stream_key` валидны.
- `as_of_ms >= 0`, `data_version > 0`, `staleness_ms >= 0`.
- `closed_candles` не пуст для вызова baseline strategy, строго упорядочен и
  не содержит duplicate или пересекающихся диапазонов; каждая candle закрыта:
  `as_of_ms > close_time_ms`.
- Все candles принадлежат одному `stream_key` по контексту snapshot.
- `latest_candle`, если задана, не старше последней закрытой candle.
- Если `latest_candle` закрыта, она совпадает с последней `closed_candles`.
- Открытая `latest_candle` может присутствовать, но не входит в `closed_candles`.
- Snapshot содержит defensive immutable `CandleSnapshot`, а не `Candle`.

Baseline использует **только `closed_candles`**. Открытая candle доступна будущим
analytics как явно отделённое наблюдение, но Strategy contract первой версии не
разрешает использовать её. Это предотвращает look-ahead и расхождение между
backtest и текущим рынком. Минимум истории задаёт конкретная strategy через
`required_history`; snapshot сам не встраивает один глобальный минимум.

Один snapshot безопасно передавать нескольким pure strategies: данные immutable,
а каждая стратегия получает одинаковый `as_of_ms` и `data_version`.

## 5. Signal

### Назначение и enum

`Signal` — полный аудируемый результат Strategy. `HOLD` является полноценным
сигналом, а не `None`: недостаток данных, stale input и отсутствие crossover должны
быть видимы и объяснимы.

```text
SignalDirection = BUY | SELL | HOLD
SignalReason = BULLISH_CROSSOVER | BEARISH_CROSSOVER |
               NO_CROSSOVER | VOLATILITY_FILTERED |
               INSUFFICIENT_HISTORY | STALE_SNAPSHOT
```

### Минимальный контракт

| Поле | Тип | Обязательность | Семантика |
|---|---|---:|---|
| `signal_id` | `str` | да | ID signal |
| `decision_correlation_id` | `str` | да | Сквозная корреляция |
| `input_snapshot_id` | `str` | да | Точный вход решения |
| `stream_key` | `MarketStreamKey` | да | Поток рынка |
| `created_at_ms` | `int` | да | Явно переданное market/application time |
| `effective_at_ms` | `int` | да | Время последней использованной закрытой candle |
| `direction` | `SignalDirection` | да | BUY, SELL или HOLD |
| `reason` | `SignalReason` | да | Машиночитаемое объяснение |
| `explanation` | `str` | да | Краткое объяснение с наблюдёнными значениями |
| `strategy_name` | `str` | да | Стабильное имя алгоритма |
| `strategy_version` | `str` | да | Версия правил |
| `strategy_parameters` | immutable typed config snapshot | да | Полная конфигурация решения |

В M2.2 `created_at_ms == effective_at_ms == snapshot.as_of_ms`; Strategy не получает
часы. Изменение семантики effective time потребует versioned contract.

BUY допускает только reason `bullish_crossover`, SELL — только
`bearish_crossover`. HOLD не может использовать эти два direction reason codes.

Confidence не включается в baseline: детерминированный crossover не производит
калиброванную вероятность, а произвольное число создаст ложную точность. SELL
описывает bearish signal и допустим без знания позиции; возможность сформировать
sell intent определяется позже portfolio-aware Intent Planner. Signal не содержит
цену, quantity, notional, time-in-force или execution instructions.

## 6. OrderIntent

### Назначение

`OrderIntent` формирует отдельный deterministic `IntentPlanner`, а не Strategy.
Planner принимает Signal и в будущем явный portfolio snapshot/policy. Это отделяет
рыночное мнение от капитала и не позволяет стратегии выбирать размер средств.

### Sizing

Первая модель использует `target_exposure: Decimal` в диапазоне `[0, 1]`: желаемую
долю long-only Spot exposure после будущего выполнения intent. Absolute quantity и
notional появятся после instrument rules и portfolio model; они не хранятся рядом с
target exposure заранее, поскольку эти значения могут противоречить друг другу.

### Enum и поля

```text
OrderIntentAction = ENTER_LONG | EXIT_LONG
```

| Поле | Тип | Обязательность |
|---|---|---:|
| `intent_id`, `source_signal_id`, `decision_correlation_id` | `str` | да |
| `stream_key` | `MarketStreamKey` | да |
| `created_at_ms` | `int` | да |
| `action` | `OrderIntentAction` | да |
| `target_exposure` | `Decimal` | да |
| `reason`, `explanation` | enum + `str` | да |
| `strategy_name`, `strategy_version` | `str` | да |

`ENTER_LONG` требует `target_exposure > 0`, а `EXIT_LONG` — ровно `0`. HOLD никогда
не преобразуется в OrderIntent. `order_type`, `limit_price`, `time_in_force`,
`reduce_only`, quantity и notional отсутствуют: intent не является `Order` и не
содержит exchange IDs или adapter payload.

## 7. RiskDecision

### Назначение и enum

```text
RiskOutcome = APPROVED | REJECTED | MODIFIED
```

Ни один intent не передаётся в execution без `RiskDecision`. Strategy, AI и Intent
Planner не могут создавать решение `APPROVED`; это право принадлежит только
детерминированному Risk Manager.

### Минимальный контракт

| Поле | Тип | Обязательность | Семантика |
|---|---|---:|---|
| `decision_id` | `str` | да | ID risk decision |
| `intent_id` | `str` | да | Проверенный intent |
| `decision_correlation_id` | `str` | да | Сквозная корреляция |
| `decided_at_ms` | `int` | да | Явно переданное время |
| `outcome` | `RiskOutcome` | да | APPROVED/REJECTED/MODIFIED |
| `requested_target_exposure` | `Decimal` | да | Исходная цель intent |
| `approved_target_exposure` | `Decimal | None` | да | Разрешённая цель |
| `rejection_reasons` | `tuple[RiskReason, ...]` | да | Структурированные причины |
| `applied_limits` | immutable typed policy snapshot | да | Применённые лимиты и значения |
| `risk_policy_version` | `str` | да | Версия risk rules |
| `explanation` | `str` | да | Человекочитаемый audit summary |

Инварианты:

- APPROVED: approved exposure задан и равен requested exposure; rejection reasons пусты.
- REJECTED: approved exposure отсутствует, есть минимум одна причина.
- MODIFIED: approved exposure задан и отличается от requested exposure; rejection
  reasons пусты, а применённое ограничение фиксируется в `applied_limits`.
- Обе exposure величины finite и находятся в диапазоне `[0, 1]`.
- `decided_at_ms >= intent.created_at_ms`; ID и версии непусты.
- Исходный и итоговый размеры находятся в самой immutable decision, поэтому audit
  не зависит только от последующего lookup intent.

Quantity/notional не появляются до утверждения instrument/portfolio contracts. При
их введении outcome должен показывать исходные и итоговые параметры столь же явно.
RiskDecision не содержит `Order`, `Fill`, adapter response или mutable portfolio.

## 8. Strategy contract

Рекомендуемый минимальный API:

```text
Strategy
  name: str
  version: str
  required_history: int
  configuration: immutable typed config

  validate_configuration() -> None
  generate_signal(snapshot: MarketSnapshot) -> Signal
```

Контракт синхронный, pure и deterministic. Strategy использует только переданный
snapshot, immutable config и injected ID factory, не выполняет I/O, не читает cache,
database, provider или clock, не вызывает risk/execution и не изменяет snapshot.
Одинаковые snapshot, config и deterministic factory sequence дают одинаковый Signal.

Недостаток истории и stale snapshot возвращают HOLD с `INSUFFICIENT_HISTORY` или
`STALE_SNAPSHOT`. Это ожидаемые domain outcomes, а не исключения. Исключения нужны
для структурно invalid snapshot/config, потому что продолжать вычисление на них
опасно. Freshness threshold принадлежит snapshot policy/application configuration,
а Strategy доверяет согласованным `is_stale` и `staleness_ms`.

## 9. Baseline strategy

### Сравнение

| Стратегия | Простота | Объяснимость | Особенности |
|---|---:|---:|---|
| SMA crossover | высокая | высокая | резкий выход окна, простейший reference |
| EMA crossover | высокая | высокая | больший вес свежих закрытий, утверждён roadmap |
| Breakout | средняя | высокая | нужны точные channel/tie rules |
| Simple momentum | высокая | средняя | чувствителен к выбору horizon/normalization |

Утверждена чистая **EMA crossover strategy без volatility/ATR filter**. Она служит
детерминированным benchmark и не обещает прибыльность. Volatility filter является
возможным будущим extension после отдельного design review.

### Immutable configuration

- `fast_window: int >= 2`;
- `slow_window: int > fast_window`;
- `strategy_name = "ema_crossover"`;
- `strategy_version = "1.0.0"`.

### Exact EMA semantics

Для периода `N` первая EMA на индексе `N - 1` равна SMA первых `N` close. Далее:

```text
alpha = Decimal(2) / Decimal(N + 1)
EMA[t] = close[t] * alpha + EMA[t - 1] * (Decimal(1) - alpha)
```

Вычисление выполняется в локальном Decimal context с фиксированной precision `28`
и не зависит от изменяемого global context. Сигнал определяется только по двум последним **закрытым**
точкам, где доступны обе EMA:

- BUY: `fast_previous <= slow_previous` и `fast_current > slow_current`;
- SELL: `fast_previous >= slow_previous` и `fast_current < slow_current`;
- HOLD: во всех остальных случаях, включая равенство текущих EMA.

Так crossover публикуется один раз, а нахождение fast EMA выше/ниже slow EMA не
создаёт повторный BUY/SELL на каждой candle.

`required_history = slow_window + 1`. Значение `+1` нужно для сравнения предыдущей
и текущей EMA точки. Defaults первой версии: fast `12`, slow `26`. ATR и volatility
filter в baseline отсутствуют.

## 10. Validation и типизированные ошибки

Предлагаемая иерархия:

```text
TradingDomainError
├── InvalidMarketSnapshotError
├── InsufficientHistoryError       # для strict analytics API, не baseline outcome
├── StaleMarketSnapshotError       # для mandatory-fresh consumers
├── InvalidStrategyConfigurationError
├── InvalidSignalError
├── InvalidOrderIntentError
├── InvalidRiskDecisionError
├── InconsistentDomainTimestampError
├── InvalidFinancialValueError
└── UnsupportedTradingInstructionError
    ├── UnsupportedOrderSideError
    └── UnsupportedOrderTypeError
```

Ожидаемые `INSUFFICIENT_HISTORY` и `STALE_SNAPSHOT` baseline strategy превращает в
HOLD; одноимённые exceptions нужны компонентам, для которых продолжение запрещено.
Ошибки должны содержать безопасные field/reason values, но не secrets или полный
неограниченный payload.

`strategy_parameters`, `metadata` и `applied_limits` defensively копируются.
Последовательности рекурсивно преобразуются в tuple; mutable nested mappings и
произвольные объекты отклоняются типизированной validation вместо поверхностного
`MappingProxyType` над изменяемым содержимым.

## 11. Предлагаемая структура модулей

Для модульного монолита достаточно компактной структуры:

```text
app/trading/
    models.py          # MarketSnapshot, Signal, OrderIntent, RiskDecision, enums
    exceptions.py      # domain validation errors
    strategy.py        # Strategy protocol and ID factory ports
    snapshot.py        # application-facing Snapshot Factory contract

app/strategies/
    ema_trend.py       # config and pure baseline implementation
```

На M2.2 модели можно разделить только после измеримого роста файлов. `orders.py` и
`risk.py` преждевременны, пока соответствующие компоненты не реализуются. Market
layer не импортирует `app.trading` или `app.strategies`; strategy импортирует только
domain contracts. Infrastructure adapters зависят внутрь, домен — ни от них, ни от
Stage 1 orchestration.

## 12. Границы ответственности

- Market layer не зависит от strategies.
- Snapshot Factory — anti-corruption boundary между Market Cache и trading domain.
- Strategy зависит от domain models, но не от cache/event implementation.
- Signal не является OrderIntent; Strategy не создаёт intent напрямую.
- Intent Planner не разрешает риск и не вызывает execution.
- OrderIntent не является Order.
- RiskDecision не является Fill.
- Только approved/modified RiskDecision может стать входом будущего execution
  application service.
- Future AI подчиняется тому же Strategy/analytics contract и не получает отдельного
  пути к intent approval или execution.

## 13. Открытые вопросы для review

1. Утвердить HOLD как полноценный Signal вместо `None` — рекомендация: да.
2. Target exposure утверждён как sizing contract первой версии.
3. Утвердить отсутствие confidence в baseline — рекомендация: не добавлять без
   калиброванной семантики.
4. Оставить optional open candle в snapshot только для analytics — рекомендация: да,
   Strategy первой версии видит контракт, но обязана использовать closed history.
5. Определить stale threshold по interval и эксплуатационному режиму; не встраивать
   одно глобальное число в model.
6. Чистая EMA crossover без ATR утверждена; фильтры являются future extension.
7. Стратегия первой версии stateless; stateful strategies требуют отдельного
   lifecycle/persistence design.
8. Утвердить SemVer для strategy/config versions.
9. M2.x остаётся single-symbol decision flow; multi-symbol snapshot требует portfolio
   и synchronization semantics.
10. Position-aware SELL и repeated execution suppression принадлежат Intent
    Planner/portfolio orchestration, а не Signal. Требуется design перед их реализацией.

## 14. Зафиксированные архитектурные изменения

- Architecture pipeline разделяет Strategy, Intent Planner и Risk Manager.
- ADR-019 запрещает Strategy и AI формировать executable order напрямую.
- ADR-020 утверждает closed-candle-only EMA crossover без дополнительных фильтров.
- Target exposure утверждён как sizing model первой Spot long-only версии.
- Roadmap согласован с EMA defaults и отсутствием ATR/volatility filter.

## 15. Минимальный implementation plan для M2.2

1. Утвердить открытые решения M2.1 и ADR, не реализуя execution/risk manager.
2. Создать immutable enums/models и типизированные validation errors в `app/trading`.
3. Реализовать `MarketSnapshotFactory` с defensive snapshots, closed-only history,
   explicit exchange time, freshness policy и injected IDs.
4. Добавить Strategy protocol и deterministic ID factory tests.
5. Реализовать pure EMA calculations и baseline strategy с точной crossover semantics.
6. Покрыть модели и стратегию unit tests: invariants, insufficient/stale HOLD,
   equality, one-shot crossings, Decimal-only calculations,
   immutable data и отсутствие look-ahead.
7. Не подключать strategy к долгоживущему agent pipeline до отдельного integration
   review; M2.2 сначала должен доказать доменную детерминированность изолированно.

## 16. Stage 3 extension boundary

Stage 2 завершён без execution. Детальная эволюция `RiskDecision`, а также модели
`InstrumentRules`, `Order`, `Fill`, `Position`, `PortfolioSnapshot` и
`PerformanceSnapshot` вынесены в `PAPER_TRADING_CORE_DESIGN.md`.

Существующие exposure-поля `RiskDecision` сохраняются. Для Stage 3 предлагается
добавить отдельные immutable approved order parameters с quantity, notional,
reference price, portfolio/rules versions и idempotency key. Это не превращает
`OrderIntent` в order и не даёт Strategy или AI права обходить Risk Manager.
