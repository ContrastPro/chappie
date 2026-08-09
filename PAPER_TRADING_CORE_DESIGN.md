# Risk, Portfolio and Paper Execution Core

## 1. Статус и область

- Статус: **approved for M3.2 implementation**.
- Этап: M3.1 design.
- Дата: 2026-08-04.
- M3.1 design: **completed**.
- M3.2 Portfolio Accounting Core: **implementation completed; final review passed**.
- Stage 3: **in progress**; Risk и paper execution завершены и прошли final review,
  PaperTradingEngine реализован и прошёл final safety review; runtime wiring остаётся
  future scope.

Документ проектирует первый исполняемый, но полностью симулированный контур:

```text
OrderIntent
    -> Risk Manager
    -> RiskDecision
    -> Paper Execution
    -> Fill
    -> Position
    -> Portfolio
    -> Performance Metrics
```

Первая версия ограничена Binance Spot, `BTCUSDT`, активами BTC/USDT, long-only,
одним процессом и детерминированным paper execution. Она не использует private API,
testnet, live capital, margin, futures, leverage или short. Этот design не разрешает
реальные ордера и не меняет Python-код.

### Утверждённые для M3.2 решения

- BUY fee в quote asset включается в position cost basis.
- Отсутствие исполняемой дельты позднее выражается typed `REJECTED`, без нового
  `RiskDecision.NO_ACTION`.
- Shadow и Paper engines остаются отдельными application services.
- `initial_base_balance` в M3.2 обязан быть равен нулю.
- Drawdown первой версии — абсолютная quote-величина, не процент.
- Unrealised PnL не учитывает гипотетическую exit fee.
- Fill idempotency ограничена bounded in-memory FIFO registry и временем жизни
  процесса; durable exactly-once отсутствует.
- Execution layer обязан передавать уже округлённые внешние quote values. Portfolio
  проверяет их exact consistency и не округляет повторно.
- Остаточный base dust после валидного partial SELL допускается. M3.6 добавляет узкое
  исключение: paper-only SELL всей текущей позиции с подтверждённым
  `dust_liquidation=True` может быть ниже `min_quantity` или `min_notional`; step,
  gross, fee, stream и reconciliation проверки остаются обязательными.

## 2. Принципы и границы

- Risk Manager — обязательный gate: ни Strategy, ни AI, ни Planner не вызывают
  execution напрямую.
- Portfolio — единственный авторитетный источник balances, position, cost basis и
  cumulative realised PnL.
- Первая immediate-fill Execution создаёт `Fill`, но не меняет Portfolio; отдельный
  `Order` появится только вместе с order lifecycle.
- Portfolio применяет Fill и не рассчитывает рыночное решение.
- Все финансовые величины — finite `Decimal`; время и IDs передаются явно.
- Одинаковые входы, config, IDs и timestamps дают одинаковый результат.
- Доменные компоненты не знают Binance, HTTP, MarketProvider, logger или SQLite.
- Paper mode не делает сетевых запросов и не маскируется под testnet/live.
- Безопасность, воспроизводимость и audit trail важнее скорости и доходности.

## 3. Runtime orchestration

Отдельный application service `PaperTradingEngine` продолжает уже вычисленное
решение и не встраивается в Strategy, Polling Engine или Event Dispatcher:

```text
CandleClosed
  -> TradingDecisionCoordinator
  -> one MarketSnapshot + one DecisionPipelineResult
  -> PaperTradingEngine
  -> RiskManager.evaluate(intent, portfolio_snapshot, market_snapshot)
  -> RiskDecision
  -> PaperExecutionProvider.execute(risk_decision)
  -> Fill
  -> Portfolio.apply_fill(fill, mark_price)
  -> PaperTradingCycleResult
  -> observer / future persistence
```

Рекомендуемый узкий Risk API:

```text
evaluate(
    intent: OrderIntent,
    portfolio: PortfolioSnapshot,
    market_snapshot: MarketSnapshot,
) -> RiskDecision
```

Risk Manager не принимает весь `TradingDecisionResult`: это сохраняет границу и
позволяет одинаково использовать intent в paper и backtest. Composition root
создаёт engine, risk policy, portfolio, instrument rules, execution provider,
factories и observer; зависимости не закрываются engine-ом.

`PAPER_TRADING_ENABLED=false` — независимый feature flag. Paper flow может
переиспользовать результат того же decision evaluation, но не зависит от включения
shadow logging observer. Pipeline не должен вычисляться дважды для одного события.

## 4. Future orchestration market context

