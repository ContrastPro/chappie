"""Deterministic feature-only Parquet view of the validated Capital Flow Free V2 dataset."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Literal

from .free_v2_formulas import (
    CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY,
    FEATURE_TIMELINE_END_EXCLUSIVE,
    FEATURE_TIMELINE_MINUTES,
    FEATURE_TIMELINE_START,
    FEATURE_UNAVAILABLE_STATES,
)
from .free_v2_materializer import (
    AVAILABLE,
    CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
    FEATURE_ORDER,
)

ANALYTICAL_VIEW_IDENTITY = "CHAPPIE_CAPITAL_FLOW_FREE_V2_ANALYTICAL_VIEW/1.0.0"
CONVERSION_IDENTITY = "CHAPPIE_CAPITAL_FLOW_FREE_V2_NDJSON_TO_WIDE_PARQUET/1.0.0"
SOURCE_DATASET_SEMANTIC_DIGEST = "07898bfdc7fec01c249bd38b479624bcdfc353237e4d8ffcf0015fafb4977e23"
SOURCE_ARTIFACT_SHA256 = "ad9ec8ec8e91375c7a579536b23a51f93cd51246685dc3e447501e7f7d6eeaed"
ANALYTICAL_ROOT = Path("E:/projects/chappie/data/research/analytical/capital_flow/free_v2")
PARTITION_SCHEME = "year=YYYY/month=MM"
PARQUET_COMPRESSION = "zstd"
PARQUET_COMPRESSION_LEVEL = 9
PARQUET_VERSION = "2.6"
ROW_GROUP_ROWS = 10_080
STATE_SUFFIX = "__state"
MANIFEST_NAME = "manifest.json"
CHECKPOINT_NAME = "checkpoint.json"
PARQUET_NAME = "data.parquet"
TARGET_COLUMN_TOKENS = (
    "target",
    "future",
    "sigma_h",
    "up_tsunami",
    "down_tsunami",
    "probability",
    "forecast",
)
AUTHORITATIVE_FEATURE_DATASET_STATUS = "VALIDATED"
TSUNAMI_EXPERIMENT_CONTRACT_STATUS = "RELEASED"
ANALYTICAL_VIEW_ENGINE_STATUS = "IMPLEMENTED"
REAL_ANALYTICAL_VIEW_STATUS = "NOT_YET_MATERIALIZED"
RESEARCH_STATUS = "NOT_RUN"
VALIDATION_STATUS = "NOT_ACCESSED"
PAPER_TRADING_STATUS = "NOT_RUN"
CAPITAL_FLOW_STATUS = "PARTIAL"

STATE_CODE_ITEMS = tuple(enumerate((AVAILABLE, *FEATURE_UNAVAILABLE_STATES)))
STATE_CODE_BY_NAME = MappingProxyType({name: code for code, name in STATE_CODE_ITEMS})
STATE_NAME_BY_CODE = MappingProxyType(dict(STATE_CODE_ITEMS))


def _canonical(value: object, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        + "\n"
    ).encode()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_minute(value: object) -> datetime:
    result = datetime.fromisoformat(str(value))
    if (
        result.tzinfo is None
        or result.utcoffset() != timedelta(0)
        or result.second
        or result.microsecond
    ):
        raise ValueError("analytical timestamp must be an exact UTC minute")
    return result.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AnalyticalViewPlanV1:
    source_dataset_identity: str = CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY
    source_dataset_semantic_digest: str = SOURCE_DATASET_SEMANTIC_DIGEST
    source_artifact_sha256: str = SOURCE_ARTIFACT_SHA256
    timeline_start: datetime = FEATURE_TIMELINE_START
    timeline_end_exclusive: datetime = FEATURE_TIMELINE_END_EXCLUSIVE
    timeline_rows: int = FEATURE_TIMELINE_MINUTES
    feature_order: tuple[str, ...] = FEATURE_ORDER

    def __post_init__(self) -> None:
        if not self.source_dataset_identity or len(self.source_dataset_semantic_digest) != 64:
            raise ValueError("invalid source dataset binding")
        if len(self.source_artifact_sha256) != 64:
            raise ValueError("invalid source artifact binding")
        if self.timeline_rows <= 0 or self.timeline_end_exclusive <= self.timeline_start:
            raise ValueError("invalid analytical timeline")
        if self.feature_order != FEATURE_ORDER:
            raise ValueError("analytical view requires the exact frozen feature order")

    def payload(self) -> dict[str, object]:
        return {
            "analytical_view_identity": ANALYTICAL_VIEW_IDENTITY,
            "conversion_identity": CONVERSION_IDENTITY,
            "source_dataset_identity": self.source_dataset_identity,
            "source_dataset_semantic_digest": self.source_dataset_semantic_digest,
            "source_artifact_sha256": self.source_artifact_sha256,
            "formula_identity": CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY,
            "feature_order": list(self.feature_order),
            "timeline": {
                "start": self.timeline_start.isoformat(),
                "end_exclusive": self.timeline_end_exclusive.isoformat(),
                "rows": self.timeline_rows,
                "frequency": "PT1M",
            },
            "representation": {
                "format": "parquet",
                "layout": "wide_feature_value_and_state_columns",
                "partition_scheme": PARTITION_SCHEME,
                "compression": PARQUET_COMPRESSION,
                "compression_level": PARQUET_COMPRESSION_LEVEL,
                "parquet_version": PARQUET_VERSION,
                "row_group_rows": ROW_GROUP_ROWS,
                "numeric": "IEEE_754_BINARY64_FINITE_AVAILABLE_ONLY",
                "unavailable_numeric": "NULL",
                "timestamp": "TIMESTAMP_US_UTC",
                "state_code_mapping": dict(STATE_CODE_BY_NAME),
            },
        }


PRODUCTION_PLAN = AnalyticalViewPlanV1()


@dataclass(slots=True)
class _MonthBuffer:
    key: str
    timestamps: list[datetime] = field(default_factory=list)
    values: dict[str, list[float | None]] = field(
        default_factory=lambda: {name: [] for name in FEATURE_ORDER}
    )
    states: dict[str, list[int]] = field(
        default_factory=lambda: {name: [] for name in FEATURE_ORDER}
    )
    semantic_digest: object = field(default_factory=hashlib.sha256)

    def add(self, timestamp: datetime, rows: list[dict[str, object]]) -> None:
        self.timestamps.append(timestamp)
        semantic: dict[str, object] = {"timestamp_utc": timestamp.isoformat()}
        for name, row in zip(FEATURE_ORDER, rows, strict=True):
            state = str(row.get("state"))
            if state not in STATE_CODE_BY_NAME:
                raise ValueError("unknown feature state")
            raw_value = row.get("value")
            if state == AVAILABLE:
                if raw_value is None:
                    raise ValueError("available feature requires a value")
                numeric = float(Decimal(str(raw_value)))
                if not math.isfinite(numeric):
                    raise ValueError("available feature must convert to finite float64")
            else:
                if raw_value is not None:
                    raise ValueError("unavailable feature value must be null")
                numeric = None
            code = STATE_CODE_BY_NAME[state]
            self.values[name].append(numeric)
            self.states[name].append(code)
            semantic[name] = numeric
            semantic[name + STATE_SUFFIX] = code
        self.semantic_digest.update(_canonical(semantic))  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class AnalyticalSelectionV1:
    parquet_glob: str
    columns: tuple[str, ...]
    start_inclusive: datetime
    end_exclusive: datetime

    def duckdb_sql(self) -> str:
        quoted = ", ".join(f'"{name}"' for name in self.columns)
        return (
            f"SELECT {quoted} FROM read_parquet('{self.parquet_glob}', hive_partitioning=false) "
            f"WHERE timestamp_utc >= TIMESTAMPTZ '{self.start_inclusive.isoformat()}' "
            f"AND timestamp_utc < TIMESTAMPTZ '{self.end_exclusive.isoformat()}' "
            "ORDER BY timestamp_utc"
        )


def analytical_selection(
    root: Path,
    split: Literal["RESEARCH", "VALIDATION"],
    features: tuple[str, ...] = FEATURE_ORDER,
    *,
    include_states: bool = True,
) -> AnalyticalSelectionV1:
    """Return a frozen DuckDB-compatible selection without opening any outcome data."""
    if not features or any(name not in FEATURE_ORDER for name in features):
        raise ValueError("selection contains an unknown feature")
    if split == "RESEARCH":
        start, end = FEATURE_TIMELINE_START, datetime(2025, 6, 1, tzinfo=UTC)
    elif split == "VALIDATION":
        start, end = datetime(2025, 6, 1, tzinfo=UTC), FEATURE_TIMELINE_END_EXCLUSIVE
    else:
        raise ValueError("split must be RESEARCH or VALIDATION")
    columns = ("timestamp_utc", *features)
    if include_states:
        columns += tuple(name + STATE_SUFFIX for name in features)
    return AnalyticalSelectionV1(
        (root / "partitions" / "year=*" / "month=*" / PARQUET_NAME).as_posix(),
        columns,
        start,
        end,
    )


def _schema():  # type: ignore[no-untyped-def]
    import pyarrow as pa

    fields = [pa.field("timestamp_utc", pa.timestamp("us", tz="UTC"), nullable=False)]
    for name in FEATURE_ORDER:
        fields.extend(
            (
                pa.field(name, pa.float64(), nullable=True),
                pa.field(name + STATE_SUFFIX, pa.uint8(), nullable=False),
            )
        )
    metadata = {
        b"analytical_view_identity": ANALYTICAL_VIEW_IDENTITY.encode(),
        b"conversion_identity": CONVERSION_IDENTITY.encode(),
    }
    return pa.schema(fields, metadata=metadata)


def _month_path(root: Path, key: str) -> Path:
    year, month = key.split("-")
    return root / "partitions" / f"year={year}" / f"month={month}"


def _parquet_semantic_digest(path: Path) -> str:
    import pyarrow.parquet as pq

    digest = hashlib.sha256()
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=ROW_GROUP_ROWS):
        columns = batch.to_pydict()
        for index, raw_timestamp in enumerate(columns["timestamp_utc"]):
            timestamp = raw_timestamp.astimezone(UTC)
            semantic: dict[str, object] = {"timestamp_utc": timestamp.isoformat()}
            for name in FEATURE_ORDER:
                value = columns[name][index]
                code = columns[name + STATE_SUFFIX][index]
                if code not in STATE_NAME_BY_CODE:
                    raise ValueError("analytical partition contains an unknown state code")
                if (code == STATE_CODE_BY_NAME[AVAILABLE]) != (value is not None):
                    raise ValueError("analytical partition state/value inconsistency")
                if value is not None and not math.isfinite(value):
                    raise ValueError("analytical partition contains a nonfinite value")
                semantic[name] = value
                semantic[name + STATE_SUFFIX] = code
            digest.update(_canonical(semantic))
    return digest.hexdigest()


def _validate_parquet_shape(path: Path, row_count: int, row_groups: int) -> None:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    if parquet.schema_arrow != _schema():
        raise ValueError("analytical partition schema mismatch")
    if parquet.metadata.num_rows != row_count:
        raise ValueError("analytical partition row count mismatch")
    if parquet.metadata.num_row_groups != row_groups:
        raise ValueError("analytical partition row-group mismatch")


def _write_month(root: Path, buffer: _MonthBuffer, contract_digest: str) -> dict[str, object]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = _month_path(root, buffer.key)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / PARQUET_NAME
    checkpoint = directory / CHECKPOINT_NAME
    semantic_digest = buffer.semantic_digest.hexdigest()  # type: ignore[attr-defined]
    if artifact.exists() or checkpoint.exists():
        if not (artifact.exists() and checkpoint.exists()):
            raise ValueError("partial analytical month checkpoint")
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        expected_groups = math.ceil(len(buffer.timestamps) / ROW_GROUP_ROWS)
        if (
            saved.get("contract_digest") != contract_digest
            or saved.get("month") != buffer.key
            or saved.get("relative_path") != artifact.relative_to(root).as_posix()
            or saved.get("row_count") != len(buffer.timestamps)
            or saved.get("row_groups") != expected_groups
            or saved.get("semantic_digest") != semantic_digest
            or saved.get("parquet_sha256") != _sha_file(artifact)
            or _parquet_semantic_digest(artifact) != semantic_digest
        ):
            raise ValueError("corrupt or incompatible analytical month checkpoint")
        _validate_parquet_shape(artifact, len(buffer.timestamps), expected_groups)
        return saved
    arrays: dict[str, object] = {"timestamp_utc": buffer.timestamps}
    for name in FEATURE_ORDER:
        arrays[name] = buffer.values[name]
        arrays[name + STATE_SUFFIX] = buffer.states[name]
    table = pa.Table.from_pydict(arrays, schema=_schema())
    pending = directory / (PARQUET_NAME + ".pending")
    pq.write_table(
        table,
        pending,
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_COMPRESSION_LEVEL,
        row_group_size=ROW_GROUP_ROWS,
        use_dictionary=False,
        write_statistics=True,
        version=PARQUET_VERSION,
    )
    os.replace(pending, artifact)
    expected_groups = math.ceil(len(buffer.timestamps) / ROW_GROUP_ROWS)
    _validate_parquet_shape(artifact, len(buffer.timestamps), expected_groups)
    if _parquet_semantic_digest(artifact) != semantic_digest:
        raise ValueError("written analytical month is not semantically equivalent")
    result = {
        "month": buffer.key,
        "relative_path": artifact.relative_to(root).as_posix(),
        "row_count": len(buffer.timestamps),
        "byte_size": artifact.stat().st_size,
        "parquet_sha256": _sha_file(artifact),
        "semantic_digest": semantic_digest,
        "row_groups": expected_groups,
        "contract_digest": contract_digest,
    }
    checkpoint.write_bytes(_canonical(result, pretty=True))
    return result


def _validate_source_manifest(
    source_root: Path, plan: AnalyticalViewPlanV1
) -> tuple[Path, dict[str, object]]:
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    manifest_digest = manifest.pop("manifest_semantic_digest", None)
    if manifest_digest != hashlib.sha256(_canonical(manifest)).hexdigest():
        raise ValueError("source manifest semantic digest mismatch")
    if manifest.get("dataset_identity") != plan.source_dataset_identity:
        raise ValueError("source dataset identity mismatch")
    if manifest.get("dataset_semantic_digest") != plan.source_dataset_semantic_digest:
        raise ValueError("source dataset semantic digest mismatch")
    if manifest.get("feature_order") != list(plan.feature_order):
        raise ValueError("source feature order mismatch")
    artifact = manifest.get("artifact", {})
    if artifact.get("file") != "features.ndjson":
        raise ValueError("source artifact name mismatch")
    if artifact.get("sha256") != plan.source_artifact_sha256:
        raise ValueError("source artifact digest binding mismatch")
    return source_root / "features.ndjson", {
        **manifest,
        "manifest_semantic_digest": manifest_digest,
    }


def _source_quality_counts(manifest: dict[str, object]) -> dict[str, dict[str, int]] | None:
    quality = manifest.get("quality")
    if quality is None:
        return None
    if not isinstance(quality, list):
        raise TypeError("source quality evidence malformed")
    result: dict[str, dict[str, int]] = {}
    for raw in quality:
        if not isinstance(raw, dict) or raw.get("feature_name") not in FEATURE_ORDER:
            raise ValueError("source quality evidence malformed")
        name = str(raw["feature_name"])
        result[name] = {AVAILABLE: int(raw["available_count"])}
        result[name].update(
            {state: int(raw[state.lower() + "_count"]) for state in FEATURE_UNAVAILABLE_STATES}
        )
    if tuple(result) != FEATURE_ORDER:
        raise ValueError("source quality feature order mismatch")
    return result


def _read_group(handle: BinaryIO, source_hash: object) -> tuple[list[dict[str, object]], bool]:
    rows = []
    for _ in FEATURE_ORDER:
        raw = handle.readline()
        if not raw:
            return rows, True
        source_hash.update(raw)  # type: ignore[attr-defined]
        rows.append(json.loads(raw))
    return rows, False


def materialize_analytical_view(
    source_root: Path,
    output_root: Path = ANALYTICAL_ROOT,
    *,
    plan: AnalyticalViewPlanV1 = PRODUCTION_PLAN,
) -> dict[str, object]:
    """Convert source NDJSON once, using bounded monthly buffers and manifest-last publication."""
    if (output_root / MANIFEST_NAME).exists():
        raise FileExistsError("valid analytical manifest already exists; output is create-only")
    artifact, source_manifest = _validate_source_manifest(source_root, plan)
    output_root.mkdir(parents=True, exist_ok=True)
    contract = plan.payload()
    contract_digest = hashlib.sha256(_canonical(contract)).hexdigest()
    source_hash = hashlib.sha256()
    state_counts = {name: {state: 0 for state in STATE_CODE_BY_NAME} for name in FEATURE_ORDER}
    partitions: list[dict[str, object]] = []
    expected_timestamp = plan.timeline_start
    buffer: _MonthBuffer | None = None
    total_rows = 0
    with artifact.open("rb") as handle:
        while True:
            rows, ended = _read_group(handle, source_hash)
            if ended:
                if rows:
                    raise ValueError("source ends with a partial feature timestamp")
                break
            timestamp = _utc_minute(rows[0].get("timestamp_utc"))
            if timestamp != expected_timestamp:
                raise ValueError("source timestamp continuity mismatch")
            for name, row in zip(FEATURE_ORDER, rows, strict=True):
                if (
                    row.get("feature_name") != name
                    or _utc_minute(row.get("timestamp_utc")) != timestamp
                ):
                    raise ValueError("source feature order or timestamp mismatch")
                state = str(row.get("state"))
                if state not in STATE_CODE_BY_NAME:
                    raise ValueError("source contains an unknown state")
                state_counts[name][state] += 1
            key = timestamp.strftime("%Y-%m")
            if buffer is None:
                buffer = _MonthBuffer(key)
            elif buffer.key != key:
                partitions.append(_write_month(output_root, buffer, contract_digest))
                buffer = _MonthBuffer(key)
            buffer.add(timestamp, rows)
            expected_timestamp += timedelta(minutes=1)
            total_rows += 1
    if buffer is not None:
        partitions.append(_write_month(output_root, buffer, contract_digest))
    if source_hash.hexdigest() != plan.source_artifact_sha256:
        raise ValueError("streamed source SHA-256 mismatch")
    if total_rows != plan.timeline_rows or expected_timestamp != plan.timeline_end_exclusive:
        raise ValueError("source timeline contract mismatch")
    expected_state_counts = _source_quality_counts(source_manifest)
    if expected_state_counts is not None and state_counts != expected_state_counts:
        raise ValueError("analytical state counts do not reconcile with source quality evidence")
    analytical_semantics = {
        "contract_digest": contract_digest,
        "partition_semantic_digests": [item["semantic_digest"] for item in partitions],
        "partition_row_counts": [item["row_count"] for item in partitions],
        "state_counts": state_counts,
    }
    manifest: dict[str, object] = {
        **contract,
        "contract_digest": contract_digest,
        "schema": [field.name + ":" + str(field.type) for field in _schema()],
        "partitions": partitions,
        "partition_count": len(partitions),
        "total_rows": total_rows,
        "quality": {
            "timestamps_unique": True,
            "timeline_continuity": "PASS",
            "feature_columns": len(FEATURE_ORDER),
            "state_columns": len(FEATURE_ORDER),
            "available_values_finite": True,
            "unavailable_values_null": True,
            "state_value_consistency": "PASS",
            "state_counts": state_counts,
            "targets_present": False,
        },
        "analytical_semantic_digest": hashlib.sha256(_canonical(analytical_semantics)).hexdigest(),
    }
    manifest["manifest_semantic_digest"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    pending = output_root / (MANIFEST_NAME + ".pending")
    pending.write_bytes(_canonical(manifest, pretty=True))
    os.replace(pending, output_root / MANIFEST_NAME)
    return verify_analytical_view(output_root, plan=plan)


def verify_analytical_view(
    root: Path, *, plan: AnalyticalViewPlanV1 = PRODUCTION_PLAN
) -> dict[str, object]:
    """Verify manifest, contract, partition digests/schema/counts, and forbidden columns."""
    import pyarrow.parquet as pq

    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    claimed = manifest.pop("manifest_semantic_digest", None)
    if claimed != hashlib.sha256(_canonical(manifest)).hexdigest():
        raise ValueError("analytical manifest digest mismatch")
    contract = plan.payload()
    if any(manifest.get(key) != value for key, value in contract.items()):
        raise ValueError("analytical conversion contract mismatch")
    total = 0
    expected_columns = ["timestamp_utc"]
    for name in FEATURE_ORDER:
        expected_columns.extend((name, name + STATE_SUFFIX))
    for item in manifest.get("partitions", []):
        artifact = root / item["relative_path"]
        if _sha_file(artifact) != item["parquet_sha256"]:
            raise ValueError("analytical partition digest mismatch")
        parquet = pq.ParquetFile(artifact)
        if parquet.schema_arrow.names != expected_columns:
            raise ValueError("analytical partition schema mismatch")
        if parquet.metadata.num_rows != item["row_count"]:
            raise ValueError("analytical partition row count mismatch")
        if parquet.metadata.num_row_groups != item["row_groups"]:
            raise ValueError("analytical partition row-group mismatch")
        if _parquet_semantic_digest(artifact) != item["semantic_digest"]:
            raise ValueError("analytical partition semantic digest mismatch")
        total += parquet.metadata.num_rows
    if total != manifest.get("total_rows") or total != plan.timeline_rows:
        raise ValueError("analytical total row count mismatch")
    lowered = tuple(name.lower() for name in expected_columns)
    if any(token in name for token in TARGET_COLUMN_TOKENS for name in lowered):
        raise ValueError("target-like analytical column forbidden")
    return {**manifest, "manifest_semantic_digest": claimed}
