from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.research.capital_flow.free_v2_analytical_view import (
    ANALYTICAL_VIEW_ENGINE_STATUS,
    ANALYTICAL_VIEW_IDENTITY,
    AUTHORITATIVE_FEATURE_DATASET_STATUS,
    CAPITAL_FLOW_STATUS,
    CHECKPOINT_NAME,
    CONVERSION_IDENTITY,
    PAPER_TRADING_STATUS,
    PARQUET_COMPRESSION,
    PARQUET_NAME,
    PARTITION_SCHEME,
    PRODUCTION_PLAN,
    REAL_ANALYTICAL_VIEW_STATUS,
    RESEARCH_STATUS,
    ROW_GROUP_ROWS,
    SOURCE_ARTIFACT_SHA256,
    SOURCE_DATASET_SEMANTIC_DIGEST,
    STATE_CODE_BY_NAME,
    STATE_SUFFIX,
    TSUNAMI_EXPERIMENT_CONTRACT_STATUS,
    VALIDATION_STATUS,
    AnalyticalViewPlanV1,
    analytical_selection,
    materialize_analytical_view,
    verify_analytical_view,
)
from app.research.capital_flow.free_v2_formulas import FEATURE_UNAVAILABLE_STATES
from app.research.capital_flow.free_v2_materializer import (
    AVAILABLE,
    CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
    FEATURE_ORDER,
)
from app.tools.capital_flow_free_v2_analytical_view import SOURCE_ROOT, build_parser


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fixture_source(root: Path, start: datetime, minutes: int) -> AnalyticalViewPlanV1:
    root.mkdir()
    artifact = root / "features.ndjson"
    state_cycle = (AVAILABLE, *FEATURE_UNAVAILABLE_STATES)
    counts = {name: {state: 0 for state in state_cycle} for name in FEATURE_ORDER}
    with artifact.open("wb") as handle:
        for minute in range(minutes):
            timestamp = start + timedelta(minutes=minute)
            for index, name in enumerate(FEATURE_ORDER):
                state = state_cycle[(minute + index) % len(state_cycle)]
                counts[name][state] += 1
                handle.write(
                    _canonical(
                        {
                            "feature_name": name,
                            "timestamp_utc": timestamp.isoformat(),
                            "value": str(minute + index / 10) if state == AVAILABLE else None,
                            "state": state,
                        }
                    )
                )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    dataset_digest = "1" * 64
    manifest = {
        "dataset_identity": CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
        "dataset_semantic_digest": dataset_digest,
        "feature_order": list(FEATURE_ORDER),
        "artifact": {"file": "features.ndjson", "sha256": digest},
        "quality": [
            {
                "feature_name": name,
                "available_count": counts[name][AVAILABLE],
                **{
                    state.lower() + "_count": counts[name][state]
                    for state in FEATURE_UNAVAILABLE_STATES
                },
            }
            for name in FEATURE_ORDER
        ],
    }
    manifest["manifest_semantic_digest"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return AnalyticalViewPlanV1(
        source_dataset_semantic_digest=dataset_digest,
        source_artifact_sha256=digest,
        timeline_start=start,
        timeline_end_exclusive=start + timedelta(minutes=minutes),
        timeline_rows=minutes,
    )


@pytest.fixture
def published(tmp_path: Path) -> tuple[Path, Path, AnalyticalViewPlanV1, dict[str, object]]:
    source = tmp_path / "source"
    output = tmp_path / "analytical"
    plan = _fixture_source(source, datetime(2024, 1, 31, 23, 58, tzinfo=UTC), 5)
    manifest = materialize_analytical_view(source, output, plan=plan)
    return source, output, plan, manifest


def test_production_identity_and_source_bindings_are_exact() -> None:
    assert ANALYTICAL_VIEW_IDENTITY == "CHAPPIE_CAPITAL_FLOW_FREE_V2_ANALYTICAL_VIEW/1.0.0"
    assert CONVERSION_IDENTITY.endswith("/1.0.0")
    assert PRODUCTION_PLAN.source_dataset_identity == CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY
    assert PRODUCTION_PLAN.source_dataset_semantic_digest == SOURCE_DATASET_SEMANTIC_DIGEST
    assert PRODUCTION_PLAN.source_artifact_sha256 == SOURCE_ARTIFACT_SHA256
    assert PRODUCTION_PLAN.feature_order == FEATURE_ORDER
    assert (
        AUTHORITATIVE_FEATURE_DATASET_STATUS,
        TSUNAMI_EXPERIMENT_CONTRACT_STATUS,
        ANALYTICAL_VIEW_ENGINE_STATUS,
        REAL_ANALYTICAL_VIEW_STATUS,
        RESEARCH_STATUS,
        VALIDATION_STATUS,
        PAPER_TRADING_STATUS,
        CAPITAL_FLOW_STATUS,
    ) == (
        "VALIDATED",
        "RELEASED",
        "IMPLEMENTED",
        "NOT_YET_MATERIALIZED",
        "NOT_RUN",
        "NOT_ACCESSED",
        "NOT_RUN",
        "PARTIAL",
    )
    with pytest.raises(TypeError):
        STATE_CODE_BY_NAME[AVAILABLE] = 99  # type: ignore[index]


def test_wide_parquet_schema_state_mapping_and_partitioning(
    published: tuple[Path, Path, AnalyticalViewPlanV1, dict[str, object]],
) -> None:
    _, output, _, manifest = published
    assert manifest["representation"]["partition_scheme"] == PARTITION_SCHEME
    assert manifest["representation"]["compression"] == PARQUET_COMPRESSION
    assert manifest["representation"]["row_group_rows"] == ROW_GROUP_ROWS
    assert manifest["representation"]["state_code_mapping"] == STATE_CODE_BY_NAME
    assert manifest["partition_count"] == 2
    assert [item["month"] for item in manifest["partitions"]] == ["2024-01", "2024-02"]
    parquet = pq.ParquetFile(output / manifest["partitions"][0]["relative_path"]).read()
    assert parquet.schema.names == [
        "timestamp_utc",
        *(item for name in FEATURE_ORDER for item in (name, name + STATE_SUFFIX)),
    ]
    assert parquet.column(FEATURE_ORDER[0]).to_pylist()[0] == 0.0
    assert parquet.column(FEATURE_ORDER[1]).to_pylist()[0] is None
    assert parquet.column(FEATURE_ORDER[1] + STATE_SUFFIX).to_pylist()[0] != 0


def test_quality_timeline_and_no_target_columns(
    published: tuple[Path, Path, AnalyticalViewPlanV1, dict[str, object]],
) -> None:
    _, _, _, manifest = published
    assert manifest["total_rows"] == 5
    assert manifest["quality"] == {
        **manifest["quality"],
        "timestamps_unique": True,
        "timeline_continuity": "PASS",
        "feature_columns": 15,
        "state_columns": 15,
        "targets_present": False,
    }
    assert not any(
        token in column.lower()
        for column in manifest["schema"]
        for token in ("target", "future", "probability")
    )


def test_manifest_last_create_only_and_deterministic_conversion(
    published: tuple[Path, Path, AnalyticalViewPlanV1, dict[str, object]], tmp_path: Path
) -> None:
    source, output, plan, first = published
    with pytest.raises(FileExistsError, match="create-only"):
        materialize_analytical_view(source, output, plan=plan)
    second_root = tmp_path / "second"
    second = materialize_analytical_view(source, second_root, plan=plan)
    assert first["analytical_semantic_digest"] == second["analytical_semantic_digest"]
    assert [item["semantic_digest"] for item in first["partitions"]] == [
        item["semantic_digest"] for item in second["partitions"]
    ]


def test_partition_tamper_is_detected(
    published: tuple[Path, Path, AnalyticalViewPlanV1, dict[str, object]],
) -> None:
    _, output, plan, manifest = published
    artifact = output / manifest["partitions"][0]["relative_path"]
    with artifact.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="partition digest"):
        verify_analytical_view(output, plan=plan)