Application orchestration может фиксировать следующий immutable context для
согласования Risk и Portfolio. `PaperExecutionProvider` его не принимает: необходимые
execution facts уже подтверждены в RiskDecision, а stream/precision приходят через
его собственные `InstrumentRules`.

| Поле | Тип | Назначение |
|---|---|---|
| `stream_key` | `MarketStreamKey` | `BTCUSDT` и interval |
| `market_snapshot_id` | `str` | точный вход решения |
| `source_event_id` | `str` | породивший `CandleClosed` |
| `correlation_cycle_id` | `str` | ingestion correlation |
| `exchange_time_ms` | `int` | единое market time цикла |
| `reference_price` | `Decimal` | close подтверждённой candle |
| `instrument_rules` | `InstrumentRules` | versioned правила округления |

Цена положительна, время неотрицательно, контекст согласован с intent stream и не
содержит mutable Candle/cache references. В M3.x stale-data policy проверяется до
risk approval; wall clock не заменяет exchange time.

## 5. Portfolio model

### 5.1 Минимальные типы

`AssetBalance(asset, available, reserved)` — immutable snapshot; в первой версии
`reserved == 0`. `Position` существует только при `quantity > 0` и содержит
`symbol`, `base_asset`, `quote_asset`, `quantity`, `average_cost`, cumulative
`realised_pnl`, `opened_at_ms`, `updated_at_ms`.

`PortfolioSnapshot` содержит:

- `portfolio_id`, монотонный `version`, `as_of_ms`;
- quote/base balances;
- `position: Position | None` для единственного инструмента;
- cumulative fees, realised PnL и applied fill IDs;
- valuation price, equity, exposure notional и exposure fraction.

Первое состояние: `USDT=10000`, `BTC=0`, position отсутствует. Конфигурация
начального капитала передаётся при создании Portfolio и не читается из environment
в домене.

### 5.2 Valuation

При цене `P`:

```text
base_value       = base_quantity * P
equity           = quote_balance + base_value
exposure_notional = base_value
exposure_fraction = 0, если equity == 0, иначе base_value / equity
unrealised_pnl   = position.quantity * (P - position.average_cost)
```

Unrealised PnL не вычитает гипотетическую будущую exit fee. Portfolio snapshot
указывает valuation price/time, поэтому результат воспроизводим.

### 5.3 Fill accounting

BUY:

```text
gross = quantity * fill_price
quote_delta = -(gross + fee)
new_cost_basis = old_quantity * old_average_cost + gross + fee
new_average_cost = new_cost_basis / new_quantity
```

SELL:

```text
gross = quantity * fill_price
quote_delta = gross - fee
realised_delta = quantity * (fill_price - average_cost) - fee
```

При partial SELL average cost не меняется. При полном SELL `Position` становится
`None`, а cumulative realised PnL остаётся в Portfolio. Для partial SELL удаляемый
cost равен `average_entry_price * sold_quantity`; для exact full SELL удаляется весь
оставшийся authoritative `cost_basis`, включая детерминированный Decimal division
residual. Нельзя получить отрицательный balance/quantity или продать больше BTC,
чем доступно.

`cost_basis` является authority бухгалтерской стоимости. `average_entry_price`
вычисляется делением с локальной детерминированной Decimal precision и не
округляется к `price_tick`. Для периодической средней произведение
`quantity * average_entry_price` может отличаться от cost basis на точный конечный
Decimal residual; reconciliation всегда использует cost basis без tolerance.
`Position.realised_pnl` означает realised PnL только текущего непрерывного lifecycle
позиции, тогда как `PortfolioSnapshot.realised_pnl` — единственный cumulative
authority между закрытиями и повторными открытиями.

`apply_fill(fill) -> PortfolioMutationResult` атомарен. Повторный `fill_id` возвращает
typed duplicate/no-op result и не меняет state; конфликтующий повтор — ошибка
integrity. Result содержит immutable previous/current snapshots и applied deltas.

## 6. Risk Manager и sizing

### 6.1 Policy первой версии

Risk Manager проверяет:

- только разрешённые `BTCUSDT`, Spot, long-only actions;
- согласованность intent/context/portfolio и freshness;
- target exposure `[0, max_target_exposure]`;
- максимальный notional одного order;
- минимальный order notional;
- максимальный position notional;
- quote reserve;
- доступность quote для BUY и base для SELL с учётом fee/slippage;
- положительную quantity после округления;
- отсутствие повторного intent/idempotency key.

Дневной loss limit, drawdown kill switch, multi-asset concentration и correlation
risk не входят в минимальную реализацию: им нужны persisted history и отдельная
семантика торгового дня.

