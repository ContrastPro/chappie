# Trading Decision Pipeline Design

## 1. Статус и область

- Статус: M2.4 implemented; final review pending.
- Дата: 2026-08-04.
- Основание: ADR-019, ADR-020 и `TRADING_DOMAIN.md`.

Документ проектирует синхронную application orchestration boundary:

```text
Market Cache / Market Events
    -> Snapshot Factory
    -> MarketSnapshot
    -> TradingDecisionPipeline
         -> Strategy
         -> Signal
         -> IntentPlanner
         -> OrderIntent | None
```

M2.3 заканчивается на `OrderIntent`. Pipeline не читает Market Cache, не получает
данные от Binance, не выполняет I/O, не вызывает Risk Manager или
`ExecutionProvider`, не читает portfolio и не отправляет ордера. В будущем:

```text
OrderIntent -> Risk Manager -> RiskDecision -> Execution
```

## 2. Граница ответственности

`TradingDecisionPipeline` получает готовый immutable `MarketSnapshot` и только
координирует заменяемые Strategy и Intent Planner. Он не содержит EMA, sizing или
risk formulas. Pipeline синхронный, stateless, reusable, не читает время и не имеет
global mutable state.

Один instance связывается ровно с одной Strategy и одним Intent Planner. Для другой
стратегии создаётся другой instance. Voting, ensembles, priorities, parallel
evaluation и conflict resolution принадлежат будущему multi-strategy coordinator.

## 3. Рекомендуемый public API

```text
TradingDecisionPipeline(
    strategy: Strategy,
    intent_planner: IntentPlanner,
    pipeline_run_id_factory: PipelineRunIdFactory,
)

evaluate(snapshot: MarketSnapshot) -> DecisionPipelineResult
```

Logger не входит в доменный/application contract. Внешняя orchestration сможет
логировать immutable result или типизированную ошибку. Constructor проверяет
runtime Protocol collaborators; согласованность фактических результатов и
configuration проверяется во время `evaluate`.

Pipeline mutable collaborators ограничены injected ID factories внутри владельцев.
Он не кэширует snapshot, Signal, result или processed IDs.

## 4. DecisionPipelineResult

```text
PipelineOutcome = HOLD | INTENT_CREATED | PLANNER_DECLINED

completed_stages = ("snapshot_validated",
                    "strategy_evaluated",
                    "intent_planned"[, "intent_created"])
```

`FAILED` не является outcome. Invalid models, invariant violations, factory failures
и unexpected exceptions поднимаются как типизированные исключения. Возврат FAILED
без полноценного результата маскировал бы дефект обычным domain outcome.

Минимальная immutable модель:

| Поле | Тип | Семантика |
|---|---|---|
| `pipeline_run_id` | `str` | ID одного вычисления |
| `snapshot_id` | `str` | Аудируемый вход |
| `stream_key` | `MarketStreamKey` | Поток решения |
| `evaluated_at_ms` | `int` | Равно `snapshot.as_of_ms` |
| `strategy_name/version` | `str` | Фактически вызванная Strategy |
| `signal` | `Signal` | Полный immutable result Strategy |
| `order_intent` | `OrderIntent | None` | Результат planner |
| `outcome` | `PipelineOutcome` | Успешная семантика пути |
| `reason_code` | `str` | Signal/planner reason |
| `correlation_id` | `str | None` | Существующая market correlation |
| `completed_stages` | `tuple[str, ...]` | Подтверждённые шаги по порядку |
| `metadata` | immutable mapping | Версии planner и безопасные audit values |

Инварианты:

- `HOLD`: Signal имеет HOLD, intent отсутствует, reason соответствует Signal;
- `INTENT_CREATED`: Signal BUY/SELL, intent присутствует, planner outcome CREATED;
- `PLANNER_DECLINED`: Signal BUY/SELL, intent отсутствует, planner outcome DECLINED;
- HOLD никогда не превращается в `PLANNER_DECLINED`;
- stages строго следуют порядку pipeline, всегда содержат первые три значения и
  содержат `intent_created` только для `INTENT_CREATED`;
- result содержит defensive immutable nested models и metadata.

## 5. Intent Planner API

Рекомендуется типизированный API:

```text
IntentPlanner
  name: str
  version: str
  configuration: immutable typed config

  plan(signal: Signal) -> IntentPlanResult
```

`create_intent(signal) -> OrderIntent | None` короче, но `None` не отличает HOLD от
осознанного planner decline и не несёт audit reason. Поэтому выбран
`IntentPlanResult`.

