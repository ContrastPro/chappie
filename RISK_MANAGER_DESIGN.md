# Deterministic Risk Manager

## 1. Статус и область

- Статус: **design and implementation completed; final review passed**.
- Этап: M3.3–M3.4 completed.
- Дата: 2026-08-05.

Документ проектирует синхронный детерминированный Risk Manager для первого
Binance Spot `BTCUSDT` long-only paper-контура. Python implementation, runtime
wiring, execution, Fill creation и Portfolio mutation в M3.3 не входят.

```text
MarketSnapshot
    -> Strategy
    -> Signal
    -> Intent Planner
    -> OrderIntent
    -> Risk Manager + immutable PortfolioSnapshot
    -> RiskDecision
    -> future Execution
```

Risk Manager — единственный компонент, имеющий право разрешить исполнение
`OrderIntent`. Он не выполняет сделку, не создаёт `Fill`, не изменяет Portfolio и
не вызывает infrastructure.

## 2. Границы ответственности

Risk Manager отвечает за:

- согласованность intent, portfolio, market snapshot и instrument rules;
- freshness gate по готовому `MarketSnapshot.is_stale`;
- преобразование target exposure в target position и delta order;
- применение hard policy limits и `InstrumentRules`;
- проверку balances, long-only semantics, minimums и dust;
- immutable объяснимый `RiskDecision` с полным sizing audit.

Risk Manager не отвечает за:

- Strategy, Signal или создание OrderIntent;
- вычисление staleness и получение market data;
- fee, slippage, spread или fill price;
- создание Order/Fill и execution lifecycle;
- Portfolio mutation, persistence, logging или events;
- retry, network, Binance payload или system time.

Запрещены зависимости от Binance, HTTP, Market Cache, Market Events, Polling,
ExecutionProvider, Shadow, TraderAgent, database, filesystem и random. Допустимы
только trading/market primitives, immutable Portfolio models, InstrumentRules и
собственные typed policy/result types.

## 3. Рекомендуемый публичный контракт

```text
RiskManager(
    policy: PortfolioRiskPolicy,
    instrument_rules: InstrumentRules,
    decision_id_factory: RiskDecisionIdFactory,
)

evaluate(
    intent: OrderIntent,
    portfolio: PortfolioSnapshot,
    market_snapshot: MarketSnapshot,
) -> RiskDecision
```

`evaluate()` полностью synchronous, side-effect free и не читает часы. Для
`decided_at_ms` используется `market_snapshot.as_of_ms`; ID создаёт injected
factory. Одинаковые входы, policy, rules и factory output дают равный decision.

### PortfolioRiskPolicy первой реализации

Минимальный immutable policy snapshot:

| Поле | Семантика |
|---|---|
| `policy_version` | стабильная версия правил |
| `allowed_stream_key` | единственный разрешённый stream |
| `base_asset`, `quote_asset` | BTC/USDT |
| `max_target_exposure` | hard upper bound в `[0, 1]` |
| `quote_reserve` | USDT, недоступные для gross BUY notional |

`max_order_notional` и `max_position_notional` являются обязательными hard limits.
Безопасное уменьшение quantity до лимита даёт deterministic `MODIFIED`; если лимит
невозможно выполнить с учётом minimums, решение отклоняется. BUY budget использует
worst-case envelope из slippage и fee rate. Daily loss, drawdown kill switch,
volatility и frequency limits в первую реализацию не входят.

## 4. Reference price и согласованность входов

Reference price — `market_snapshot.latest_closed_candle.close`. Открытая candle не
используется. Snapshot без закрытой candle отклоняется как `MARKET_PRICE_UNAVAILABLE`.
Цена обязана быть положительной, finite Decimal и уже соответствовать
`InstrumentRules.price_tick`; Risk её не округляет.

До вызова Risk application layer обязан mark-to-market Portfolio этой же reference
price и `market_snapshot.as_of_ms`. Risk проверяет:

- все три stream keys и rules stream равны;
- portfolio assets равны rules assets;
- `portfolio.as_of_ms == market_snapshot.as_of_ms`;
- при позиции `position.last_mark_price == reference_price`;
- correlation intent совместима с вызывающим decision flow.

