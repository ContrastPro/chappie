# Chappie Trader Architecture

## Design Principles

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
  execution/      Зарезервировано для исполнения сделок
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

Минимальный design proposal и открытые вопросы описаны в `MARKET_CACHE_DESIGN.md`.

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

`MarketProvider` создаётся и закрывается composition root. Polling Engine использует внедрённый provider, но не владеет его ресурсом и не закрывает его самостоятельно. Детальный lifecycle, state machine и retry policy описаны в `POLLING_ENGINE_DESIGN.md`.

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

Минимальный event model, точная семантика `CandleClosed` и delivery policy описаны в `MARKET_EVENTS_DESIGN.md`.

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
`SHADOW_DECISION_ENGINE_DESIGN.md`.

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

```text
OrderIntent + PortfolioSnapshot + MarketSnapshot
    -> Risk Manager
    -> APPROVED | MODIFIED | REJECTED RiskDecision
    -> future Execution
```

Target exposure преобразуется в target position через Portfolio equity и latest
closed reference price, затем в delta order с применением InstrumentRules. Полный
контракт, порядок gates, dust и fee boundary описаны в `RISK_MANAGER_DESIGN.md`.

### `execution`

Преобразует разрешённое RiskDecision намерение в `Order`/`Fill`. Первый adapter —
детерминированный synchronous paper execution без сети; он не изменяет Portfolio.
Testnet и live adapters требуют отдельных design review и допуска.

Первая версия получает только готовый `RiskDecision`, immutable execution config и
`InstrumentRules`; она не читает Market Cache и не знает Risk Policy. `APPROVED` и
`MODIFIED` исполняются сразу одним существующим immutable `Fill` по reference price,
с нулевым slippage и explicit Decimal fee. Время fill выводится из decision timestamp
и configured deterministic latency без clock, sleep или random. Подробный контракт
описан в `PAPER_EXECUTION_PROVIDER_DESIGN.md`.

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
Cache и не знает Binance. Контракт описан в `PAPER_TRADING_ORCHESTRATION_DESIGN.md`.

PaperTradingEngine является синхронным single-writer application service без
start/stop lifecycle, background thread, сети и persistence. Composition root владеет
Portfolio, RiskManager, PaperExecutionProvider, factories и observer; engine владеет
только bounded process-local dedup и operational status. После неоднозначной ошибки
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
`PAPER_TRADING_CORE_DESIGN.md`, `RISK_MANAGER_DESIGN.md` и
`PAPER_EXECUTION_PROVIDER_DESIGN.md`. Portfolio, Risk и Paper Execution реализованы;
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
8. В shadow mode `CandleClosed` вызывает Shadow Trading Decision Engine, который читает Market Cache и строит `MarketSnapshot` с временем и correlation события.
9. Strategy формирует только immutable `Signal` из `MarketSnapshot`.
10. Application-level `TradingDecisionPipeline` проверяет Signal и координирует отдельный deterministic Intent Planner, который формирует `OrderIntent` или типизированно не создаёт его.
11. Shadow Engine формирует immutable record и передаёт его observer-у; на этом shadow flow заканчивается без risk и execution.
12. В paper-контуре application orchestration предварительно согласует Portfolio valuation с MarketSnapshot и передаёт `OrderIntent`, immutable Portfolio snapshot и MarketSnapshot обязательному Risk Manager.
13. Execution симулирует только параметры, разрешённые `RiskDecision`, и создаёт immutable `Order`/`Fill`.
14. Portfolio атомарно и идемпотентно применяет Fill; execution не меняет его напрямую.
15. PaperTradingEngine по утверждённому M3.8 контракту формирует единый immutable cycle result и compact
    observer record; HOLD и declined cycles обновляют только valuation.
16. Каждое решение и изменение состояния записывается для аудита и аналитики.

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

Переход между режимами должен менять инфраструктурные адаптеры, но не бизнес-правила стратегии и риска.

Live-режим должен быть запрещён по умолчанию и требовать явного подтверждения, валидной конфигурации и прохождения эксплуатационных критериев.

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