### 6.2 Target exposure

Risk вычисляет цель через portfolio equity по reference price:

```text
target_notional = approved_target_exposure * equity
delta_notional  = target_notional - current_base_value
```

Положительная дельта создаёт BUY, отрицательная — SELL. Если абсолютная дельта
после limits и rounding меньше minimum order, решение `REJECTED` с устойчивым
reason code `already_at_target` или `below_minimum_order`; execution не вызывается.
Отдельный `NO_ACTION` outcome пока не добавляется в `RiskDecision`, чтобы не менять
утверждённый контракт без необходимости. `PaperTradingCycleResult` явно различает
`NO_INTENT`, `RISK_REJECTED` и `EXECUTED`.

`APPROVED` сохраняет requested target exposure. `MODIFIED` фиксирует уменьшенную
цель и конкретный limit. `REJECTED` не содержит executable quantity.

### 6.3 Эволюция RiskDecision

Текущий `RiskDecision` достаточен как approval record, но недостаточен как единственный
execution input. В M3.2 его следует обратно совместимо дополнить immutable
`ApprovedOrderParameters` (только для APPROVED/MODIFIED): side, quantity, estimated
notional, reference price, fee/slippage assumptions, instrument-rules version,
portfolio version и idempotency key. Исходные поля exposure и applied limits
сохраняются. Execution принимает только эти разрешённые параметры, а не исходный
intent.

## 7. InstrumentRules и округление

```text
InstrumentRules(
    symbol, base_asset, quote_asset,
    price_tick, quantity_step, minimum_quantity,
    minimum_notional, version,
)
```

Все значения `Decimal` и положительны. Quantity всегда округляется вниз к
`quantity_step`; это предотвращает overspend/oversell. Price и fee квантуются по
явным правилам quote precision. После округления повторно проверяются min quantity,
min notional, balances и limits. Никакие Binance filters не разбираются доменом;
позднее адаптер преобразует их в `InstrumentRules`.

Gross notional, fee и net quote change округляет execution layer. Portfolio требует
точного равенства этим уже округлённым значениям. Internal cost basis, realised PnL,
equity и drawdown не квантуются повторно; операции используют собственный
`localcontext`, независимый от global Decimal context.

Начальные статические значения: price tick `0.01`, quantity step `0.00001`, minimum
quantity `0.00001`. Они конфигурируемы и versioned, не hard-coded в risk formulas.

## 8. Execution contract

```text
PaperExecutionProvider.execute(
    decision: RiskDecision,
) -> PaperExecutionResult
```

Первая immediate-fill версия не создаёт отдельный `Order`: `execution_id` используется
как `Fill.order_id`. Существующий `Fill` содержит fill/execution/decision/intent IDs,
stream, side, quantity, fill price, gross notional, fee asset/amount, logical execution
time и correlation. Отдельная Order lifecycle model появится только с явным consumer.

`PaperExecutionProvider`:

- выполняет MARKET order сразу и полностью;
- не делает network calls и не меняет Portfolio;
- использует Risk-approved reference price и нулевой slippage;
- рассчитывает fee от fill notional;
- использует только внедрённые config, InstrumentRules и ID factories;
- выводит execution time из decision timestamp и configured fixed latency без clock;
- для одинаковых входов возвращает одинаковую семантику результата;
- остаётся stateless; decision-level dedup принадлежит будущей orchestration/journal.

Первая версия не моделирует partial fill, variable latency, spread/order book,
cancellation или liquidity impact. Эти ограничения обязательно присутствуют в отчёте.

## 9. Idempotency и атомарность

Сквозной ключ первой версии:

```text
paper:{portfolio_id}:{intent_id}:{risk_policy_version}:{portfolio_version}
```

Защита действует на трёх уровнях:

1. engine dedup не обрабатывает один intent повторно;
2. future execution journal обеспечивает decision-level uniqueness;
3. Portfolio не применяет один `fill_id` дважды.

Risk approval, execution и portfolio mutation пока не образуют durable transaction.
Порядок всегда `risk -> fill -> apply_fill`; failure до fill не меняет Portfolio,
failure после fill, но до apply возвращает явный `COMMITTED_FILL_NOT_APPLIED` и
требует reconciliation, а не слепого retry. Для безопасного runtime такого окна
недостаточно: до длительного paper run нужен persistence journal/outbox. Ошибка
observer не откатывает уже применённый Fill.

## 10. Cycle result и ошибки

