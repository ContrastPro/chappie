"""Create-only Validation authority for frozen V2 one-hour direction targets."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from .binance_breadth_authority_v2 import (
    BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
    BINANCE_BREADTH_V2_SCHEMA,
)
from .free_v2_materializer import BINANCE_V2_AUTHORITY_DIGEST
from .short_horizon_direction_v2_contract import (
    EXPERIMENT_IDENTITY,
    VALIDATION_END_EXCLUSIVE,
    VALIDATION_START,
)
from .tsunami_forecast_contract import (
    DIRECTION_SIGMA,
    Direction,
    Severity,
    classify_standardized_return,
)
from .tsunami_research_targets import (
    BINANCE_V2_ROOT,
    BTC_CANONICAL_RELATIVE_PATH,
    TARGET_PARQUET_COMPRESSION,
    TARGET_ROW_GROUP_ROWS,
    BtcMinuteCandleV1,
    ResearchTargetRowV1,
    RollingCausalVolatilityV1,
    TargetState,
    _arrow_payload,
    _binance_manifest_binding,
    _finite_positive,
    _target_schema,
    _utc_minute,
    _verify_partition_file,
    horizon_name,
    target_semantic_digest,
)

VALIDATION_TARGET_IDENTITY = "CHAPPIE_SHORT_HORIZON_DIRECTION_V2_VALIDATION_TARGETS/1.0.0"
VALIDATION_TARGET_ROOT = Path("E:/projects/chappie/data/research/targets/market_brain/short_horizon_direction_v2/validation")
VALIDATION_TARGET_PUBLICATION_VERSION = "validation-target-authority-v1"
VALIDATION_HORIZON = timedelta(hours=1)
# The row at end-1h exits exactly at the boundary and uses the 23:59 close,
# so only the following 59 forecast timestamps would require OOS observation.
TERMINAL_ROWS_EXCLUDED = int(VALIDATION_HORIZON / timedelta(minutes=1)) - 1


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def validation_target_contract() -> dict[str, object]:
    return {
        "identity": VALIDATION_TARGET_IDENTITY,
        "phase": "VALIDATION",
        "experiment_identity": EXPERIMENT_IDENTITY,
        "start": VALIDATION_START.isoformat(),
        "end_exclusive": VALIDATION_END_EXCLUSIVE.isoformat(),
        "horizon": horizon_name(VALIDATION_HORIZON),
        "target_price_semantics": "entry=next minute open; exit=last minute close before timestamp+horizon",
        "volatility_semantics": "exact causal 43,200 completed 1m log-return sample stdev times sqrt(horizon minutes)",
        "direction_sigma": str(DIRECTION_SIGMA),
        "class_definitions": [item.value for item in Direction],
        "source_authority_identity": BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
        "source_authority_digest": BINANCE_V2_AUTHORITY_DIGEST,
        "publication_version": VALIDATION_TARGET_PUBLICATION_VERSION,
        "terminal_rows_excluded": TERMINAL_ROWS_EXCLUDED,
    }


def validation_target_semantic_identity() -> str:
    return hashlib.sha256(_canonical(validation_target_contract())).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationTargetAuthorityV2:
    row_count: int
    first_timestamp: str
    last_timestamp: str
    class_support: dict[str, int]

    def manifest(self, partitions: list[dict[str, object]], semantic_digest: str) -> dict[str, object]:
        payload = {
            **validation_target_contract(),
            "target_semantic_identity": validation_target_semantic_identity(),
            "schema": [field.name + ":" + str(field.type) for field in _target_schema()],
            "row_count": self.row_count,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "class_support": self.class_support,
            "partitions": partitions,
            "target_semantic_digest": semantic_digest,
        }
        payload["manifest_semantic_digest"] = hashlib.sha256(_canonical(payload)).hexdigest()
        return payload


def _load_validation_candles(root: Path = BINANCE_V2_ROOT) -> dict[datetime, BtcMinuteCandleV1]:
    _binance_manifest_binding(root)
    result: dict[datetime, BtcMinuteCandleV1] = {}
    with (root / BTC_CANONICAL_RELATIVE_PATH).open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        if tuple(next(reader, ())) != BINANCE_BREADTH_V2_SCHEMA:
            raise ValueError("BTC Validation target source schema mismatch")
        for row in reader:
            timestamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
            if timestamp >= VALIDATION_END_EXCLUSIVE:
                break
            # 43,201 prices produce the frozen 43,200-return volatility window.
            if timestamp >= VALIDATION_START - timedelta(minutes=43_201):
                opened, closed = Decimal(row[1]), Decimal(row[4])
                if not _finite_positive(opened) or not _finite_positive(closed):
                    raise ValueError("BTC Validation target source contains invalid price")
                result[timestamp] = BtcMinuteCandleV1(timestamp, opened, closed)
    expected_start = VALIDATION_START - timedelta(minutes=43_201)
    expected = int((VALIDATION_END_EXCLUSIVE - expected_start) / timedelta(minutes=1))
    if len(result) != expected or set(result) != {expected_start + timedelta(minutes=i) for i in range(expected)}:
        raise ValueError("BTC Validation target source continuity mismatch")
    return result


def _calculate_validation_target(timestamp: datetime, candles: dict[datetime, BtcMinuteCandleV1], rolling: RollingCausalVolatilityV1) -> ResearchTargetRowV1:
    timestamp = _utc_minute(timestamp)
    if timestamp < VALIDATION_START or timestamp + VALIDATION_HORIZON > VALIDATION_END_EXCLUSIVE:
        raise ValueError("Validation target future exit crosses sealed boundary")
    if not rolling.ready:
        raise ValueError("Validation target rolling volatility warm-up incomplete")
    entry, exit_candle = candles[timestamp + timedelta(minutes=1)], candles[timestamp + timedelta(minutes=59)]
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        sigma = rolling.sigma(VALIDATION_HORIZON)
        future = (exit_candle.close / entry.open).ln()
        z_score = +(future / sigma)
    label = classify_standardized_return(z_score)
    return ResearchTargetRowV1(timestamp, VALIDATION_HORIZON, timestamp + timedelta(minutes=1), timestamp + VALIDATION_HORIZON, entry.open, exit_candle.close, future, sigma, z_score, label.direction, label.severity, label.severity in (Severity.MOVE, Severity.STRESS, Severity.TSUNAMI), label.severity in (Severity.STRESS, Severity.TSUNAMI), label.severity is Severity.TSUNAMI, label.tsunami_direction.value == "UP_TSUNAMI" if label.tsunami_direction else False, label.tsunami_direction.value == "DOWN_TSUNAMI" if label.tsunami_direction else False, label.severity is Severity.TSUNAMI, TargetState.AVAILABLE)


def build_validation_targets(source_root: Path = BINANCE_V2_ROOT) -> tuple[ResearchTargetRowV1, ...]:
    candles = _load_validation_candles(source_root)
    rolling = RollingCausalVolatilityV1()
    rows = []
    first_close = VALIDATION_START - timedelta(minutes=43_201)
    last = VALIDATION_END_EXCLUSIVE - VALIDATION_HORIZON
    for timestamp in (first_close + timedelta(minutes=i) for i in range(int((VALIDATION_END_EXCLUSIVE - first_close) / timedelta(minutes=1)))):
        rolling.add_completed_close(timestamp + timedelta(minutes=1), candles[timestamp].close)
        candidate = timestamp + timedelta(minutes=1)
        if VALIDATION_START <= candidate <= last:
            rows.append(_calculate_validation_target(candidate, candles, rolling))
    if len(rows) != int((last - VALIDATION_START) / timedelta(minutes=1)) + 1:
        raise ValueError("Validation target row containment reconciliation failed")
    return tuple(rows)


def _partition(rows: tuple[ResearchTargetRowV1, ...], root: Path) -> list[dict[str, object]]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    result = []
    for month in sorted({row.timestamp_utc.strftime("%Y-%m") for row in rows}):
        batch = [row for row in rows if row.timestamp_utc.strftime("%Y-%m") == month]
        year, number = month.split("-")
        artifact = root / "horizon=1h" / f"year={year}" / f"month={number}" / "data.parquet"
        artifact.parent.mkdir(parents=True)
        temporary = artifact.with_suffix(".parquet.partial")
        pq.write_table(pa.Table.from_pydict(_arrow_payload(batch), schema=_target_schema()), temporary, compression=TARGET_PARQUET_COMPRESSION, compression_level=9, row_group_size=TARGET_ROW_GROUP_ROWS, use_dictionary=False, version="2.6")
        os.replace(temporary, artifact)
        groups = math.ceil(len(batch) / TARGET_ROW_GROUP_ROWS)
        _verify_partition_file(artifact, len(batch), groups)
        result.append({"month": month, "relative_path": artifact.relative_to(root).as_posix(), "row_count": len(batch), "row_groups": groups, "physical_sha256": _sha(artifact), "semantic_digest": target_semantic_digest(tuple(batch))})
    return result


def verify_validation_targets(root: Path = VALIDATION_TARGET_ROOT) -> dict[str, object]:
    manifest = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
    claimed = manifest.pop("manifest_semantic_digest", None)
    if claimed != hashlib.sha256(_canonical(manifest)).hexdigest() or any(manifest.get(k) != v for k, v in validation_target_contract().items()):
        raise ValueError("Validation target manifest binding mismatch")
    if manifest.get("target_semantic_identity") != validation_target_semantic_identity():
        raise ValueError("Validation target semantic identity mismatch")
    total = 0
    support = {item.value: 0 for item in Direction}
    for item in manifest["partitions"]:
        path = Path(root) / item["relative_path"]
        if _sha(path) != item["physical_sha256"]:
            raise ValueError("Validation target partition digest mismatch")
        _verify_partition_file(path, item["row_count"], item["row_groups"])
        import pyarrow.parquet as pq
        table = pq.ParquetFile(path).read().to_pandas()
        timestamps = table["timestamp_utc"]
        if timestamps.duplicated().any() or not timestamps.is_monotonic_increasing or (timestamps < VALIDATION_START).any() or (timestamps + VALIDATION_HORIZON > VALIDATION_END_EXCLUSIVE).any():
            raise ValueError("Validation target containment/order violation")
        for label, count in table["direction_class"].value_counts().items(): support[label] += int(count)
        total += len(table)
    if total != manifest["row_count"] or support != manifest["class_support"]:
        raise ValueError("Validation target evidence mismatch")
    return {**manifest, "manifest_semantic_digest": claimed}


def materialize_validation_targets(root: Path = VALIDATION_TARGET_ROOT, source_root: Path = BINANCE_V2_ROOT) -> dict[str, object]:
    root = Path(root)
    if (root / "manifest.json").exists(): return {"reused": True, **verify_validation_targets(root)}
    if root.exists(): raise FileExistsError("Validation target root exists without canonical manifest")
    root.mkdir(parents=True)
    rows = build_validation_targets(source_root)
    partitions = _partition(rows, root)
    support = {item.value: sum(row.direction_class is item for row in rows) for item in Direction}
    manifest = ValidationTargetAuthorityV2(len(rows), rows[0].timestamp_utc.isoformat().replace("+00:00", "Z"), rows[-1].timestamp_utc.isoformat().replace("+00:00", "Z"), support).manifest(partitions, target_semantic_digest(rows))
    pending = root / "manifest.json.pending"; pending.write_bytes(_canonical(manifest)); os.replace(pending, root / "manifest.json")
    return {"reused": False, **verify_validation_targets(root)}
