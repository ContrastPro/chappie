# Multi-model runtime foundation

Chappie's backend-owned local-model policy approves `QWEN3_4B` (`qwen3:4b`) and
`QWEN3_14B` (`qwen3:14b`). Model selection is request-local: Control Plane validates a
stable model ID, resolves its descriptor, and builds an independent AI runtime for that request.
It never mutates durable provider, profile, model, or timeout configuration.

Execution budgets are policy-owned and bounded: 4B uses 45 seconds; 14B uses 75 seconds.
Clients cannot supply an arbitrary timeout. The provider receives the selected request budget;
there is no retry or fallback to another model. A following default request remains 4B/45 seconds.

`GET /v1/ai/models` projects only approved local models. It is read-only and does not expose raw
Ollama inventory. Workbench model selection remains a future consumer of these backend contracts.
