"""Opt-in local Project QA; never invoked by tests."""

import argparse

from app.ai.providers import OllamaProvider
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.tasks import ModelProfile
from app.knowledge import committed_snapshot, initial_records, manifest
from app.knowledge_retrieval import KnowledgeRetrievalRequestV1, retrieve


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--provider", choices=("ollama",), required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args()
    s = committed_snapshot()
    records = initial_records(snapshot=s)
    _, context = retrieve(
        records,
        manifest(records, s),
        KnowledgeRetrievalRequestV1(
            include_historical=True, topic_tokens=tuple(a.question.casefold().split())
        ),
    )
    if a.verbose:
        print(context.context_digest, [x.knowledge_id for x in context.records])
    ai = ChappieAI(
        ModelRouter({ModelProfile.LOCAL_DEFAULT: OllamaProvider(model=a.model)}),
        AIConfig(model=a.model),
    )
    try:
        print(ai.answer_project_qa(question=a.question, knowledge_context=context).payload())
    finally:
        ai.close()


if __name__ == "__main__":
    main()
