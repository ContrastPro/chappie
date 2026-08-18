# Chappie AI v1

Chappie AI is a research-only reasoning subsystem shared by future agents. An
agent owns a workflow and responsibility; Chappie AI supplies model-backed,
provider-neutral interpretation through `ChappieAI.generate(...)`. Agents never
import Ollama or another concrete backend directly.

## Architecture and authority

The call boundary is:

`Agent → ChappieAI → ModelRouter → ModelProvider → backend`

AI may read supplied facts, interpret them, generate unvalidated hypotheses,
critique hypotheses, and request research. It may not alter source provenance,
events, timestamps, the frozen RDE methodology, Risk, orders, or execution state.
It cannot approve an order or declare deterministic research evidence. No AI
package imports an execution adapter.

The canonical output separates observed fact references, interpretations,
unvalidated hypotheses, contradictions, unknowns, evidence requests, and bounded
confidence. An observed fact is only a stable reference to an input `fact_id`;
unknown references fail closed. Interpretations remain model reasoning rather
than factual provenance. Hypotheses always begin `UNVALIDATED`; later RDE evidence
alone can support, reject, or contradict them.

Every request has an information cutoff. Input facts later than that cutoff are
rejected before a provider call. The `HistoricalEventAIContextV1` adapter
preserves event provenance and its cutoff, exposes no future outcomes, and has a
deterministic input identity.

## Providers and prompts

`ModelProvider` exposes provider/model identity, readiness, structured generation,
typed failures, and non-sensitive usage/latency metadata. `ModelRouter` maps model
profiles without changing callers. The current development profile is:

- profile: `LOCAL_DEFAULT`
- provider: Ollama at `http://localhost:11434`
- model: `qwen3:8b`

Endpoint, model, timeout, and temperature are configuration. Ollama credentials
do not exist. The adapter requests JSON-schema output, but Chappie independently
validates JSON shape, confidence, hypothesis status, and fact references.

Prompts are versioned as `news_interpretation/1.0.0`,
`market_interpretation/1.0.0`, `hypothesis_generation/1.0.0`, and
`hypothesis_critique/1.0.0`. Prompt identity and content contribute to the input
digest. They require supplied facts only, explicit unknowns/contradictions, no
invented causes, no trade instructions, and structured output only.

Provider chain-of-thought is neither required nor canonical. Ollama thinking is
disabled when supported and discarded if returned. It is not stored as evidence,
placed in a market hypothesis, or passed to Risk or execution. Observability is
limited to request/task/provider/model/prompt identity, latency, optional token
counts, digests, and validation status.

## Local development

Start Ollama separately and install the configured model, then run the optional
smoke test:

```bash
python -m app.tools.chappie_ai_smoke --provider ollama --model qwen3:8b
```

The smoke is opt-in and is not part of CI. Normal tests use `MockProvider` and
require no running model or network. The smoke prints validation metadata and
counts only; it never prints chain-of-thought.

V1 is request/validated-response only. It contains no agents, trading authority,
OpenAI or paid API integration, vector database, embeddings, semantic retrieval,
or long-term memory. Future providers and profiles may be added behind the same
router after separate review.
