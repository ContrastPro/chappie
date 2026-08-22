# Local Development Copilot acceptance

Run outside the Codex sandbox after a clean repository, ready canonical Knowledge, and ready
local Ollama/qwen3:4b:

```sh
.venv/bin/python tools/acceptance/chappie_copilot_acceptance.py
```

It calls the production Control Plane and `WorkspaceContextV1` contract using real repository
files, selections, and Git metadata. Results are written under
`data/acceptance/development_copilot/` as local, noncanonical `result.json` and `report.md`.
No internet, package installation, model download, or production-code modification is performed.

The run includes the semantic matrix plus a separate 30-turn mixed session, question limits up
to the production 12,000-character boundary, and WorkspaceContext limits up to its production
12,000-character boundary. Runtime is model-dependent and may take several minutes. Known-invalid
boundary cases are rejected before model inference where the production contract permits it.

The runner grades generated responses consistently across primary and session cases: a prompt echo,
empty synthesis, hard-authority violation, or non-substantive file/selection review is not a PASS.
Typed `UNKNOWN_CURRENT_CAPABILITY` responses are accepted only for questions that explicitly ask
about an unsupported current implementation.

## qwen3:14b one-command diagnostic

Run the bounded local diagnostic without changing configuration or installing models:

```sh
.venv/bin/python tools/acceptance/run_qwen3_14b_diagnostic.py
```

It writes ignored local evidence to `data/acceptance/model_ab/diagnostic/`, including
`report.md` and `result.json`.

## Focused qwen3:4b versus qwen3:14b A/B

```sh
.venv/bin/python tools/acceptance/run_model_ab.py
```

This explicitly selects each model for the same focused review/control cases and writes ignored
local evidence to `data/acceptance/model_ab/focused/report.md` and `result.json`.