Planner pure/deterministic, не читает portfolio, balances, Binance filters или
clock, не вычисляет quantity, не вызывает risk/execution и использует только Signal,
immutable config и IntentIdFactory.

## 6. LongOnlyIntentPlanner policy

Первая Spot long-only policy:

```text
BUY  -> ENTER_LONG, target_exposure = configured_long_target
SELL -> EXIT_LONG,  target_exposure = Decimal("0")
HOLD -> no intent
```

Immutable configuration:

- `long_target_exposure: Decimal = Decimal("1")`;
- `planner_name = "long_only_target_exposure"`;
- `planner_version = "1.0.0"`.

`long_target_exposure` должен быть finite Decimal и удовлетворять
`0 < value <= 1`. Float, NaN и infinity запрещены.

BUY/SELL здесь не означают немедленную сделку. Intent описывает только желаемую
экспозицию. Planner пока не знает, открыта ли позиция, и не подавляет повторное
ENTER/EXIT. Risk Manager позднее может APPROVE, MODIFY или REJECT; quantity и
instrument rounding рассчитываются после появления portfolio/instrument contracts.

## 7. IntentPlanResult

```text
IntentPlanOutcome = CREATED | NO_INTENT_FOR_HOLD | DECLINED
```

Immutable поля:

- `outcome`;
- `signal_id`;
- `intent: OrderIntent | None`;
- `reason_code`;
- `planner_name`;
- `planner_version`.

Точные инварианты:

- CREATED требует BUY/SELL Signal и intent;
- NO_INTENT_FOR_HOLD требует HOLD Signal и отсутствие intent;
- DECLINED требует BUY/SELL Signal, отсутствие intent и явный planner reason;
- BUY может создать только ENTER_LONG;
- SELL может создать только EXIT_LONG;
- `intent.source_signal_id == signal.signal_id`;
- intent сохраняет `stream_key`, strategy name/version и correlation ID Signal;
- `OrderIntent.created_at_ms = Signal.effective_at_ms`;
- HOLD path не вызывает IntentIdFactory.

DECLINED предусмотрен для стабильного расширения API, хотя первая long-only policy
его обычно не создаёт. Unsupported или structurally invalid Signal является
exception, а не DECLINED.

## 8. Pipeline ordering и проверки

`evaluate` выполняет шаги строго последовательно:

1. Проверить тип и invariants MarketSnapshot.
2. Получить `pipeline_run_id`.
3. Вызвать `Strategy.generate_signal(snapshot)`.
4. Проверить Signal относительно snapshot и Strategy.
5. Вызвать `IntentPlanner.plan(signal)`.
6. Проверить IntentPlanResult и вложенный OrderIntent.
7. Собрать immutable DecisionPipelineResult.

Проверки шага 4:

- `signal.snapshot_id == snapshot.snapshot_id`;
- `signal.stream_key == snapshot.stream_key`;
- name/version совпадают с вызванной Strategy;
- `created_at_ms == effective_at_ms == snapshot.as_of_ms`;
- correlation ID не заменён.

Проверки шага 6:

- result относится к `signal.signal_id`;
- planner name/version совпадают с вызванным Planner;
- intent identity, stream, strategy version, correlation и timestamp сохранены;
- direction/action mapping соответствует разделу 6;
- HOLD не содержит intent и не вызывает IntentIdFactory.

Pipeline не продолжает выполнение после ошибки Strategy и не вызывает Planner.
После ошибки Planner result не создаётся.

## 9. IDs, correlation и audit

Factories остаются рядом с владельцами типов:

- `SignalIdFactory` — strategy layer;
- `IntentIdFactory` — `app.trading.intent`;
- `PipelineRunIdFactory` — `app.trading.pipeline`.

Production factories могут использовать UUID; тесты используют deterministic
sequences. Общий `app/common/ids.py` сейчас не создаётся: одинаковая сигнатура ещё не
доказывает единый lifecycle/ownership, а преждевременная централизация связывает
независимые модели.

Audit chain:

```text
MarketSnapshot.snapshot_id
  -> Signal.snapshot_id
  -> Signal.signal_id
  -> OrderIntent.source_signal_id
  -> DecisionPipelineResult.pipeline_run_id
```

Result должен сохранять snapshot/stream, strategy/planner versions, Signal,
OrderIntent, reason, evaluated market time, correlation и completed stages.
`pipeline_run_id` обозначает вычисление и не заменяет market `correlation_id`.

## 10. Determinism и idempotency

