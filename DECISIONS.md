# Chappie Trader Architecture Decisions

Этот документ фиксирует принятые архитектурные и процессные решения. Все приведённые решения действуют с даты `2026-08-03`, пока не заменены новым явно зафиксированным решением.

## ADR-001: Decimal для финансовых значений

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Цены, объёмы, комиссии, балансы и PnL требуют предсказуемой десятичной арифметики. Двоичное представление `float` создаёт ошибки округления, неприемлемые для финансового учёта.
- **Решение:** Использовать `Decimal`, а не `float`, для всех финансовых значений и явно применять правила точности и округления торгового инструмента.
- **Причины:** Точность десятичной арифметики, воспроизводимость расчётов и возможность корректно соблюдать биржевые фильтры.
- **Последствия:** Внешние данные преобразуются в `Decimal` на границе системы. Смешивание с `float` не допускается. Необходимо определить единые правила округления и сериализации.

## ADR-002: Dependency injection

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Trading Agent должен работать с реальными, тестовыми и replay-реализациями внешних зависимостей без изменения бизнес-логики.
- **Решение:** Передавать зависимости компонентам извне; сборку конкретных реализаций выполнять в composition root.
- **Причины:** Изоляция бизнес-логики, тестируемость, заменяемость адаптеров и явное управление жизненным циклом ресурсов.
- **Последствия:** Компоненты не создают внутри себя HTTP-клиенты, базы данных или биржевые адаптеры. Конструкторы и application services получают зависимости через типизированные контракты.

## ADR-003: MarketProvider как порт

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Рыночные данные могут поступать от Binance, другой биржи, fake-объекта или исторического replay-источника.
- **Решение:** Использовать `MarketProvider` как независимый от инфраструктуры порт получения нормализованных рыночных данных.
- **Причины:** Единый контракт, контрактное тестирование и отсутствие зависимости Trading Agent от формата конкретной биржи.
- **Последствия:** Каждая реализация обязана полностью соответствовать порту и проходить общий набор контрактных тестов. Биржевые payload не должны распространяться за пределы адаптера.

## ADR-004: BinanceMarketProvider как адаптер

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Первым источником публичных рыночных данных выбран Binance, но Binance не должен определять внутреннюю бизнес-модель проекта.
- **Решение:** Реализовать Binance через `BinanceMarketProvider`, являющийся инфраструктурным адаптером порта `MarketProvider`.
- **Причины:** Локализация HTTP API, нормализации ответов и обработки специфичных ошибок Binance.
- **Последствия:** Изменения Binance API ограничиваются адаптером. Адаптер преобразует внешние значения в доменные модели и ошибки. Его соответствие полному контракту должно быть завершено и протестировано.

## ADR-005: Paper mode по умолчанию

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Ошибки конфигурации, стратегии или исполнения не должны случайно подвергать реальный капитал риску.
- **Решение:** Использовать `paper` как режим по умолчанию. Testnet и live требуют явной конфигурации, а live — дополнительных защитных условий и отдельного допуска.
- **Причины:** Безопасные значения по умолчанию, снижение операционного риска и возможность проверять полный pipeline без реальных сделок.
- **Последствия:** Отсутствие или ошибка конфигурации не должны включать live execution. Одного строкового значения `live` недостаточно для допуска к реальным ордерам.

## ADR-006: AI не имеет прямого доступа к execution

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** AI-компонент может быть вероятностным, нестабильным, ошибаться или генерировать результат, выходящий за ожидаемые ограничения.
- **Решение:** AI может формировать анализ, сигнал или типизированный `OrderIntent`, но никогда не вызывает execution adapter и не отправляет ордер напрямую. Любое намерение обязательно проходит детерминированный Risk Manager.
- **Причины:** Ограничение ущерба, объяснимость, тестируемость, аудит и централизованное применение торговых лимитов.
- **Последствия:** Между AI и execution существует жёсткая архитектурная граница. Для каждого AI-решения сохраняются входные данные, версия модели, конфигурация, проверки риска и итоговое действие.

## ADR-007: Ruff и pytest обязательны перед коммитом

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Раннее обнаружение дефектов и единый стандарт качества дешевле исправления проблем после объединения изменений.
- **Решение:** Перед каждым коммитом запускать Ruff и полный актуальный набор pytest. Коммит с известными ошибками этих проверок не считается готовым.
- **Причины:** Стабильный стиль, обнаружение простых дефектов и защита уже проверенного поведения.
- **Последствия:** Изменение должно включать необходимые тесты. Временное исключение возможно только как явно документированное решение с причиной и планом устранения.

## ADR-008: Один завершённый этап — один отдельный коммит

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Смешивание нескольких незавершённых этапов затрудняет review, поиск регрессии и безопасный откат.
- **Решение:** Каждый завершённый этап разработки оформлять отдельным атомарным коммитом после прохождения обязательных проверок.
- **Причины:** Понятная история проекта, удобное review, bisect и возможность отката законченной единицы работы.
- **Последствия:** Коммит должен иметь одну ясную цель и не включать несвязанные изменения. Документационный этап может быть отдельным коммитом, но сам коммит создаётся только по явному решению пользователя.

## ADR-009: Модульный монолит вместо микросервисов

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Проект находится на ранней стадии, команда и эксплуатационные требования ещё не оправдывают распределённость.
- **Решение:** Развивать Chappie Trader как модульный монолит с явными портами, адаптерами и границами предметных модулей.
- **Причины:** Более простые разработка, тестирование, транзакционность, отладка и развёртывание при сохранении возможности дальнейшего разделения.
- **Последствия:** Границы модулей должны соблюдаться внутри одного приложения. Микросервисы рассматриваются только при появлении измеримых требований к независимому масштабированию, отказоустойчивости или владению компонентами.

## ADR-010: Binance Spot как первый рынок

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Для построения первого сквозного торгового контура необходим один конкретный рынок с доступными публичными данными и testnet-интеграцией. Одновременная поддержка нескольких сегментов увеличит объём правил инструментов, исполнения и риска.
- **Решение:** Использовать Binance Spot как первый рынок Chappie Trader. Futures, margin и другие биржи не входят в начальный контур.
- **Причины:** Ограниченный объём первой реализации, соответствие существующему адаптеру Binance и отсутствие производных инструментов и плеча на первом этапе.
- **Последствия:** Первые модели, адаптеры и тестовые сценарии проверяются на правилах Spot. Бизнес-логика сохраняет независимость от Binance, а расширение на другой рынок потребует отдельного адаптера и review его специфических рисков.