Несогласованная оценка даёт `PORTFOLIO_MARK_MISMATCH` или
`PORTFOLIO_SNAPSHOT_MISMATCH`. Risk не пересчитывает и не изменяет snapshot.

## 5. Полный порядок проверок

Порядок стабилен; первый terminal reject определяет primary reason, остальные уже
доказанные безопасные причины могут быть добавлены в упорядоченный tuple.

1. Проверить типы и structural invariants зависимостей; programming/configuration
   defects вызывают typed exception.
2. Если `market_snapshot.is_stale` — `REJECTED: STALE_MARKET`.
3. Проверить наличие latest closed price — `MARKET_PRICE_UNAVAILABLE`.
4. Проверить intent/portfolio/snapshot/rules stream — `STREAM_MISMATCH`.
5. Проверить BTC/USDT assets и portfolio identity — `PORTFOLIO_SNAPSHOT_MISMATCH`.
6. Проверить одинаковое market/portfolio time и mark price —
   `PORTFOLIO_MARK_MISMATCH`.
7. Проверить long-only action и target exposure — `UNSUPPORTED_ACTION` или
   `INVALID_TARGET_EXPOSURE`.
8. Сравнить exposure с `max_target_exposure` — `TARGET_EXCEEDS_LIMIT`.
9. Проверить reference price по price tick — `INVALID_PRICE`.
10. Рассчитать requested target notional и raw target quantity.
11. Округлить target quantity вниз через `InstrumentRules`.
12. Рассчитать текущую quantity и signed delta к rounded target.
13. Проверить соответствие action направлению delta: EXIT без Position —
    `NO_POSITION`; отсутствие delta — `ALREADY_AT_TARGET`; конфликт —
    `ACTION_TARGET_CONFLICT`.
14. Получить absolute order quantity и rounded quote notional.
15. Проверить exact quantity step и `min_quantity` — `TARGET_TOO_SMALL` или
    `INVALID_QUANTITY`.
16. Проверить `min_notional` — `NOTIONAL_TOO_SMALL`.
17. Для BUY проверить gross notional против `quote_balance - quote_reserve` —
    `INSUFFICIENT_QUOTE`.
18. Для SELL проверить Position, base balance и order quantity — `NO_POSITION`,
    `INSUFFICIENT_BASE` или `SELL_EXCEEDS_POSITION`.
19. Для partial SELL проверить остаток: zero разрешён; positive remainder меньше
    `min_quantity` или с mark notional меньше `min_notional` — `DUST_REMAINDER`.
20. Определить `APPROVED` либо `MODIFIED`, сформировать полный audit result.

Rejection не создаёт executable parameters. Ни одна ошибка не превращается в
`FAILED` outcome: ожидаемая policy-ветка — `REJECTED`, нарушение programming/model
invariant — typed exception.

## 6. Target exposure и sizing

Все вычисления используют локальный детерминированный Decimal context и те же
precision helpers, что утверждены Portfolio Core. Float и tolerance запрещены.

```text
requested_target_notional = intent.target_exposure * portfolio.current_equity
raw_target_quantity       = requested_target_notional / reference_price
rounded_target_quantity   = rules.round_quantity_down(raw_target_quantity)
current_quantity          = portfolio.position.quantity или 0
signed_delta_quantity     = rounded_target_quantity - current_quantity
requested_order_quantity  = abs(raw_target_quantity - current_quantity)
approved_order_quantity   = abs(signed_delta_quantity)
approved_order_notional   = rules.round_quote(approved_order_quantity * reference_price)
actual_target_notional    = rounded_target_quantity * reference_price
approved_target_exposure  = actual_target_notional / portfolio.current_equity
```

При `equity == 0` положительный target отклоняется `ZERO_EQUITY`; EXIT_LONG всё ещё
может продать существующую позицию только если snapshot сам непротиворечив.

`ENTER_LONG` разрешает только положительную delta/BUY. Он не используется для
сокращения уже слишком большой позиции. `EXIT_LONG` требует target `0` и создаёт
полный SELL текущей quantity. Short, margin, leverage, borrow и negative target
запрещены.