Determinism означает: одинаковые snapshot/config и одинаковые deterministic factory
sequences дают одинаковый result. Повторный evaluate с production UUID factory может
создать новые `pipeline_run_id`, `signal_id` и `intent_id`, но semantic direction,
target exposure, reasons и versions остаются теми же.

Это **не execution idempotency**. Stateless pipeline не хранит processed IDs и не
должен получать скрытый in-memory dedupe cache. Будущая защита от повторной сделки
потребует durable decision record и idempotency key, например:

```text
strategy_name + strategy_version + snapshot_id +
signal_direction + planner_name + planner_version
```

Формат ключа и persistence не реализуются в M2.3/M2.4.

## 11. Error policy

Предлагаемая иерархия:

```text
TradingPipelineError
├── InvalidPipelineConfigurationError
├── PipelineInvariantError
├── StrategyEvaluationError
├── IntentPlanningError
│   └── UnsupportedSignalError
├── PipelineRunIdFactoryError
└── IntentFactoryError
```

- HOLD, stale HOLD и insufficient-history HOLD — обычные результаты;
- invalid snapshot/Signal/result — invariant exception;
- configuration errors обнаруживаются при construction;
- factory failure — exception с сохранённой причиной;
- unexpected Strategy error оборачивается в StrategyEvaluationError с cause;
- unexpected Planner error — IntentPlanningError с cause;
- pipeline не превращает unexpected failure в HOLD, DECLINED или FAILED result;
- retry отсутствует: pipeline не является I/O component.

Ошибка ID factory не оставляет pipeline/planner в изменённом состоянии. Единственное
допустимое mutable состояние находится внутри injected factories и принадлежит им.

## 12. Module structure

Минимальная структура M2.4:

```text
app/trading/
    intent.py
        IntentPlanner Protocol
        LongOnlyIntentPlannerConfig
        IntentPlanResult / IntentPlanOutcome
        LongOnlyIntentPlanner
        IntentIdFactory / UUIDIntentIdFactory

    pipeline.py
        TradingDecisionPipeline
        DecisionPipelineResult / PipelineOutcome
        PipelineRunIdFactory / UUIDPipelineRunIdFactory
        pipeline errors или их расширение в exceptions.py
```

Новые мелкие packages и общий ID module не нужны. Existing domain models и Strategy
Protocol остаются без инфраструктурных зависимостей.

## 13. Testing strategy для M2.4

Intent Planner:

- BUY → ENTER_LONG; SELL → EXIT_LONG; HOLD → no intent;
- default/custom target exposure и invalid Decimal values;
- deterministic IDs; HOLD не вызывает ID factory;
- Signal не изменяется; correlation и timestamps сохраняются;
- incompatible direction/action и malformed result отклоняются.

Pipeline:

- HOLD, BUY, SELL и planner-declined paths;
- Strategy/Planner/factory exceptions и сохранение cause;
- Planner не вызывается после Strategy failure;
- invalid snapshot↔Signal и Signal↔Intent relationships;
- deterministic semantics и stateless repeated calls;
- reuse для разных streams; отсутствие hidden time/I/O;
- immutable result и nested values;
- точный ordering и completed stages.

Contract tests подтверждают, что EMA strategy соответствует Strategy, а long-only
planner — IntentPlanner. Реальная сеть, cache, database и execution не используются.

## 14. Открытые вопросы

1. Нужны ли дополнительные DECLINED reasons до position-aware planner.
2. Должен ли future planner принимать PortfolioSnapshot или отдельный allocation
   context; текущий planner остаётся position-unaware.
3. Нужны ли target exposure меньше `1` как production default; M2.4 default — `1`,
   но это не risk approval.
4. Утвердить canonical future idempotency key и место durable audit persistence.
5. Нужен ли application-level structured logging wrapper вокруг immutable result.
6. Где именно Risk Manager расширит pipeline на M2.5+ и кто будет владельцем полного
   decision transaction.
7. Multi-strategy coordination остаётся отдельным design после одного pipeline на
   Strategy.

## 15. Минимальный implementation plan M2.4

1. Утвердить contracts и error policy этого документа.
2. Реализовать `intent.py`: frozen config/result, protocols, factories и long-only
   planner без portfolio/risk/execution.
3. Реализовать `pipeline.py`: frozen result, ordering и cross-model invariants.
4. Добавить deterministic unit/contract tests из раздела 13.
5. Проверить dependency boundary и обязательные Ruff/pytest checks.
6. Не подключать pipeline к TraderAgent/Market Events до отдельного integration
   review и design механизма вызова на закрытии свечи.