## ADR-011: BTCUSDT как первый инструмент эксплуатации

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Мультиинструментальная работа требует дополнительного управления подписками, капиталом, корреляцией и конкурентным исполнением.
- **Решение:** Ограничить первый эксплуатационный pipeline инструментом `BTCUSDT`.
- **Причины:** Один ликвидный инструмент упрощает проверку данных, стратегии, округления, учёта и восстановления состояния.
- **Последствия:** Начальные критерии качества и сценарии строятся для `BTCUSDT`. Архитектура не должна жёстко встраивать этот символ в доменную логику; добавление инструментов потребует отдельного этапа проверки портфельного риска.

## ADR-012: Синхронный REST polling как первый market loop

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Первый контур работает с одним инструментом и не имеет подтверждённых требований, оправдывающих сложность конкурентной асинхронной обработки и WebSocket-соединений.
- **Решение:** Реализовать первый market loop как синхронный REST polling. `asyncio` и WebSocket отложить до отдельного ADR.
- **Причины:** Простота управления потоком, детерминированное поведение, более лёгкое тестирование и достаточность для начальной частоты стратегии.
- **Последствия:** Необходимо определить частоту polling, обработку задержек, rate limits, пропусков и graceful shutdown. Переход на `asyncio` или WebSocket допускается после измерения ограничений и отдельного архитектурного решения.

## ADR-013: SQLite как начальное хранилище

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Агенту потребуется локально хранить состояние портфеля, audit trail и результаты экспериментов, но на первом этапе нет требований к распределённому доступу или внешнему серверу базы данных.
- **Решение:** Использовать SQLite как начальное хранилище за типизированным persistence port.
- **Причины:** Простое развёртывание, транзакционность, переносимость и достаточность для одного процесса и первого инструмента.
- **Последствия:** Домен не зависит от SQLite или SQL-схемы. Необходимо определить миграции, резервное копирование, режим транзакций и восстановление. При появлении требований к конкурентной записи или масштабированию замена хранилища оформляется отдельным ADR.

## ADR-014: Первая ограниченная роль AI

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Польза и устойчивость AI-компонента ещё не подтверждены, а его непосредственное влияние на торговые решения увеличивает риск и усложняет воспроизводимость.
- **Решение:** На первом AI-этапе использовать AI только для аналитики, объяснения и классификации рыночного режима в shadow mode. Его выводы не изменяют ордера, позиции или execution.
- **Причины:** Возможность оценить качество AI на реальных наблюдениях без риска для капитала и без нарушения детерминированного baseline pipeline.
- **Последствия:** AI-выводы сохраняются вместе с входами, версией модели и последующим исходом для сравнительного анализа. Любое расширение влияния AI требует отдельного ADR, доказательств качества и сохранения обязательного Risk Manager.

## ADR-015: Live trading как отдельный шлюз допуска

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Успешный backtest или календарно длительный paper-run не доказывают эксплуатационную безопасность и не исключают критические ошибки исполнения.
- **Решение:** Сохранить live trading как долгосрочную цель, доступную только после отдельного review и явного допуска. Допуск определяется утверждёнными критериями качества, достаточным количеством наблюдений и отсутствием критических ошибок, а не только продолжительностью тестирования.
- **Причины:** Защита капитала, необходимость оценивать качество данных, риск, исполнение, восстановление и наблюдаемость как единую систему.
- **Последствия:** Прохождение backtest, paper и testnet обязательно, но не даёт автоматического live-доступа. Перед допуском фиксируются выборка наблюдений, пороги метрик, перечень критических ошибок, лимиты капитала, kill switch, reconciliation и процедура ручного вмешательства.

## ADR-016: Market Cache as the internal source of market data

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Если стратегии, индикаторы, risk, analytics и будущие AI-модули самостоятельно обращаются к `MarketProvider`, они зависят от внешней инфраструктуры, могут видеть разные срезы рынка и дублируют получение и проверку данных. Для одинакового поведения backtest, paper, testnet и live необходима единая внутренняя граница чтения нормализованных данных.
- **Решение:** Использовать Market Cache как единственный внутренний источник рыночных данных. `MarketProvider` доступен только market ingestion слою. После получения, преобразования и независимой валидации данные помещаются в Market Cache. Стратегии, индикаторы, risk, analytics и AI читают нормализованные данные только через единый интерфейс Market Cache во всех режимах работы.
- **Причины:** Единый согласованный срез данных, независимость потребителей от биржи и транспорта, воспроизводимость, упрощённое тестирование и одинаковый read path для live-данных и исторического replay.
- **Последствия:** Market Cache не содержит торговой логики, не выполняет сетевые запросы, retries или polling и не зависит от Binance. Ingestion отвечает за получение и валидацию, cache — только за ограниченное хранение и детерминированное чтение нормализованных данных. Детальный минимальный контракт зафиксирован в `MARKET_CACHE_DESIGN.md`.

## ADR-017: Synchronous REST Polling as the first market ingestion model

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** Chappie Trader требуется первый долгоживущий ingestion-процесс для одного Binance Spot потока `BTCUSDT`. Подтверждённых требований к конкурентной обработке, WebSocket или нескольким writer пока нет, а доменным компонентам нужны явные и воспроизводимые временные значения.
- **Решение:** Первая версия market ingestion использует синхронный REST polling. Polling Engine является единственным writer в Market Cache, отвечает за расписание, retries/backoff и graceful stop, получает данные через `MarketProvider`, передаёт их в Validation и только затем записывает в Market Cache. `asyncio` и WebSocket откладываются до отдельного ADR. Wall-clock и ожидание предоставляются engine через тестируемые зависимости; доменные компоненты не читают системное время скрыто.
- **Причины:** Простая модель жизненного цикла, детерминированный порядок записи, соответствие текущему синхронному провайдеру, воспроизводимые тесты и отсутствие преждевременной конкурентности.
- **Последствия:** `run_forever()` блокирует вызывающий поток, а остановка выполняется кооперативно. Polling Engine не содержит стратегию, risk или execution и не вызывается ими напрямую. Первая реализация обслуживает один `symbol + interval`; расширение на `asyncio`, WebSocket или несколько конкурентных потоков требует отдельного решения. Детальный proposal зафиксирован в `POLLING_ENGINE_DESIGN.md`.

## ADR-018: Synchronous in-process market events for the first implementation