def test_source_digest_mismatch_fails_before_conversion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    plan = _fixture_source(source, datetime(2024, 1, 1, tzinfo=UTC), 1)
    bad = AnalyticalViewPlanV1(
        source_dataset_semantic_digest=plan.source_dataset_semantic_digest,
        source_artifact_sha256="f" * 64,
        timeline_start=plan.timeline_start,
        timeline_end_exclusive=plan.timeline_end_exclusive,
        timeline_rows=plan.timeline_rows,
    )
    with pytest.raises(ValueError, match="artifact digest binding"):
        materialize_analytical_view(source, output, plan=bad)
    assert not output.exists()


def test_verified_month_resume_and_corrupt_checkpoint_rejection(
    published: tuple[Path, Path, AnalyticalViewPlanV1, dict[str, object]],
) -> None:
    source, output, plan, first = published
    (output / "manifest.json").unlink()
    resumed = materialize_analytical_view(source, output, plan=plan)
    assert resumed["analytical_semantic_digest"] == first["analytical_semantic_digest"]
    (output / "manifest.json").unlink()
    checkpoint = output / "partitions/year=2024/month=01" / CHECKPOINT_NAME
    checkpoint.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt or incompatible"):
        materialize_analytical_view(source, output, plan=plan)


def test_partial_month_checkpoint_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    plan = _fixture_source(source, datetime(2024, 1, 1, tzinfo=UTC), 1)
    month = output / "partitions/year=2024/month=01"
    month.mkdir(parents=True)
    (month / PARQUET_NAME).write_bytes(b"partial")
    with pytest.raises(ValueError, match="partial analytical month"):
        materialize_analytical_view(source, output, plan=plan)


def test_research_validation_selection_helpers_are_disjoint(tmp_path: Path) -> None:
    research = analytical_selection(tmp_path, "RESEARCH", (FEATURE_ORDER[0],))
    validation = analytical_selection(tmp_path, "VALIDATION", (FEATURE_ORDER[0],))
    assert research.end_exclusive == validation.start_inclusive
    assert research.start_inclusive < research.end_exclusive <= validation.end_exclusive
    assert research.columns == (
        "timestamp_utc",
        FEATURE_ORDER[0],
        FEATURE_ORDER[0] + STATE_SUFFIX,
    )
    assert "read_parquet" in research.duckdb_sql()
    assert "ORDER BY timestamp_utc" in research.duckdb_sql()


def test_operator_cli_defaults_to_durable_e_volume_and_does_not_run_while_parsing() -> None:
    args = build_parser().parse_args([])
    assert args.source_root == SOURCE_ROOT
    assert args.output_root.drive == "E:"
