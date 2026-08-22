import subprocess
import sys

import pytest

from app.ai.errors import AIValidationError
from app.ai.providers.mock import MockProvider
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.tasks import ModelProfile
from app.control_plane.workspace_context import WorkspaceContextV1, WorkspaceRangeV1
from app.knowledge import RepositorySnapshotV1, initial_records, manifest
from app.knowledge.retrieval import KnowledgeRetrievalRequestV1, retrieve


def test_knowledge_and_ai_imports_are_order_independent() -> None:
    for modules in (
        ("app.knowledge", "app.ai"),
        ("app.ai", "app.knowledge"),
        ("app.ai.project_qa",),
    ):
        statement = "; ".join(f"import {module}" for module in modules)
        completed = subprocess.run(
            [sys.executable, "-c", statement], capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, completed.stderr


def _context():
    s = RepositorySnapshotV1("a" * 40, True)
    r = initial_records(snapshot=s)
    return retrieve(
        r,
        manifest(r, s),
        KnowledgeRetrievalRequestV1(include_historical=True, topic_tokens=("multi",)),
    )[1]


def test_project_qa_is_source_bounded_and_preserves_negative_result():
    c = _context()
    fact = f"knowledge.{c.records[0].knowledge_id}.statement"
    out = {
        "answer": "The recorded result is negative.",
        "supporting_knowledge_fact_ids": [fact],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.5,
    }
    ai = ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: MockProvider(output=out)}), AIConfig())
    answer = ai.answer_project_qa(question="What do we know?", knowledge_context=c)
    assert answer.context_digest == c.context_digest


def test_project_qa_rejects_unknown_knowledge_fact():
    c = _context()
    out = {
        "answer": "unsupported",
        "supporting_knowledge_fact_ids": ["knowledge.fake.statement"],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.5,
    }
    ai = ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: MockProvider(output=out)}), AIConfig())
    with pytest.raises(AIValidationError, match="unknown knowledge"):
        ai.answer_project_qa(question="x", knowledge_context=c)


@pytest.mark.parametrize(
    "answer",
    ("What do we know?", "What do we know?!"),
)
def test_project_qa_rejects_exact_or_near_question_echo(answer: str):
    c = _context()
    out = {
        "answer": answer,
        "supporting_knowledge_fact_ids": [f"knowledge.{c.records[0].knowledge_id}.statement"],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.5,
    }
    ai = ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: MockProvider(output=out)}), AIConfig())
    with pytest.raises(AIValidationError, match="repeats the user question") as error:
        ai.answer_project_qa(question="What do we know?", knowledge_context=c)
    assert error.value.code == "PROJECT_QA_SYNTHESIS_INVALID"


def test_project_qa_accepts_substantive_grounded_answer():
    c = _context()
    out = {
        "answer": "The selected canonical record reports a negative result.",
        "supporting_knowledge_fact_ids": [f"knowledge.{c.records[0].knowledge_id}.statement"],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.5,
    }
    ai = ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: MockProvider(output=out)}), AIConfig())
    assert (
        ai.answer_project_qa(question="What do we know?", knowledge_context=c).answer
        == out["answer"]
    )


def test_project_qa_adds_compact_review_instruction_only_for_attached_source():
    c = _context()
    output = {
        "answer": "The selected canonical record reports a negative result.",
        "supporting_knowledge_fact_ids": [f"knowledge.{c.records[0].knowledge_id}.statement"],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.5,
    }
    provider = MockProvider(output=output)
    ai = ChappieAI(ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig())
    ai.answer_project_qa(question="What do we know?", knowledge_context=c)
    assert "bounded source review" not in provider.requests[-1].system_instruction

    ai.answer_project_qa(
        question="Review the attached source.",
        knowledge_context=c,
        workspace_context=WorkspaceContextV1(
            workspace_name="chappie",
            workspace_id="test",
            active_file_relative_path="app/knowledge/store.py",
            active_file_excerpt="class KnowledgeStore: ...",
            excerpt_range=WorkspaceRangeV1(1, 1),
        ),
    )
    assert "bounded source review" in provider.requests[-1].system_instruction