- **Статус:** принято
- **Дата:** 2026-08-03
- **Контекст:** После подтверждённого обновления Market Cache будущим indicators, strategies, analytics и AI нужен единый способ узнать об изменении нормализованного рыночного состояния, не обращаясь к `MarketProvider` и не связываясь с Polling Engine.
- **Решение:** Первая версия использует синхронные in-process Market Events и один Event Dispatcher. `asyncio`, message broker и distributed delivery не применяются. Cache-события создаются только после успешного атомарного cache commit или успешной идемпотентной reconciliation, подтвердившей переход свечи в закрытое состояние. Порядок публикации соответствует порядку подтверждённых изменений cache. Обработчики получают immutable snapshot-данные и не могут изменить cache через ссылки.
- **Причины:** Детерминированный порядок, простое тестирование, соответствие одному процессу и writer, отсутствие преждевременной распределённой инфраструктуры и единая граница для внутренних потребителей.
- **Последствия:** Доставка не означает durable storage и не гарантируется после перезапуска процесса; durable event log будет отдельной частью persistence/audit. Dispatcher вызывает всех подходящих handlers, собирает типизированные ошибки и не откатывает уже подтверждённый cache commit. Ошибка одного handler не должна незаметно останавливать остальных. Event Dispatcher не зависит от Binance, HTTP, Polling Engine internals или execution. Детальный контракт зафиксирован в `MARKET_EVENTS_DESIGN.md`.

## ADR-019: Signals are separated from order intents

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Рыночное мнение, распределение капитала, контроль риска и исполнение имеют разные требования к изменениям и аудиту. Их объединение позволило бы стратегии или AI задавать исполняемый ордер в обход централизованных политик.
- **Решение:** Strategy создаёт только immutable `Signal`. Отдельный deterministic Intent Planner позднее создаёт `OrderIntent`. Только Risk Manager создаёт `RiskDecision`. Ни Strategy, ни AI не формируют финальный executable order напрямую.
- **Причины:** Разделение анализа, sizing, риска и исполнения; воспроизводимость; независимое тестирование; обязательный risk gate.
- **Последствия:** `Signal` не содержит sizing или execution parameters, `OrderIntent` не является `Order`, а HOLD не создаёт intent. M2.2 реализует модели, но не Intent Planner, Risk Manager или execution.

## ADR-020: EMA crossover without filters as the first baseline strategy

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Stage 2 нужен максимально прозрачный benchmark до оптимизации и backtesting. Volatility/ATR filter добавляет параметры и затрудняет выделение ошибок базовой crossover-семантики.
- **Решение:** Первая baseline strategy использует только закрытые свечи и чистое пересечение fast/slow EMA. EMA и alpha рассчитываются через `Decimal`; начальное EMA равно SMA первого окна. ATR, volatility filter, confidence, fees и position state не входят в первую версию.
- **Причины:** Простота, объяснимость, отсутствие look-ahead, детерминированность и лёгкость unit-тестирования.
- **Последствия:** Baseline является техническим benchmark и не обещает прибыльность. Добавление фильтров или изменение crossover rules требует версионирования и отдельного review.

## ADR-021: Trading decisions are coordinated by an application pipeline

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Strategy и Intent Planner должны образовать воспроизводимый поток, но прямое связывание компонентов затруднит проверку межмодельных invariants, аудит и дальнейшее добавление обязательного Risk Manager.
- **Решение:** Синхронный application-level Trading Decision Pipeline получает готовый `MarketSnapshot`, вызывает одну Strategy, проверяет `Signal`, вызывает один заменяемый Intent Planner и возвращает immutable result. Pipeline не содержит торговых формул, не читает инфраструктуру и не выполняет risk или execution.
- **Причины:** Явный порядок, единая проверка cross-model invariants, детерминированное тестирование и подготовленная граница для будущего risk gate.
- **Последствия:** Один pipeline instance использует одну Strategy. Pipeline stateless и deterministic при одинаковых factories, но сам по себе не обеспечивает execution idempotency. Multi-strategy coordination, persistence, Risk Manager и execution добавляются отдельно.

## ADR-022: First intent planner uses long-only target exposure

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Stage 2 должен преобразовать Signal в типизированное намерение без преждевременной зависимости от balances, instrument filters, portfolio accounting и executable quantity.
- **Решение:** Первый deterministic Spot long-only planner преобразует BUY в `ENTER_LONG` с configurable target exposure, SELL — в `EXIT_LONG` с exposure `0`, а HOLD не создаёт intent. Default long target равен `Decimal("1")`.
- **Причины:** Простая объяснимая policy, соответствие long-only Spot и отделение направления стратегии от будущих sizing/risk calculations.
- **Последствия:** Planner не знает текущую позицию и может повторно описать ту же target exposure. Quantity, position-aware suppression и risk approval появляются позже; OrderIntent не является приказом на немедленную сделку.

## ADR-023: Trading decisions first run in shadow mode

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Trading Decision Pipeline реализован и проверен offline, но ещё не наблюдался в долгоживущем runtime. Подключение execution до эксплуатационной проверки нарушило бы принцип «безопасность важнее прибыльности» и обязательный risk gate.
- **Решение:** Первое runtime-подключение торговых решений выполняется только в shadow mode. Подтверждённый `CandleClosed` запускает application-level Shadow Trading Decision Engine, который читает Market Cache, строит snapshot, вызывает Trading Decision Pipeline и делает результат наблюдаемым. Результат не передаётся в risk, portfolio или execution. Bootstrap history не replay-ится; первое решение возникает после нового `CandleClosed`. Подключение требует feature flag, выключенный по умолчанию до отдельного smoke-review.
- **Причины:** Безопасная проверка реального event-driven пути, auditability, измерение ошибок и latency без воздействия на капитал или market ingestion.
- **Последствия:** `OrderIntent` в shadow mode является только наблюдаемым результатом. Shadow failures изолируются от Polling Engine и других event handlers, cache не откатывается. Paper/live orders, risk и portfolio остаются запрещены; включение shadow mode не меняет trading mode.

## ADR-024: Shadow decisions use observer ports

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Shadow result должен быть наблюдаемым сейчас и пригодным для будущего audit persistence, но прямая запись engine в файл или database связала бы application orchestration с инфраструктурой.
- **Решение:** Shadow Engine формирует immutable `ShadowDecisionRecord` и передаёт его через `ShadowDecisionObserver` port. Первая реализация — safe structured logging observer. JSONL, database, metrics и UI observers добавляются отдельными адаптерами позднее. Engine не владеет lifecycle observer.
- **Причины:** Разделение вычисления и наблюдения, заменяемость адаптеров, тестируемость без файловой системы и стабильная audit boundary.
- **Последствия:** Observer failure не откатывает вычисленное решение, не запускает pipeline повторно и не ломает market ingestion; record сохраняет semantic decision status и отдельный observation status. Durable storage и delivery guarantees отсутствуют до отдельного persistence решения.