## 7. Rounding и outcome semantics

Risk применяет только утверждённые `InstrumentRules`:

1. target notional;
2. raw target quantity;
3. target quantity rounded down to quantity step;
4. delta order quantity;
5. order notional через explicit quote rounding;
6. повторная проверка minimums, balances и dust.

Risk не округляет market price и не округляет Portfolio accounting values.

- `APPROVED`: requested target уже достижим без изменения; approved target exposure
  равен requested, а requested/approved order quantity совпадают exactly.
- `MODIFIED`: rounding либо утверждённый cap изменил target/order quantity, notional
  или фактическую exposure, но сохранил направление и безопасный исполняемый размер.
- `REJECTED`: исполнение запрещено; approved order отсутствует и есть минимум один
  typed reason.

Rounding до zero или ниже minimum — `REJECTED`, не `MODIFIED`. Нулевая delta —
`REJECTED: ALREADY_AT_TARGET`; отдельный `NO_ACTION` в RiskDecision не вводится.

## 8. RiskDecision evolution

Текущие поля и invariants сохраняются:

```text
decision_id, intent_id, decided_at_ms, outcome,
requested_target_exposure, approved_target_exposure,
rejection_reasons, applied_limits, risk_policy_version, correlation_id
```

Для безопасного execution текущей модели недостаточно. Предлагается добавить
immutable nested records:

```text
RiskSizingAudit(
    reference_price,
    portfolio_id,
    portfolio_version,
    market_snapshot_id,
    requested_target_notional,
    raw_target_quantity,
    rounded_target_quantity,
    current_quantity,
    requested_order_quantity,
)

ApprovedOrderParameters(
    side: BUY | SELL,
    quantity,
    reference_notional,
    resulting_target_quantity,
    resulting_target_exposure,
    instrument_rules_version,
    portfolio_version,
)
```

`RiskDecision` получает `sizing_audit`, optional `approved_order`, typed
`rejection_reasons` и `modification_reasons`. Для APPROVED/MODIFIED approved order
обязателен; для REJECTED отсутствует. `applied_limits` остаётся immutable snapshot
policy values для обратной совместимости и аудита. Decision не содержит `Order`,
`Fill`, mutable Portfolio или adapter payload.

`decided_at_ms == market_snapshot.as_of_ms`; `correlation_id` наследуется intent.
Decision сохраняет snapshot/rules/portfolio versions, поэтому воспроизведение не
зависит от последующего mutable lookup.

## 9. Typed reasons и exceptions

Минимальный `RiskRejectionReason`:

```text
STALE_MARKET
MARKET_PRICE_UNAVAILABLE
STREAM_MISMATCH
PORTFOLIO_SNAPSHOT_MISMATCH
PORTFOLIO_MARK_MISMATCH
UNSUPPORTED_ACTION
INVALID_TARGET_EXPOSURE
TARGET_EXCEEDS_LIMIT
ZERO_EQUITY
INVALID_PRICE
ALREADY_AT_TARGET
ACTION_TARGET_CONFLICT
TARGET_TOO_SMALL
INVALID_QUANTITY
NOTIONAL_TOO_SMALL
INSUFFICIENT_QUOTE
NO_POSITION
INSUFFICIENT_BASE
SELL_EXCEEDS_POSITION
DUST_REMAINDER
```

Минимальный `RiskModificationReason`: `QUANTITY_ROUNDED_DOWN`,
`TARGET_EXPOSURE_REDUCED`, `ORDER_NOTIONAL_CAPPED`, `POSITION_NOTIONAL_CAPPED`.
Последние два используются только после утверждения соответствующих policy fields.

Typed exceptions предназначены не для market outcomes, а для defects:
`RiskManagerError`, `RiskManagerConfigurationError`, `InvalidRiskInputError`,
`RiskInvariantError`, `RiskDecisionConstructionError`. Они содержат безопасные IDs,
expected/received values и reason, но не metadata целиком.

## 10. BUY, SELL, dust и fees

### BUY

Risk проверяет reference gross notional и консервативный all-in BUY envelope:

```text
worst_case_price = reference_price * (1 + max_slippage_bps / 10000)
worst_case_gross = quantity * worst_case_price
worst_case_fee = worst_case_gross * max_fee_rate
required_quote = worst_case_gross + worst_case_fee
required_quote <= quote_balance - min_cash_reserve
```

Это approval envelope, а не прогноз или расчёт фактической комиссии, slippage либо
fill price. Эти значения остаются ответственностью будущего Execution. Quantity
уменьшается по step до максимально доступной; `required_quote` внутри Risk не
округляется до quote precision.

### SELL и dust

SELL требует Position, `quantity == position.quantity == base_balance` и не может
создать short. `EXIT_LONG` всегда закрывает всю позицию; partial SELL в M3.4 нет.
Обычный full exit даёт `APPROVED` без `FULL_EXIT` modification reason. Если полная
позиция ниже `min_quantity` либо `min_notional`, paper risk contour разрешает её
полную ликвидацию как `MODIFIED` с `DUST_LIQUIDATION`. SELL не использует BUY
`required_quote`: значение равно нулю.

### Fees

Risk не вычисляет и не записывает fees. Fee, slippage и fill price принадлежат
Execution. Risk audit фиксирует reference values, а будущий execution обязан
отклонить результат, который нарушил бы approved quantity или Portfolio balance.

## 11. Determinism, immutability и ownership

- Все входы и результаты immutable; mutable references не возвращаются.
- Risk Manager stateless и thread-safe при stateless injected ID factory; первая
  composition всё равно остаётся single-threaded.
- Нет I/O, clock, cache, retries, lifecycle, close или global mutable state.
- Composition root владеет policy, rules и factories.
- Risk не резервирует Portfolio. Между decision и future execution возможен stale
  portfolio version; execution обязан проверять exact approved version.
- Повторная evaluate с одинаковыми semantic inputs детерминирована, но execution
  idempotency не обеспечивается Risk Manager.

## 12. Future risk extensions

Additive versioned policies могут добавить:

- daily realised loss limit;
- maximum drawdown и kill switch;
- maximum target/order/position exposure;
- per-symbol и aggregate multi-symbol limits;
- volatility regime limits;
- trade frequency/cooldown;
- portfolio reservations/locking;
- persistent decisions и recovery.

Leverage, margin, borrow, futures и short требуют отдельного domain design и ADR;
они не являются простым увеличением текущего exposure range.

## 13. Future test plan

### Models and configuration

- immutable policy, sizing audit, approved parameters и decision;
- Decimal-only, non-finite/float rejection, invalid bounds/versions/assets;
- defensive metadata и injected deterministic IDs;
- APPROVED/MODIFIED/REJECTED cross-field invariants; отсутствие FAILED.

### Input gates

- stale snapshot; no latest closed price; stream mismatch каждого входа;
- portfolio/rules asset mismatch; portfolio time/mark mismatch;
- invalid price tick; zero equity; unsupported action/target conflict;
- typed exceptions только для malformed/programming inputs.

### BUY

- exact approved BUY;
- rounding создаёт MODIFIED quantity/exposure;
- rounding to zero; minimum quantity/notional;
- insufficient quote и quote reserve boundary;
- already at target; current position below target;
- target exposure above policy limit.

### SELL

- approved full EXIT_LONG;
- no position; insufficient base; exceeds position;
- partial reduction action conflict;
- exact zero remainder; valid positive remainder;
- min-quantity и min-notional dust rejection;
- существующий untradable dust.

### Determinism and boundaries

- ручные Decimal sizing examples и global-context independence;
- repeated inputs/factory outputs produce equal results;
- no mutation of input snapshots;
- no Binance/cache/events/polling/execution/shadow/agent/database/files/time/random
  imports через AST boundaries;
- Risk не создаёт Order/Fill и не вызывает Portfolio methods.

## 14. Утверждённые решения M3.4 и открытые вопросы

1. BUY использует approved all-in worst-case budget: slippage envelope плюс
   максимальная fee rate; Risk не предсказывает фактический fill.
2. Max target/order/position limits безопасно уменьшают quantity и дают `MODIFIED`;
   невыполнимый после cap ордер отклоняется.