`PaperTradingCycleResult` immutable и содержит correlation, intent/risk/order/fill
IDs, exchange time, outcome, previous/current portfolio version, execution model
version, durations и typed failure stage.

Минимальные outcomes:

- `NO_INTENT`;
- `RISK_REJECTED`;
- `EXECUTED`;
- `EXECUTION_FAILED`;
- `PORTFOLIO_UPDATE_FAILED`;
- `OBSERVATION_DEGRADED`;
- `DUPLICATE_SUPPRESSED`.

Typed error families: invalid market context/instrument rules, risk policy violation,
inconsistent portfolio version, insufficient balance, invalid approved quantity,
duplicate conflict, paper execution error, invalid fill, portfolio integrity error
и observation error. Ошибки не содержат secrets или полный market payload.

## 11. Performance metrics

После каждого committed portfolio mutation строится immutable
`PerformanceSnapshot`: equity, realised/unrealised/total PnL, cumulative fees,
turnover, current exposure и peak-to-current drawdown. Initial equity и peak equity
хранятся явно. Метрики не влияют на execution и вычисляются из PortfolioSnapshot с
одной valuation price/time. Sharpe и другие risk-adjusted metrics откладываются до
replay series и утверждения sampling semantics.

## 12. Events, persistence и audit

M3.2 не расширяет Market Events торговыми событиями. Минимальная реализация
возвращает immutable results и вызывает observer. Будущие `RiskDecided`,
`PaperOrderCreated`, `PaperFillCreated`, `PortfolioChanged` публикуются только после
соответствующего commit; их ordering/delivery требует отдельного ADR.

M3.12 заменяет ранний набор независимых repository proposals единым transactional
`TradingPersistence` / `PaperCycleTransaction` boundary: authoritative append-only
journal и verified Portfolio checkpoints должны commit-ить Fill, valuation и
Portfolio transition атомарно. SQLite остаётся первым adapter, но domain и aggregate
от него не зависят. Полный контракт: `PERSISTENCE_RECOVERY_DESIGN.md`, ADR-048–054.

Audit record связывает source event, snapshot, signal, intent, risk policy и limits,
order, fill, portfolio versions, metrics и model/config versions. Секретов в нём нет.

## 13. Lifecycle, observer и безопасность

`PaperTradingEngine` владеет только своей subscription/dedup state. Dispatcher,
decision service, Portfolio, RiskManager, ExecutionProvider и observers принадлежат
composition root. Первая версия synchronous, single-threaded, один writer Portfolio;
thread safety не обещается. `close()` provider-а engine не вызывает.

Логи содержат IDs, outcome, reason codes, quantities/notional, versions и latency,
но не credentials или payload. Paper execution явно помечается `mode=paper`.
`PAPER_TRADING_ENABLED` по умолчанию false; `TRADING_MODE=paper` не означает
автоматического включения execution contour.

## 14. Предлагаемая конфигурация

Документируемые defaults для будущей реализации:

```text
PAPER_TRADING_ENABLED=false
PAPER_INITIAL_QUOTE_BALANCE=10000
PAPER_INITIAL_BASE_BALANCE=0
PAPER_FEE_RATE=0.001
PAPER_SLIPPAGE_BPS=5
PAPER_MAX_TARGET_EXPOSURE=1
PAPER_MAX_ORDER_NOTIONAL=1000
PAPER_MIN_ORDER_NOTIONAL=10
PAPER_MAX_POSITION_NOTIONAL=5000
PAPER_QUOTE_RESERVE=0
PAPER_PRICE_TICK=0.01
PAPER_QUANTITY_STEP=0.00001
PAPER_MIN_QUANTITY=0.00001
```

Settings boundary преобразует строки в Decimal и валидирует cross-field invariants.
В M3.1 `.env.example` не меняется.

## 15. Предлагаемая структура модулей

```text
app/risk/
    models.py
    policy.py
    exceptions.py
app/portfolio/
    models.py
    portfolio.py
    metrics.py
    exceptions.py
app/execution/
    models.py
    provider.py
    paper.py
    exceptions.py
app/paper/
    engine.py
    observer.py
```

Общие trading IDs и `OrderIntent` остаются в `app/trading`. Infrastructure imports
domain ports, но risk/portfolio не импортируют paper adapter или Binance.

## 16. Test strategy будущей реализации