## ADR-025: Market Cache tracks confirmed candle closure

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Свеча может быть сохранена незакрытой при bootstrap, а после границы закрытия provider может вернуть её финальную OHLCV-версию. Определение права update только по новому `observed_at_ms` ошибочно объявляло ранее открытую свечу immutable до фиксации её финального состояния.
- **Решение:** Market Cache хранит внутренний immutable entry с `Candle`, `closure_confirmed` и последним явным временем наблюдения. Свеча, сохранённая открытой, может получить монотонный финальный update после close boundary; данные и переход `OPEN -> CLOSED_CONFIRMED` коммитятся атомарно. Exact duplicate также может подтвердить закрытие без изменения данных. После подтверждения свеча immutable, кроме exact duplicate. `MarketCacheChange` явно сообщает data change и closure transition Event Projector-у.
- **Причины:** Детерминированная lifecycle-модель, корректный Binance REST reconciliation, атомарность и запрет изменения действительно подтверждённой истории.
- **Последствия:** Update permission больше не выводится только из текущего времени. Event Projector публикует финальный `CandleUpdated` перед `CandleClosed`; public snapshots остаются обычными defensive `Candle` и не раскрывают lifecycle metadata. Persistence и distributed exactly-once guarantees не добавляются.

## ADR-026: Risk Manager is mandatory before execution

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** `OrderIntent` выражает желаемую экспозицию, но не учитывает balances, instrument rules и централизованные лимиты.
- **Решение:** Любой paper, testnet или live order обязан пройти детерминированный Risk Manager. Strategy, AI и Intent Planner не могут создавать executable approval.
- **Причины:** Безопасность, единая policy, объяснимость и невозможность обхода лимитов.
- **Последствия:** Execution принимает только разрешённые параметры RiskDecision. Недоступность Risk Manager означает отсутствие исполнения.

## ADR-027: Deterministic paper execution is the first execution adapter

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** До testnet нужен воспроизводимый способ проверить sizing, fees, fills и accounting без private API и капитала.
- **Решение:** Первым `ExecutionProvider` становится synchronous in-memory paper adapter с immediate full MARKET fill, явными fee/slippage rules, injected IDs/time и без сети.
- **Причины:** Детерминированные тесты, безопасная интеграция и минимальная инфраструктурная сложность.
- **Последствия:** Модель не обещает реалистичную liquidity/latency/partial-fill симуляцию; ограничения явно отражаются в результатах.

## ADR-028: Portfolio is authoritative for balances and positions

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Дублирование balances и positions между risk, execution и orchestration создаёт расхождения.
- **Решение:** Portfolio является единственным владельцем balances, position, cost basis, fees и cumulative realised PnL; только подтверждённый Fill может изменить это состояние.
- **Причины:** Ясное владение, атомарные инварианты, idempotency и auditability.
- **Последствия:** Risk читает immutable snapshot, execution не мутирует Portfolio, а повторный fill подавляется или вызывает typed conflict.

## ADR-029: Target exposure is resolved through portfolio equity

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** `OrderIntent.target_exposure` не является quantity; перевод зависит от текущего капитала, позиции и цены.
- **Решение:** Risk Manager вычисляет target notional как target exposure, умноженную на Portfolio equity по одной reference price, затем применяет limits и получает delta order.
- **Причины:** Единая семантика sizing для enter, resize и exit, независимая от абсолютного капитала.
- **Последствия:** Нужны согласованные immutable market/portfolio snapshots; rounding и минимальные размеры проверяются после расчёта delta.

## ADR-030: Instrument rules control financial rounding

- **Статус:** принято
- **Дата:** 2026-08-04
- **Контекст:** Quantity, price и notional должны удовлетворять правилам инструмента без float и скрытой биржевой логики.
- **Решение:** Использовать versioned `InstrumentRules`; quantity округляется вниз к step, затем повторно проверяются minimum quantity/notional, balances и limits.
- **Причины:** Запрет overspend/oversell, воспроизводимость и независимость домена от Binance payload.
- **Последствия:** Rules передаются через boundary и сохраняются в audit. Изменение правил не должно менять интерпретацию уже созданных решений.

## ADR-031: Risk sizing uses one closed market snapshot and synchronized portfolio valuation

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Target exposure зависит от equity и reference price. Использование разных market times для Portfolio и Risk создаёт ложную delta quantity и невоспроизводимое решение.
- **Решение:** Risk использует close последней закрытой candle из одного immutable `MarketSnapshot`. До evaluate Portfolio должен быть оценён по той же цене и `as_of_ms`; несовпадение приводит к typed rejection. Risk не читает cache и не выполняет mark-to-market самостоятельно.
- **Причины:** Exact воспроизводимость sizing, отсутствие скрытого времени и чёткая ownership boundary.
- **Последствия:** Application orchestration обязана подготовить согласованные snapshots. Decision сохраняет market snapshot ID, portfolio version, reference price и rules version.

## ADR-032: Risk Manager does not calculate execution fees

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Fee и slippage зависят от execution model, тогда как Risk Manager должен оставаться независимым pure approval component.
- **Решение:** Risk не рассчитывает фактические fee, slippage или fill price. M3.4 использует только консервативный approval envelope `quantity × reference_price × (1 + max_slippage_bps / 10000) × (1 + max_fee_rate)` для резервирования BUY quote. Execution отвечает за фактические значения и не может нарушать approved quantity или сделать Portfolio balance отрицательным.
- **Причины:** Разделение risk approval и execution simulation, один источник fee semantics и отсутствие зависимости Risk от adapter-а.
- **Последствия:** BUY reserve gap закрыт консервативным верхним пределом, но совместимость будущей execution-конфигурации с этим envelope обязательна перед runtime wiring.

## ADR-033: Paper execution uses one immediate full fill

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Первый paper adapter должен проверить execution/Portfolio boundary без преждевременной модели exchange lifecycle.
- **Решение:** `APPROVED` и `MODIFIED` RiskDecision синхронно создают ровно один полный существующий immutable `Fill`; отдельный `Order`, partial fills, reject, timeout и cancel в первую версию не входят.
- **Причины:** Минимальный воспроизводимый контракт, отсутствие ложной реалистичности и повторное использование утверждённой Fill-модели.
- **Последствия:** `execution_id` используется как `Fill.order_id`; расширение order lifecycle потребует отдельного ADR.

## ADR-034: Paper execution time is derived from the risk decision

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Wall clock, monotonic clock и sleep сделали бы одинаковый offline input невоспроизводимым и смешали бы market chronology с runtime scheduling.
- **Решение:** `started_at_ms` равен `RiskDecision.decided_at_ms`, `completed_at_ms` и `Fill.filled_at_ms` равны ему плюс configured deterministic `latency_ms`; default latency равна нулю.
- **Причины:** Детерминированный replay, отсутствие скрытого времени и простые exact tests.
- **Последствия:** Monotonic time может использоваться будущей orchestration только как operational metric и не определяет timestamp Fill.