3. `EXIT_LONG` всегда означает полный выход. Полная ликвидация существующего dust
   ниже minimum разрешена только в paper risk contour, отмечается
   `DUST_LIQUIDATION` и никогда не превращается в partial SELL.
4. Нужна ли отдельная action `REDUCE_LONG`; текущий Planner создаёт только
   ENTER_LONG/EXIT_LONG.
5. Должен ли application слой mark-to-market Portfolio перед каждым evaluate либо
   передавать единый `RiskMarketContext`? Рекомендация M3.4: явный pre-risk mark и
   строгая equality проверка.
6. Нужно ли сохранять `approved_target_exposure` как точный фактический ratio либо
   как policy target до rounding? Рекомендация: фактический ratio, исходная цель уже
   хранится отдельно.
7. Как версионировать reason enums и сериализацию после появления persistence?
8. Нужен ли bounded decision dedup до execution? Рекомендация: idempotency находится
   в application/execution слое, Risk остаётся stateless.

## 15. Итоговые гарантии реализации

### M4.4 additive exchange risk hardening

M4.4 не меняет Stage 3 sizing и `RiskDecision`. После него вызывается pure
`evaluate_exchange_risk`, которому явно передаются reconciled account state,
`PendingExposure`, `ExecutionSession` и immutable `ExchangeRiskPolicy`. Результат
`ExchangeRiskAssessment` отделён от `ExchangeSubmissionDecision`: финансовое разрешение
само по себе не даёт права на network submission.

Effective resources учитывают remaining reservations: для BUY это quote balance минус
reserved quote и cash reserve, для SELL — base balance минус reserved base. Projected
BUY position включает remaining pending BUY и candidate; SELL вычитает remaining pending
SELL и candidate и fail-closed запрещает short. Первая policy фиксирует максимум один
non-terminal order и один in-flight submission.

Session controls ограничивают cumulative submitted notional, conservative projected
turnover, submission count и execution failure count. Это operator-started session без
calendar/daily reset. Exact equality допустима для upper notional/count limits;
failure-count и drawdown thresholds блокируют на equality. Realised-loss limit пока не
реализован: существующий domain не содержит однозначного immutable session-loss baseline,
поэтому он должен появиться отдельным reviewed contract, а не выводиться из process
clock или неявного состояния.

Submission safety требует TESTNET, approved financial assessment, cleared kill switch,
SAFE reconciliation, совпадающие reconciliation/control generations, настоящий opaque
process-local capability и safe pending control. Все обычные отказы представлены
ordered typed reasons; exceptions оставлены только для повреждённых контрактов. Обе
функции stateless, Decimal-only и не используют network, filesystem, SQLite или clock.

M4.4 final safety review пройден 2026-08-10. Review отдельно доказал exact-equality
resource/session boundaries, remaining-only partial-fill reservations, deterministic
multi-record aggregation до one-order hard gate, source Risk outcome preservation,
assessment/session lineage, kill-switch generation invalidation и immutable applied-policy
audit. Stage 3 paper Risk semantics не изменены.

M3.4 реализован изолированно и прошёл final safety review:

1. immutable policy/reason/sizing models и typed exceptions;
2. эволюция RiskDecision без удаления текущих полей;
3. pure Decimal sizing и ordered checks;
4. unit/property tests и AST boundary tests;
5. offline integration `OrderIntent + PortfolioSnapshot + MarketSnapshot`;
6. без runtime, execution, Fill, persistence и расчёта фактической комиссии.

Следующий milestone после final review — M3.5 Deterministic Paper Execution.

Подтверждено тестами:

- BUY резервирует точный worst-case envelope без округления `required_quote`;
- exposure, position, order и quote-budget caps применяются детерминированно;
- affordability boundary разрешает точное равенство и уменьшает quantity по step;
- approved exposure вычисляется от итоговой позиции, а не от размера order;
- `EXIT_LONG` всегда закрывает всю позицию и не создаёт partial reduction;
- обычный full exit даёт `APPROVED`, dust liquidation — `MODIFIED`;
- Risk не изменяет Portfolio и не выполняет Order/Fill;
- все финансовые вычисления используют Decimal и не зависят от global context.
