from __future__ import annotations

from pathlib import Path

from app.ai.providers import MockMode, MockProvider
from app.ai.router import ModelRouter
from app.ai.runtime import AIConfig, ChappieAI
from app.ai.tasks import ModelProfile
from app.bootstrap import ChappieLocalConfigV1
from app.knowledge import (
    KnowledgeTypeV1,
    RepositorySnapshotV1,
    initial_records,
    manifest,
    write_store,
)
from app.project_qa_setup_smoke import (
    ProjectQASetupSmokeStateV1,
    ProjectQASetupSmokeV1,
)


def _config() -> ChappieLocalConfigV1:
    return ChappieLocalConfigV1(
        provider="ollama", model="configured-model:1", enabled_capabilities=("BALANCED",)
    )


def _store(tmp_path: Path) -> tuple[Path, str]:
    records = initial_records(snapshot=RepositorySnapshotV1("a" * 40, True))
    store = tmp_path / "knowledge.json"
    write_store(store, records, manifest(records, RepositorySnapshotV1("a" * 40, True)))
    authority = next(
        item for item in records if item.knowledge_type == KnowledgeTypeV1.AUTHORITY_BOUNDARY
    )
    return store, authority.knowledge_id


def _smoke(provider: MockProvider) -> ProjectQASetupSmokeV1:
    def build(config: ChappieLocalConfigV1) -> ChappieAI:
        return ChappieAI(
            ModelRouter({ModelProfile.LOCAL_DEFAULT: provider}), AIConfig(model=config.model or "")
        )

    return ProjectQASetupSmokeV1(ai_builder=build)


def _valid_output(knowledge_id: str) -> dict[str, object]:
    return {
        "answer": "RDE owns research evidence authority.",
        "supporting_knowledge_fact_ids": [f"knowledge.{knowledge_id}.statement"],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.5,
    }


def test_valid_project_qa_smoke_is_grounded_and_read_only(tmp_path: Path) -> None:
    store, knowledge_id = _store(tmp_path)
    provider = MockProvider(output=_valid_output(knowledge_id))
    before = store.read_bytes()

    result = _smoke(provider).run(_config(), store)

    assert result.state == ProjectQASetupSmokeStateV1.VALID
    assert result.selected_knowledge_ids == (knowledge_id,)
    assert result.context_digest
    assert len(provider.requests) == 1
    assert store.read_bytes() == before


def test_unknown_fact_and_wrong_authority_are_rejected(tmp_path: Path) -> None:
    store, knowledge_id = _store(tmp_path)
    unknown = MockProvider(
        output={**_valid_output(knowledge_id), "supporting_knowledge_fact_ids": ["knowledge.bad"]}
    )
    wrong = MockProvider(
        output={**_valid_output(knowledge_id), "answer": "AI owns research evidence authority."}
    )

    assert (
        _smoke(unknown).run(_config(), store).state == ProjectQASetupSmokeStateV1.VALIDATION_ERROR
    )
    assert _smoke(wrong).run(_config(), store).state == ProjectQASetupSmokeStateV1.GROUNDING_ERROR


def test_provider_failures_and_invalid_config_fail_closed(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    for mode, expected in (
        (MockMode.ERROR, ProjectQASetupSmokeStateV1.PROVIDER_ERROR),
        (MockMode.TIMEOUT, ProjectQASetupSmokeStateV1.TIMEOUT),
    ):
        provider = MockProvider(mode)
        assert _smoke(provider).run(_config(), store).state == expected
        assert len(provider.requests) == 1

    provider = MockProvider()
    assert _smoke(provider).run(ChappieLocalConfigV1(), store).state == (
        ProjectQASetupSmokeStateV1.CONFIG_ERROR
    )
    assert provider.requests == []


def test_invalid_knowledge_fails_before_provider_call(tmp_path: Path) -> None:
    provider = MockProvider()

    result = _smoke(provider).run(_config(), tmp_path / "missing.json")

    assert result.state == ProjectQASetupSmokeStateV1.KNOWLEDGE_ERROR
    assert provider.requests == []