## ADR-035: Execution owns explicit quote rounding and fee facts

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Portfolio не округляет валидные Fill values, а Risk резервирует envelope, но не создаёт фактическую execution fee.
- **Решение:** Paper execution использует versioned `InstrumentRules`: gross и fee вычисляются Decimal-only и явно округляются вниз до quote precision; fee равна rounded `gross_notional × configured fee_rate`, slippage первой версии равен нулю.
- **Причины:** Exact reconciliation, отсутствие скрытого float/rounding и единый владелец внешних quote facts.
- **Последствия:** Fee config не может превышать Risk-approved envelope; изменение rounding или slippage требует versioning и review.

## ADR-036: PaperTradingEngine coordinates risk, execution and portfolio

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Risk, execution и Portfolio имеют отдельные безопасные контракты, но сквозной paper cycle требует единого владельца порядка вызовов, cross-model проверок и failure boundaries.
- **Решение:** Synchronous application-level `PaperTradingEngine` получает готовые `DecisionPipelineResult` и `MarketSnapshot`, владеет injected Portfolio aggregate, координирует valuation, Risk, paper execution, Fill apply и observer, не дублируя доменные вычисления.
- **Причины:** Один явный orchestration authority, audit chain и невозможность обойти обязательный Risk gate.
- **Последствия:** Engine является stateful in-memory service, но не содержит Strategy, execution/accounting formulas, network или runtime subscription.

## ADR-037: Portfolio is marked to market on every closed-candle decision

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Если Portfolio оценивается только при intent, HOLD-candles не обновляют equity, unrealised PnL и drawdown, а Risk получает несогласованную valuation.
- **Решение:** После validation/dedup PaperTradingEngine выполняет `Portfolio.mark_to_market()` по latest closed close и snapshot time для HOLD, planner-declined и intent cycles до любых Risk/Execution действий.
- **Причины:** Непрерывная paper equity history и один authority синхронизации Portfolio с MarketSnapshot.
- **Последствия:** Valuation является committed non-trade update и сохраняется при Risk rejection или последующей ошибке; duplicate cycle не выполняет её повторно.

## ADR-038: Paper cycle deduplication is bounded and process-local

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Risk и execution stateless, а повтор одного decision до persistence мог бы создать новый Fill и повторно изменить Portfolio.
- **Решение:** Engine хранит bounded FIFO completed-result registry: ключ intent cycle — `intent_id`, no-intent cycle — `pipeline_run_id`; duplicate возвращает прежний immutable result без новых ID или component calls.
- **Причины:** Безопасная process-lifecycle защита с детерминированным eviction до появления durable journal.
- **Последствия:** Dedup сбрасывается при restart и не является exactly-once; persistence milestone должен сохранить observable semantics.

## ADR-039: Shadow and paper consume one shared decision result

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Независимый запуск Strategy для shadow и paper на одном CandleClosed создаёт разные IDs/results, лишние вычисления и риск расхождения решений.
- **Решение:** Будущий `TradingDecisionCoordinator` вычисляет один `DecisionPipelineResult` и передаёт его независимым shadow observer и PaperTradingEngine paths согласно feature flags.
- **Причины:** Одна торговая гипотеза на candle, воспроизводимость и единый audit origin.
- **Последствия:** Shadow и paper flags независимы, но pipeline существует при включении любого consumer; runtime coordinator реализуется отдельным milestone.

## ADR-040: PaperTradingEngine has composition lifecycle and single-writer ownership

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Orchestrator нуждается в mutable operational state, но M3.8 не включает runtime, concurrency или lifecycle worker-а.
- **Решение:** Composition root создаёт и владеет Portfolio, RiskManager, PaperExecutionProvider, factories и observer. Engine работает синхронно, не имеет `start/stop/close`, владеет только bounded dedup/status и допускает один вызов `process()` одновременно.
- **Причины:** Явное владение, отсутствие скрытых ресурсов и сохранение single-writer гарантий Portfolio.
- **Последствия:** Re-entrant/concurrent вызов отклоняется типизированно; подписка на события и shutdown остаются ответственностью будущего runtime coordinator.

## ADR-041: Ambiguous post-execution failures degrade the engine

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Без persistence нет общей транзакции между созданием Fill, применением Portfolio и process-local dedup.
- **Решение:** После возврата валидного `FILLED` результата ошибка `Portfolio.apply_fill()` или последующая неоднозначная проверка commit переводит экземпляр в `DEGRADED`. Новые cycles запрещены до внешней проверки и пересоздания; автоматического retry нет. Обычная execution exception до возвращённого Fill не считается финансовым commit.
- **Причины:** Повтор может создать второй Fill или повторно изменить Portfolio, а продолжение после неоднозначного состояния нарушает безопасность.
- **Последствия:** M3.8 не обещает crash recovery/exactly-once; durable journal и reconciliation остаются отдельным persistence milestone.

## ADR-042: Paper observer delivery occurs after business commit and is isolated

- **Статус:** принято
- **Дата:** 2026-08-05
- **Контекст:** Observer нужен для диагностики, но не должен участвовать в финансовой атомарности или инициировать повтор исполнения.
- **Решение:** Engine вызывает один injected observer после определения и commit бизнес-результата, затем фиксирует delivery status в immutable result и dedup registry. Ошибка observer даёт явную degradation доставки, не откатывает Portfolio и не повторяет Risk/Execution.
- **Причины:** Разделение финансового состояния и best-effort наблюдаемости, однозначный результат для caller-а.
- **Последствия:** Dedup hit не публикуется повторно; fan-out реализуется отдельным observer adapter; durable delivery/replay отсутствуют до persistence.

## ADR-043: One shared decision evaluation feeds all runtime consumers

- **Статус:** принято
- **Дата:** 2026-08-08
- **Контекст:** Независимые shadow и paper event paths повторно строили бы snapshot и запускали Strategy/Pipeline для одного `CandleClosed`, создавая разные IDs и потенциально разные решения.
- **Решение:** Runtime создаёт ровно один `MarketSnapshot` и вызывает `TradingDecisionPipeline.evaluate()` ровно один раз на уникальный source event. Shadow и paper получают те же экземпляры snapshot, result, signal и intent.
- **Причины:** Одна торговая гипотеза на событие, воспроизводимость, единая audit chain и отсутствие расхождения consumers.
- **Последствия:** Все decision consumers подключаются только после общего coordinator; самостоятельный Shadow Engine не может одновременно оставаться активным runtime subscriber.

## ADR-044: TradingDecisionCoordinator owns CandleClosed decision orchestration