- Decimal validation, immutability и defensive snapshots;
- target exposure sizing для enter/resize/exit/already-at-target;
- каждый risk limit, APPROVED/MODIFIED/REJECTED и stable reason codes;
- rounding boundaries, min notional, reserve, no overspend/oversell;
- deterministic fee/slippage и BUY/SELL direction;
- weighted average cost, partial/full exit, fees и realised/unrealised PnL;
- atomic `apply_fill`, duplicate idempotency и conflicting duplicates;
- cycle failure boundaries и observer degradation;
- один decision evaluation/fill на event при duplicate delivery;
- property-based accounting invariants и контрольные end-to-end scenarios;
- отсутствие network/private API и зависимостей от wall clock.

## 17. План реализации Stage 3

1. **M3.2 — Portfolio Accounting Core:** модели, InstrumentRules, snapshots,
   Portfolio accounting, deterministic precision, idempotency и unit tests —
   **completed; final review passed**. Risk вынесен в M3.3.
2. **M3.3 — Risk Manager Design:** ordered gates, portfolio-aware target/delta
   sizing, limits и approved order parameters — **completed**.
3. **M3.4 — Risk Manager Implementation:** pure risk service и offline tests без
   execution adapter — **completed; final review passed**.
4. **M3.5 — Paper Execution Provider Design:** immediate-fill contract, fee/time/
   factory semantics и offline integration plan.
5. **M3.6 — Paper Execution Provider Implementation:** deterministic adapter и
   offline RiskDecision -> Fill -> Portfolio tests без runtime — **completed; final
   review passed**.
6. **M3.7 — Paper Trading Orchestration Design:** application contract, continuous
   valuation, failure boundaries, dedup и observer semantics — **completed**.
7. **M3.8 — Paper Trading Engine Design Review:** lifecycle, composition root,
   ownership, atomicity, recovery и final implementation contract — **completed**.
8. **M3.9 — Paper Trading Engine Implementation:** in-memory orchestration и offline
   HOLD -> BUY -> HOLD -> SELL flow — **completed; final safety review passed**.
9. **M3.10 — Shared Decision Runtime Coordination Design:** единый coordinator,
   feature matrix, consumer isolation и event-level dedup — **completed; design review passed**.
10. **M3.11 — Shared Decision Runtime Coordination Implementation:** feature-flagged
   delivery одного DecisionPipelineResult в shadow и paper без повторного Strategy run
   — **completed; final safety review passed**.
11. **M3.12 — Persistence, Audit and Recovery Design:** journal/checkpoint model,
   stable durable keys, transaction и fail-closed recovery — **design completed;
   final design review passed**.
12. **M3.13 — Persistence Core and SQLite Adapter:** ports, records, migrations,
   journal/checkpoints и adapter без runtime wiring.
13. **M3.14 — Runtime Recovery Integration:** durable Paper Unit of Work, startup
   recovery и composition lifecycle.
14. **M3.15 — Crash/Restart Safety Review:** failpoints, reconstruction, corruption
   и locking review — **completed** (2026-08-09).
15. **M3.16 — Stage 3 Completion:** backtest/replay parity, controlled paper smoke,
   quality evidence и отдельный review. Testnet/live остаются закрыты.

M3.14 implementation добавила additive Portfolio contracts: validated `restore`, pure
valuation/Fill previews и atomic `apply_committed_transition`. Accounting formulas,
Decimal semantics и instrument validation остаются в Portfolio; persistence хранит
полные доказуемые before/after states. M3.15 усилил synchronization до атомарного
valuation+Fill batch. Application runtime выполняет durable commit до
изменения in-memory Portfolio и до observer delivery.

## 18. Открытые вопросы

1. Выбрать точную quote precision для fee и balances; не выводить её неявно из tick.
2. Определить freshness threshold для paper risk gate по interval.
3. **Решено в M3.4:** non-positive equity отклоняется; `EXIT_LONG` ликвидирует всю
   позицию, а существующий dust допускается только как явно отмеченная paper-only
   `DUST_LIQUIDATION` без partial SELL.
4. Утвердить idempotency key следующего orchestration слоя; fill registry M3.2 уже
   использует bounded FIFO по `fill_id`.
5. **Решено в M3.7:** будущий общий runtime coordinator вычисляет один
   `DecisionPipelineResult` и передаёт его независимым shadow и paper consumers;
   двойной запуск pipeline запрещён. M3.10 зафиксировал точный contract, а runtime
   wiring вынесен в M3.11.
6. Утвердить semantics peak equity/drawdown при внешнем deposit/withdrawal; в первой
   версии внешние cash flows запрещены.
7. Определить момент внедрения trading events: рекомендация — после устойчивого
   immutable result/observer contour.
8. Утвердить, что реальный paper smoke возможен только после M3.4 review и остаётся
    без private Binance API.
