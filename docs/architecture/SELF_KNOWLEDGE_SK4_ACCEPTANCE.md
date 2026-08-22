# Self-Knowledge SK-4 Acceptance

## Forensic baseline

The real local acceptance snapshot `9c302fba592e6222fcf861710daa6d288cb1fd9f` recorded
74 cases: 64 PASS and 10 FAIL. The primary evidence is the generated local file
`data/acceptance/development_copilot/result.json`; it is noncanonical evidence.

| Case | Retrieval / provider result | Runner result | Root cause |
| --- | --- | --- | --- |
| `ai_bypass_risk` | Correct AI authority records; provider answer exactly echoed question | FAIL | `MODEL_PROMPT_ECHO`, `SYNTHESIS_VALIDATION_GAP` |
| `knowledge_file` | Knowledge records selected; provider answer echoed question | false PASS | `MODEL_PROMPT_ECHO`, `ACCEPTANCE_GRADER_BUG` |
| `selection_store` | Knowledge records selected; provider answer echoed question | false PASS | `MODEL_PROMPT_ECHO`, `ACCEPTANCE_GRADER_BUG` |
| `research_orders` | No records selected; no provider call | FAIL | `RETRIEVAL_COMPOSITION_GAP` |
| `economic_calendar` | No records selected; no provider call | FAIL | `ZERO_KNOWLEDGE_POLICY_GAP`, `RETRIEVAL_COMPOSITION_GAP` |
| `unknown_news`, `unknown_calendar`, `unknown_memory` | No records selected; no provider call | FAIL | `ZERO_KNOWLEDGE_POLICY_GAP` |
| `git_honesty` | No records selected; no provider call despite bounded Git context | FAIL | `CONTEXT_ONLY_REASONING_GAP` |
| `session_13` | Same echo as `ai_bypass_risk` | false PASS | `ACCEPTANCE_GRADER_BUG` |

The Project QA parser does not project the question into an answer: it validates the provider's
structured `answer` field unchanged. Therefore the recorded exact echoes originate in model output,
not parser fallback or UI projection. The prior prompt did not clearly separate QUESTION, canonical
facts, workspace observations, and the required answer role.

## SK-4 policy

- A question echo is a typed synthesis failure.
- Current implementation questions with no canonical record return an honest, deterministic
  `UNKNOWN_CURRENT_CAPABILITY` result without calling AI.
- A bounded Git snapshot can produce a `CONTEXT_OBSERVATION`, explicitly labelled as workspace
  evidence rather than canonical Knowledge.
- Architecture/design intent composes a bounded set of general governance records; it does not
  create feature-specific facts.
- The acceptance runner rejects echoes everywhere and requires substantive file/selection analysis.

No News, Economic Calendar, or other feature-specific Knowledge record is introduced by SK-4.