- **Статус:** принято
- **Дата:** 2026-08-08
- **Контекст:** Event subscription, cache consistency, snapshot creation, pipeline evaluation и fan-out являются application/runtime orchestration, а не торговой доменной логикой.
- **Решение:** `TradingDecisionCoordinator` становится единственным decision-path subscriber на `CandleClosed`, владеет только своей subscription, bounded dedup и status, а зависимости и их lifecycle принадлежат composition root.
- **Причины:** Явная ownership boundary, тестируемый lifecycle и запрет скрытого повторного запуска Strategy.
- **Последствия:** Coordinator размещается в `app/coordination`, не содержит Strategy/Risk/Execution/Portfolio logic и не закрывает dispatcher или provider.

## ADR-045: Consumer failures are isolated after shared decision creation

- **Статус:** принято
- **Дата:** 2026-08-08
- **Контекст:** Shadow observation и paper mutation имеют разные failure domains; сбой одного не должен отменять общий decision или препятствовать другому consumer.
- **Решение:** Consumers вызываются детерминированно: shadow, затем paper. Обычный `Exception` одного consumer классифицируется и не препятствует другому; `BaseException` не подавляется. Trading outcome отделён от delivery outcome.
- **Причины:** Непрерывность ingestion/shadow analysis, честная диагностика partial delivery и отсутствие ложной общей транзакции.
- **Последствия:** Coordination result хранит отдельные статусы/errors; после consumer attempt final result сохраняется и duplicate не повторяет side effects.

## ADR-046: Event-level shared-decision deduplication is bounded and process-local

- **Статус:** принято
- **Дата:** 2026-08-08
- **Контекст:** Повторная доставка `CandleClosed` может повторно вычислить decision и вызвать consumers, а durable journal пока отсутствует.
- **Решение:** Coordinator использует bounded FIFO по `source_event_id` и хранит immutable final result. Pre-decision failures не кэшируются; после любого consumer attempt результат завершается и кэшируется независимо от delivery failure.
- **Причины:** Предотвращение повторных shadow/paper side effects при partial delivery и возможность повторить transient pre-decision failure.
- **Последствия:** Защита ограничена process lifetime и capacity, internal retry отсутствует, durable exactly-once не обещается.

## ADR-047: Paper degradation does not stop shadow analysis

- **Статус:** принято
- **Дата:** 2026-08-08
- **Контекст:** `PaperTradingEngine.DEGRADED` означает неоднозначное финансовое состояние, но не повреждение market ingestion или общего аналитического pipeline.
- **Решение:** Coordinator продолжает shared decision и shadow path, но не вызывает degraded paper engine и отмечает paper как `UNAVAILABLE` до внешней reconciliation и пересоздания.
- **Причины:** Безопасность Portfolio при сохранении наблюдаемости и рыночной аналитики.
- **Последствия:** Paper degradation не останавливает ingestion; coordinator не выполняет reset/recovery и ведёт отдельный `paper_unavailable` counter.

## ADR-048: Paper trading uses an append-only journal plus Portfolio checkpoints

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M3.15 crash/restart safety review)
- **Дата:** 2026-08-08
- **Контекст:** Snapshot-only storage не доказывает историю Fill и не позволяет надёжно классифицировать crash, а replay полного журнала без checkpoints неограниченно увеличивает startup time.
- **Решение:** Authoritative source для paper mutations — append-only durable trading journal. Периодические verified Portfolio checkpoints являются только recovery optimization и ссылаются на точный journal sequence/state digest.
- **Причины:** Полный audit trail, deterministic replay и ограниченное время восстановления без конкурирующих sources of truth.
- **Последствия:** Checkpoint может быть отброшен и перестроен из journal; trading history не удаляется автоматически; реализация требует проверки digest/invariants при restore.

## ADR-049: SQLite is the first local persistence adapter

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M3.15 crash/restart safety review)
- **Дата:** 2026-08-08
- **Контекст:** Текущий paper runtime однопроцессный, синхронный и имеет одного writer, поэтому отдельный database server пока не оправдан.
- **Решение:** Первый adapter использует SQLite за application persistence ports; SQLite imports и connection/transaction ownership остаются только в adapter/composition layer.
- **Причины:** ACID, локальная durability, простой deployment/backup и достаточность для Stage 3 с возможностью будущего PostgreSQL adapter.
- **Последствия:** Применяются WAL, `synchronous=FULL`, foreign keys, busy timeout и explicit writer lock; второй trading writer не допускается.

## ADR-050: Financial Decimal values are persisted losslessly

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M3.15 crash/restart safety review)
- **Дата:** 2026-08-08
- **Контекст:** SQLite REAL и float не обеспечивают exact reconciliation финансовых значений и делают digest/replay зависимыми от округления.
- **Решение:** Все financial Decimal сохраняются как валидированные canonical base-10 TEXT values; NaN/Infinity и float intermediates запрещены.
- **Причины:** Exact round-trip, context-independent replay и стабильные audit digests.
- **Последствия:** Adapter обязан canonicalize zero/notation и проверять limits; presentation scale хранится отдельно только если является бизнес-фактом.

## ADR-051: Durable idempotency uses stable decision and execution keys

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M3.15 crash/restart safety review)
- **Дата:** 2026-08-08
- **Контекст:** Runtime UUID, `source_event_id` и `intent_id` меняются при restart/replay и не предотвращают повторный Fill для той же strategy/candle semantics.
- **Решение:** Durable decision key — SHA-256 от versioned canonical fingerprint фактического ordered Strategy input window, latest candle, snapshot policy и strategy/planner identity/configuration. От него, approved Risk authorization, policy/rules и execution configuration выводится отдельный durable command key. Correlation UUID не используются как idempotency keys.
- **Причины:** Доказуемое распознавание одного решения после restart и различение EMA/config/version changes.
- **Последствия:** Storage вводит PK для decision/command, UNIQUE semantic decision slot и `UNIQUE(fills.durable_command_key)`; identical retry возвращает committed result, corrected/conflicting input в том же slot переводит recovery в fail-closed state; deliberate reprocessing требует новой audited Portfolio lineage.

## ADR-052: Paper execution and Portfolio mutation share one durable transaction

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M3.15 crash/restart safety review)
- **Дата:** 2026-08-08
- **Контекст:** Crash между stateless paper Fill creation и in-memory `Portfolio.apply_fill()` создаёт риск второго Fill и неоднозначное финансовое состояние.
- **Решение:** Application-level Paper Cycle Unit of Work атомарно commit-ит decision claim, risk/execution/fill facts, valuation/Portfolio transition и terminal cycle state. `PaperExecutionProvider` остаётся deterministic calculator без database access.
- **Причины:** В paper mode Fill и соответствующая Portfolio mutation находятся в одном ACID boundary и не могут стать видимыми частично.
- **Последствия:** In-memory state публикуется только после durable commit; rollback допускает safe retry; unexpected post-commit swap failure требует recovery, а не execution replay.

