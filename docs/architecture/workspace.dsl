workspace "Chappie Architecture Map V1" "Authoritative C4 model for the current and planned Chappie architecture." {
    model {
        operator = person "Operator / User" "Runs research, Paper, explicitly gated Testnet, setup, and development workflows." "External"
        vscode = softwareSystem "VS Code" "Optional local Chappie workbench." "External"
        binancePublic = softwareSystem "Binance Public Market Data" "Public candles and historical market data." "External,Data"
        binanceTestnet = softwareSystem "Binance Spot Testnet" "Explicitly configured non-live exchange execution boundary." "External,SafetyCritical"
        ollama = softwareSystem "Ollama" "Optional local structured-inference provider." "External"
        storage = softwareSystem "Local Filesystem / SQLite" "Runtime journals, research evidence, Knowledge, caches, and status files." "External,Data,SafetyCritical"
        researchSources = softwareSystem "Offline Research Information Sources" "Bounded public research datasets and official information sources." "External,Research"

        cloudModels = softwareSystem "Cloud Model Providers" "No concrete provider adapter exists today." "External,Planned"
        capitalSources = softwareSystem "Capital / Macro Data Sources" "Future breadth, dominance, liquidity, equities, VIX, DXY, and rates inputs." "External,Planned,Research"
        derivativesSources = softwareSystem "Derivatives Data Sources" "Future OI, funding, liquidation, and basis inputs." "External,Planned,Research"
        onchainSources = softwareSystem "On-chain Sources" "Future on-chain inputs." "External,Planned,Research"
        liveExchange = softwareSystem "LIVE Exchange" "Reserved boundary; structurally unavailable in current composition." "External,Planned,SafetyCritical"

        chappie = softwareSystem "Chappie" "Safety-first Spot trading research, Paper runtime, gated Testnet MVP, and AI-assisted development platform." "Implemented" {
            runtime = container "Runtime / Composition" "Builds and owns the in-process application graph; TraderAgent is only a polling lifecycle wrapper." "Python" "Implemented" {
                traderLifecycle = component "TraderAgent Lifecycle" "Starts/stops finite or continuous polling; not an autonomous LLM agent." "app.runtime.trader" "Implemented"
                applicationComposition = component "Application Composition" "Constructs Market, decision, Paper, persistence, and optional Testnet services." "app.runtime.application" "Implemented,SafetyCritical"
                statusProjection = component "Runtime Status" "Sanitized process and subsystem status." "app.runtime.status" "Implemented"
            }
            market = container "Market" "Public market provider, validation, bounded cache, polling, and completed-candle events." "Python" "Implemented,Data" {
                publicMarketAdapter = component "Public Market Adapter" "Reads Binance public time, ticker, and candle APIs." "app.market.binance_provider" "Implemented"
                marketCache = component "Market Cache" "Bounded process-local completed-candle state; not model memory." "app.market.cache" "Implemented,Data"
                marketEvents = component "Polling and Events" "Validates candles and emits synchronous CandleClosed events." "app.market.polling / app.market.events" "Implemented"
            }
            decision = container "Trading Domain & Coordination" "Builds one advisory decision per confirmed close and delivers it to bounded consumers." "Python" "Implemented,SafetyCritical" {
                strategyPort = component "Strategy and Snapshot Contracts" "Immutable snapshots, advisory signals, and strategy port." "app.trading" "Implemented"
                planner = component "Intent Planner" "Converts non-HOLD signals to bounded long-only intents." "app.trading.intent" "Implemented"
                coordinator = component "Decision Coordinator" "Evaluates once and orders Shadow, Paper, and ready Testnet delivery." "app.coordination" "Implemented,SafetyCritical"
            }
            strategies = container "Strategies" "Advisory EMA, trend, breakout, momentum, and reversion implementations." "Python" "Implemented"
            risk = container "Risk" "Mandatory deterministic portfolio and exchange risk decisions." "Python / Decimal" "Implemented,SafetyCritical"
            portfolio = container "Portfolio" "Financial accounting authority." "Python / Decimal" "Implemented,SafetyCritical,Data"
            paper = container "Paper Runtime" "Credential-free deterministic execution and durable Paper cycles." "Python" "Implemented,SafetyCritical" {
                paperEngine = component "Paper Trading Engine" "Coordinates decision, Risk, simulated execution, portfolio, and persistence." "app.paper.engine" "Implemented,SafetyCritical"
                paperExecution = component "Paper Execution Provider" "Creates deterministic simulated fills." "app.execution.paper" "Implemented"
                attribution = component "Attribution" "Binds strategy, decision, Risk, fill, and portfolio evidence." "app.paper.attribution" "Implemented,Data"
            }
            persistence = container "Persistence" "Single-writer SQLite journals, migrations, checkpoints, and recovery." "Python / SQLite" "Implemented,SafetyCritical,Data"
            shadow = container "Shadow" "Read-only observation of shared decisions." "Python" "Implemented"
            exchangeRuntime = container "Exchange Runtime" "Testnet readiness, submission consumer, reconciliation, settlement, and SAFE_STOP." "Python" "Implemented,SafetyCritical" {
                readiness = component "Readiness and Capability" "Fail-closed recovery, reconciliation, settlement, and process-local capability gate." "app.exchange_runtime.readiness" "Implemented,SafetyCritical"
                submission = component "Submission Service" "Revalidates gates and coordinates a bounded submission." "app.exchange_runtime.submission" "Implemented,SafetyCritical"
                reconciliation = component "Reconciliation and Settlement" "Resolves authoritative remote state without blind retry." "app.exchange_execution.reconciliation / app.exchange_runtime.settlement" "Implemented,SafetyCritical"
                safeStop = component "SAFE_STOP" "Blocks subsequent process-local submission after unexpected consumer failure." "app.exchange_runtime.consumer" "Implemented,SafetyCritical"
            }
            exchangeExecution = container "Exchange Execution" "Durable PREPARED/SUBMITTING order lifecycle and reconciliation contracts." "Python" "Implemented,SafetyCritical"
            binanceInfrastructure = container "Binance Infrastructure" "Signed Spot Testnet adapter implementing bounded exchange ports." "Python / HTTP" "Implemented,SafetyCritical"

            research = container "Research Platform" "Offline-only reproducible research; never runtime or execution authority." "Python" "Implemented,Research" {
                dataAuthority = component "Research Data Authority" "Canonical history, validation, manifests, alignment, labels, and outcomes." "app.research.data / app.research.strategy.history" "Implemented,Research,Data"
                strategyResearch = component "Strategy Research" "Labs, walk-forward studies, frozen evaluation, and authorized OOS." "app.research.strategy" "Implemented,Research"
                precursor = component "Market Precursor Discovery" "Base/context authorities, typed observations, frozen evaluation, classification, ranking, and Report V2." "app.research.market_precursor" "Implemented,Research,Data"
                intelligence = component "Research Intelligence" "Offline market, flow, event, behavioral, and macro experiments." "app.research.intelligence" "Experimental,Research"
            }

            capitalFlow = container "Global Risk & Capital Rotation Context" "Offline free-first provider contracts, metadata, causal availability, and future context sidecar; never runtime or execution authority." "Python" "Partial,Research,Data"
            derivatives = container "Derivatives Context" "Future offline OI, funding, liquidation, and basis context owned by Research." "Planned" "Planned,Research"
            orderFlow = container "Order Flow / LOB" "Future offline trade and order-book context owned by Research." "Planned" "Planned,Research"
            newsContext = container "News / Event Context" "Future authoritative offline news/event context owned by Research." "Planned" "Planned,Research"
            macroContext = container "Macro Context" "Future authoritative offline macro-risk context owned by Research." "Planned" "Planned,Research"
            onchainContext = container "On-chain Context" "Future authoritative offline on-chain context owned by Research." "Planned" "Planned,Research"

            ai = container "AI / Model Layer" "Provider-neutral tasks, profile routing, structured inference, and strict validation; no trading authority." "Python" "Implemented" {
                aiFacade = component "ChappieAI" "Creates bounded requests and validates structured responses." "app.ai.runtime" "Implemented"
                modelRouter = component "Model Router" "Maps task model profiles to provider ports." "app.ai.router" "Implemented"
                ollamaAdapter = component "Ollama Provider" "Concrete local provider adapter." "app.ai.providers.ollama" "Implemented"
                cloudAdapter = component "Cloud Provider Adapter" "No concrete adapter implemented." "Planned" "Planned"
            }
            knowledge = container "Knowledge" "Clean-snapshot provenance store and deterministic retrieval; not general-purpose Memory." "Python / JSON" "Implemented,Data" {
                knowledgeBuild = component "Knowledge Build" "Ingests only allowlisted sources from a clean commit and creates a digest-bound manifest." "app.knowledge.store" "Implemented,Data"
                knowledgeRetrieval = component "Knowledge Retrieval" "Verifies and selects bounded canonical context." "app.knowledge.retrieval" "Implemented"
            }
            development = container "Development Intelligence" "AI-assisted, bounded read-only repository analysis and human-approved advisory handoffs." "Python" "Implemented" {
                repositoryInventory = component "Repository Inventory" "Produces bounded facts from a tracked repository snapshot." "app.repository_inventory" "Implemented"
                contextResolver = component "Implementation Context" "Builds and completes immutable context without mutation authority." "app.implementation_context / app.targeted_context" "Implemented"
                developmentAnalysis = component "Development Analysis" "Synthesizes evidence and prepares advisory CodexTaskV1 handoffs." "app.development_analysis" "Implemented"
            }
            controlPlane = container "Control Plane" "Read-only local projection and loopback transport." "Python / HTTP" "Implemented"
            interfaces = container "CLI / Tools / VS Code Adapter" "Stable CLI, leaf operator/research commands, and optional local workbench." "Python / TypeScript" "Implemented"
            runtimeState = container "Runtime State & Caches" "Process-local caches and bounded cached evidence; distinct from canonical Knowledge." "Memory / Files" "Implemented,Data"
            historical = container "Historical Testnet Milestones" "Retained one-shot Stage 4 evidence excluded from active MVP composition." "Python / SQLite" "Historical,SafetyCritical"
        }

        operator -> chappie "Operates and reviews"
        vscode -> chappie.interfaces "Uses loopback Control Plane"
        chappie.market -> binancePublic "Reads public market data"
        chappie.binanceInfrastructure -> binanceTestnet "Reads account state and submits only when authorized"
        chappie.ai -> ollama "Requests structured local inference"
        chappie.persistence -> storage "Owns runtime SQLite state"
        chappie.research -> storage "Writes immutable/atomic research evidence"
        chappie.knowledge -> storage "Reads/writes provenance-bound Knowledge"
        researchSources -> chappie.research "Supplies bounded offline inputs"

        chappie.runtime -> chappie.market "Starts and owns"
        chappie.market -> chappie.decision "Publishes confirmed closes"
        chappie.decision -> chappie.strategies "Evaluates advisory strategy"
        chappie.decision -> chappie.risk "Requires Risk decision"
        chappie.decision -> chappie.shadow "Delivers read-only decision"
        chappie.decision -> chappie.paper "Delivers shared decision"
        chappie.decision -> chappie.exchangeRuntime "Delivers only when ready"
        chappie.paper -> chappie.portfolio "Applies deterministic fills"
        chappie.paper -> chappie.persistence "Commits cycle and attribution"
        chappie.exchangeRuntime -> chappie.exchangeExecution "Uses durable lifecycle"
        chappie.exchangeRuntime -> chappie.risk "Rechecks exchange Risk"
        chappie.exchangeExecution -> chappie.persistence "Persists before side effect"
        chappie.exchangeRuntime -> chappie.binanceInfrastructure "Uses bounded adapter"
        chappie.controlPlane -> chappie.ai "Requests advisory inference"
        chappie.controlPlane -> chappie.knowledge "Retrieves canonical context"
        chappie.development -> chappie.knowledge "Uses provenance-bound evidence"
        chappie.development -> chappie.ai "Requests validated synthesis"
        chappie.interfaces -> chappie.controlPlane "Uses read-only transport"
        chappie.research -> chappie.strategies "Evaluates strategy contracts offline"
        chappie.research -> chappie.runtimeState "Uses bounded caches/evidence"

        capitalSources -> chappie.capitalFlow "Supplies future offline inputs" "" "Partial"
        derivativesSources -> chappie.derivatives "Future offline input" "" "Planned"
        researchSources -> chappie.newsContext "Future offline input" "" "Planned"
        capitalSources -> chappie.macroContext "Future offline input" "" "Planned"
        onchainSources -> chappie.onchainContext "Future offline input" "" "Planned"
        cloudModels -> chappie.ai.cloudAdapter "Future structured inference" "" "Planned"
        chappie.capitalFlow -> chappie.research "Owns offline context contracts only" "" "Partial"
        chappie.derivatives -> chappie.research "Produces offline context only" "" "Planned"
        chappie.orderFlow -> chappie.research "Produces offline context only" "" "Planned"
        chappie.newsContext -> chappie.research "Produces offline context only" "" "Planned"
        chappie.macroContext -> chappie.research "Produces offline context only" "" "Planned"
        chappie.onchainContext -> chappie.research "Produces offline context only" "" "Planned"
    }

    views {
        systemContext chappie "system-context" "Chappie and its current/planned external systems" { include *; autoLayout lr }

        container chappie "core-runtime" "Core Runtime" { include operator chappie.runtime chappie.market chappie.decision chappie.strategies chappie.risk chappie.shadow binancePublic; autoLayout lr }
        container chappie "paper-runtime" "Paper Runtime" { include operator chappie.decision chappie.risk chappie.paper chappie.portfolio chappie.persistence storage; autoLayout lr }
        container chappie "testnet-runtime" "Testnet Runtime" { include operator chappie.decision chappie.risk chappie.exchangeRuntime chappie.exchangeExecution chappie.binanceInfrastructure chappie.persistence binanceTestnet storage; autoLayout lr }
        container chappie "research-platform" "Research Platform" { include operator chappie.research chappie.strategies chappie.runtimeState storage researchSources binancePublic; autoLayout lr }
        container chappie "ai-development" "AI and Development" { include operator vscode chappie.interfaces chappie.controlPlane chappie.development chappie.ai chappie.knowledge ollama storage; autoLayout lr }
        container chappie "planned-research-contexts" "Planned Research Contexts" { include chappie.research chappie.capitalFlow chappie.derivatives chappie.orderFlow chappie.newsContext chappie.macroContext chappie.onchainContext capitalSources derivativesSources onchainSources researchSources; autoLayout lr }

        component chappie.market "market-decision-components" "Market and Decision Runtime" { include chappie.market.* chappie.decision.* chappie.strategies chappie.risk binancePublic; autoLayout lr }
        component chappie.paper "paper-risk-attribution-components" "Paper, Risk, and Attribution" { include chappie.paper.* chappie.risk chappie.portfolio chappie.persistence; autoLayout lr }
        component chappie.exchangeRuntime "testnet-safety-components" "Testnet Submission, Reconciliation, and SAFE_STOP" { include chappie.exchangeRuntime.* chappie.exchangeExecution chappie.binanceInfrastructure chappie.risk chappie.persistence binanceTestnet; autoLayout lr }
        component chappie.research "strategy-oos-components" "Strategy Research and Frozen OOS" { include chappie.research.dataAuthority chappie.research.strategyResearch chappie.strategies storage binancePublic; autoLayout lr }
        component chappie.research "market-precursor-components" "Market Precursor Discovery" { include chappie.research.dataAuthority chappie.research.precursor storage binancePublic; autoLayout lr }
        component chappie.ai "ai-routing-components" "AI Model Routing" { include chappie.ai.* ollama cloudModels; autoLayout lr }
        component chappie.knowledge "knowledge-components" "Knowledge Build and Retrieval" { include chappie.knowledge.* storage; autoLayout lr }
        component chappie.development "development-intelligence-components" "Development Intelligence" { include chappie.development.* chappie.ai chappie.knowledge chappie.controlPlane; autoLayout lr }

        styles {
            element "Element" { color #1f2937; background #f8fafc; stroke #475569 }
            element "Implemented" { background #dbeafe; stroke #1d4ed8 }
            element "Partial" { background #fef3c7; stroke #d97706 }
            element "Experimental" { background #f3e8ff; stroke #7e22ce; border dashed }
            element "Historical" { background #e5e7eb; color #6b7280; stroke #9ca3af }
            element "Planned" { background #eff6ff; color #1e3a8a; stroke #60a5fa; border dashed }
            element "External" { background #f3f4f6; stroke #6b7280 }
            element "Research" { stroke #0f766e }
            element "SafetyCritical" { stroke #b91c1c; strokeWidth 4 }
            element "Data" { shape Cylinder }
            relationship "Relationship" { color #64748b; thickness 2 }
            relationship "Planned" { color #60a5fa; style dashed }
        }
    }
}