## ADR-053: Recovery fails closed on ambiguous or corrupted financial state

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M3.15 crash/restart safety review)
- **Дата:** 2026-08-08
- **Контекст:** Автоматическое округление, удаление конфликтующей записи или fallback к пустому Portfolio способны скрыть финансовое повреждение.
- **Решение:** Schema mismatch, malformed Decimal, broken FK/digest, impossible lifecycle/Portfolio state или conflicting duplicate приводят к `RECOVERY_REQUIRED`; decision runtime не стартует до доказуемой reconciliation.
- **Причины:** Безопасность важнее доступности и прибыльности; деньги нельзя «исправлять» эвристически.
- **Последствия:** Нужны typed diagnostics, backup/restore и будущий explicit forensic workflow; market ingestion может быть доступен отдельно, но paper mutation запрещена.

## ADR-054: Observation delivery follows durable business commit

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M3.15 crash/restart safety review)
- **Дата:** 2026-08-08
- **Контекст:** Observer/logging являются best-effort side effects и не должны определять, был ли Fill/Portfolio transition committed.
- **Решение:** Observer вызывается только после durable business commit. Delivery status может сохраняться отдельно, но observer failure или missing delivery никогда не вызывает trade replay и не меняет business outcome.
- **Причины:** Разделение authoritative financial state и operational observability.
- **Последствия:** Exactly-once observer delivery не обещается; audit journal остаётся достаточным для business recovery независимо от logs/observer.

## ADR-055: Exchange execution uses a durable multi-state order lifecycle

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** Paper immediate-fill semantics не представляют acknowledgement, open order, partial fill, cancellation и ambiguous exchange state.
- **Решение:** TESTNET order является durable aggregate со state machine `PREPARED`, `SUBMITTING`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, exchange-observed terminal states и `RECONCILIATION_REQUIRED`. Initial MARKET-only scope не предоставляет cancellation command.
- **Причины:** Exchange execution асинхронен в реальности и должен восстанавливаться без догадок после каждого process/network boundary.
- **Последствия:** `PaperExecutionResult` не переиспользуется; lifecycle events append-only, projection rebuildable, transport outcome не является exchange truth, status conflicts fail closed.

## ADR-056: Client order identity derives from durable command identity

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** Runtime intent/execution UUID меняются при replay и не могут безопасно идентифицировать remote order.
- **Решение:** ID равен `ctt1t-` плюс первые 26 lowercase unpadded RFC4648 Base32 символов SHA-256 от versioned canonical TESTNET venue/account scope и `durable_command_key`; он сохраняется до submission и используется во всех query/reconciliation operations.
- **Причины:** Один semantic authorization должен иметь одну restart-stable remote identity.
- **Последствия:** 32-символьный ID содержит 130 hash bits; collision проверяется UNIQUE constraint/payload digest. REST contract должен быть подтверждён через `/api/v3/order/test` до первого Testnet submit.

## ADR-057: Ambiguous submission requires reconciliation

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** После передачи request timeout или process death не доказывает, что Binance не принял order.
- **Решение:** Любой ambiguous submit переводит order в `RECONCILIATION_REQUIRED`; blind resubmission запрещён, exchange запрашивается по сохранённому client order ID.
- **Причины:** Повторный order опаснее временной недоступности execution.
- **Последствия:** Binance не предоставляет strong proof of absence: даже bounded повторные `NotFound` не разрешают automatic resubmit/new ID; требуется audited operator resolution.

## ADR-058: Exchange-reported trades are authoritative execution facts

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** Exchange order может иметь несколько partial fills с фактическими price, quantity и commission, которые нельзя вывести из Risk reference price или acknowledgement.
- **Решение:** Только normalized exchange trades создают Portfolio Fill; каждый trade имеет venue/account/symbol identity и durable uniqueness. Cumulative order quantities служат reconciliation check.
- **Причины:** Exact accounting и duplicate protection должны следовать exchange truth.
- **Последствия:** Один command допускает много Fill; exchange truth commit-ится отдельно от Portfolio applicability. Unsupported commission asset сохраняется и блокирует settlement/new submissions.

## ADR-059: Exchange command commits before network submission

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** SQLite и Binance не могут участвовать в одной ACID transaction, но remote identity должна существовать до side effect.
- **Решение:** Transaction A commit-ит decision, Risk authorization, command, client ID и `PREPARED`; отдельная transaction commit-ит `SUBMITTING` attempt до network; последующая transaction сохраняет observations/trades/Portfolio transitions.
- **Причины:** Crash должен оставлять однозначное правило: first attempt только из `PREPARED`, query/reconciliation после attempt marker.
- **Последствия:** Network I/O запрещён внутри SQLite transaction; transport attempt/response отделены от authoritative order events; availability уступает duplicate-order safety.

## ADR-060: Startup reconciliation gates exchange submissions

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** После restart локальный journal может отставать от exchange orders/trades, а новый Risk decision без сверки использует неполные balances и exposure.
- **Решение:** TESTNET startup восстанавливает local state, сверяет все non-terminal orders, trades, balances, rules и cursors, и только успешная durable reconciliation открывает submission gate.
- **Причины:** New execution требует доказуемого account/Portfolio baseline.
- **Последствия:** Market ingestion/shadow могут работать отдельно; local recovery и exchange reconciliation имеют независимые статусы; conflict не сбрасывает Portfolio и блокирует orders.

## ADR-061: Durable kill switch blocks submission but permits reconciliation

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** Emergency control должен прекращать новый exchange risk, не препятствуя обнаружению и учёту уже исполненных trades.
- **Решение:** Kill switch — durable versioned application control, default active для новой TESTNET lineage. Он запрещает prepare/submit, но разрешает query, reconciliation и settlement. Disarm требует current clean reconciliation, audited operator command и новую process-local generation-bound capability.
- **Причины:** Остановка новых side effects не должна ослеплять recovery или терять exchange facts.
- **Последствия:** Restart/config не очищает switch и уничтожает process-local submission capability; cancellation не входит в initial MARKET-only scope.

## ADR-062: TESTNET and LIVE execution are structurally separated

- **Статус:** принято
- **Дата принятия:** 2026-08-09 (M4.2 final design and safety review)
- **Дата:** 2026-08-09
- **Контекст:** Один mode flag, общий credential namespace или свободный base URL создают риск случайного real-capital execution.
- **Решение:** PAPER, TESTNET и LIVE являются разными capabilities. Initial Stage 4 поддерживает только PAPER/TESTNET, использует dedicated testnet credentials и endpoint allowlist, а LIVE unconditionally fail-closed до отдельного live-readiness review/build capability.
- **Причины:** Stage 4 testnet success не является разрешением live.
- **Последствия:** Public-data configuration и environment label не дают execution authority; Testnet использует exact endpoint allowlist. Будущий LIVE потребует отдельные credentials, build capability, ceremony, ADR и review.

## ADR-063: Blocked Testnet startup composes a recovery-only runtime

- **Статус:** принято
- **Дата принятия:** 2026-08-12 (M4.8 final acceptance review)
- **Дата:** 2026-08-12
- **Контекст:** Kill switch, reconciliation gap или non-terminal order должны запрещать новые submissions, но process exit одновременно отключает reconciliation, settlement и operational visibility.
- **Решение:** Успешное local recovery создаёт доступный recovery-only Testnet graph с persistence, Portfolio, private read adapter, reconciliation и settlement. Пока readiness не `READY`, graph не содержит `SubmissionCapability`, application service или Testnet execution consumer. Testnet-only process не запускает decision coordinator; shadow может продолжить через общий coordinator без Testnet delivery. Переход в submit-capable `READY` требует нового process startup после изменения durable control/recovery state.
- **Причины:** Submission safety и runtime availability являются независимыми состояниями; блокировка новых side effects не должна блокировать repair уже существующего durable/exchange state.
- **Последствия:** `BLOCKED` startup является успешным degraded startup, а не исключением. Fatal local corruption, schema incompatibility и ownership failure по-прежнему завершают startup. In-process promotion запрещён, поэтому capability не может появиться из stale startup lineage.
- **Evidence:** Full production `build_application()` process tests recreate every graph around one file-backed SQLite authority and fake remote state. They prove lost-ACK recovery, FILLED/PARTIALLY_FILLED exact-once settlement, five repeated NOT_FOUND restarts and ambiguity-persistence loss without replacement submission. M4.8 final acceptance review found no blocking recovery-only safety gap.

## ADR-064: Controlled Testnet smoke uses an isolated managed subledger and durable one-shot authority

- **Статус:** принято
- **Дата принятия:** 2026-08-13 (M4.10 Phase 3 durable one-shot authority final design review)
- **Дата:** 2026-08-13
- **Контекст:** M4.10 Phase 1 observed `1 BTC` and `10000 USDT` in the dedicated Testnet account before any managed order. The current Portfolio starts with zero base and configured quote, while reconciliation validates balance scope but does not prove ownership. Separately, clearing the durable kill switch before the first attempt leaves a crash window in which restart can mint a fresh capability because no submission counter has yet been consumed.
- **Решение:** External exchange inventory and managed Portfolio inventory are separate authorities. The first smoke uses zero managed BTC and an explicit `25 USDT` quote allocation; only authoritative trades for Chappie's own durable BUY create managed BTC. The global kill switch remains ACTIVE. A single application/persistence operation `authorize_phase3_smoke_command()` validates current SAFE reconciliation, ACTIVE control, session/root/account lineage, cleanliness, freshness and the exact BUY-only envelope; inside one `BEGIN IMMEDIATE` transaction it inserts the authority, creates the sole command/order/PREPARED event and reservation, links them one-to-one, marks the authority CONSUMED and commits an audit event. There is no authority-only commit and no committed unconsumed state. Authority consumption occurs at PREPARED commit, never at HTTP transmission.
- **Durable identity:** `authority_id`, TESTNET, `binance-spot`, execution root, session, account fingerprint, expected control/reconciliation generations, unique operator run ID, unique Phase 3 economic-slot key, policy version, exact durable command key, rules/configuration fingerprints, canonical risk-envelope and balance-scope payloads/digests, creation/consumption timestamps and record digest. No credential-derived value participates.
- **Control/capability:** Successful commit returns only an in-memory claim receipt from which the application layer issues a non-copyable, non-serializable, TESTNET-only `SubmissionCapability` bound to `authority_id`, command, session and generations. The final gate accepts ACTIVE control only for that exact consumed authority/command pair; ACTIVE continues to block every other command. Restart never restores the receipt or capability, recovered PREPARED is not submit-eligible, and SUBMITTING remains reconciliation-only.
- **Lifecycle/freshness:** The reviewed authorization input has a bounded integer-millisecond age checked before and again inside the transaction. Stale input fails without inserting a row. The only committed authority status is CONSUMED; transaction-local PREPARED either becomes CONSUMED with its command or rolls back. CONSUMED is terminal. Therefore no EXPIRED/CANCELED state or replacement authority is needed for this one-shot milestone.
- **Managed balances:** Existing Portfolio root/config represent `0 BTC / 25 USDT`; no separate financial-root migration is needed. The authority records immutable external baselines from a fresh cleanliness preflight (`exchange total - managed allocation/state`). Reconciliation compares free+locked totals to external baseline plus managed exchange effects derived only from authoritative Chappie trades. Excess exchange USDT remains external; exchange free quote must cover managed requirements and reserve. Any unexplained delta is conflict, not import. SELL is absent from the policy/schema payload and typed-rejected before command creation.
- **Envelope:** BUY, BTCUSDT, MARKET, one submission/non-terminal/in-flight maximum, `12 USDT` maximum order/submitted/turnover/managed-position notional, `25 USDT` allocation and minimum `13 USDT` reserve, plus canonical fee/slippage assumptions, current rules fingerprint and configuration/policy version. Final price and Decimal quantity remain fresh command facts and must fit the bound envelope.
- **Schema/recovery:** Additive schema v5 adds immutable `exchange_smoke_authorities` plus append-only authority events. PK `authority_id`; UNIQUE `(execution_root_id, operator_run_id)`, Phase 3 economic-slot key and `consumed_durable_command_key`; FKs bind root/session/command; CHECKs fix TESTNET/binance-spot, BUY-only policy, CONSUMED linkage/timestamps and 64-character digests. UPDATE/DELETE triggers forbid mutation. Local recovery validates digests, lineage, generations, one-to-one command linkage, envelope/balance payloads and timestamps; any impossible state is `RECOVERY_REQUIRED`.
- **Причины:** Exchange inventory and managed Portfolio ownership are different facts. A durable consumed-before-side-effect ceremony closes the crash window without a raw adapter shortcut, reusable environment bypass or inferred ownership.
- **Последствия:** Schema-v5 implementation and crash review are still required; accepting this design does not authorize Phase 3. Migrations 001–004 remain byte-identical. The future migration is additive and transactional under existing WAL/FULL/foreign-key/single-writer rules. Phase 4 reopens the same database/account/authority/command identity, reconciles and settles without a second submission. `25 USDT` is a managed ceiling, not ownership of the whole exchange balance.
