"""Research-only causal BTC target materialization for the frozen Tsunami experiment."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from .binance_breadth_authority_v2 import (
    BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
    BINANCE_BREADTH_V2_SCHEMA,
)
from .free_v2_analytical_view import (
    ANALYTICAL_ROOT,
    ANALYTICAL_VIEW_IDENTITY,
    verify_analytical_view,
)
from .free_v2_formulas import natural_log_return
from .free_v2_materializer import BINANCE_V2_AUTHORITY_DIGEST
from .tsunami_forecast_contract import (
    DIRECTION_SIGMA,
    EXPERIMENT_IDENTITY,
    MOVE_SIGMA,
    PRIMARY_HORIZON,
    PRIMARY_HORIZONS,
    RESEARCH_END_EXCLUSIVE,
    RESEARCH_START,
    STRESS_SIGMA,
    TARGET_PRICE_SEMANTICS,
    TSUNAMI_SIGMA,
    VOLATILITY_ESTIMATOR,
    VOLATILITY_PRICE_COUNT,
    VOLATILITY_RETURN_COUNT,
    Direction,
    ExperimentSplit,
    Severity,
    TsunamiCandidateV1,
    TsunamiDirection,
    TsunamiEpisodeV1,
    classify_standardized_return,
    cluster_tsunami_episodes,
    last_target_eligible_timestamp,
)

RESEARCH_TARGET_IDENTITY = "CHAPPIE_CAPITAL_FLOW_TSUNAMI_RESEARCH_TARGETS/1.0.0"
ANALYTICAL_SEMANTIC_DIGEST = "46b7762bbee33d6831ab77d6525fc1f539f0db5505e9af13d1c3fb5fda8b7243"
ANALYTICAL_MANIFEST_SEMANTIC_DIGEST = (
    "c60ab829c5ad31c80b74b34c672f800353edb61d27c4a8eb9dd098acd3c88a21"
)
RESEARCH_TARGET_ROOT = Path(
    "E:/projects/chappie/data/research/targets/capital_flow/tsunami_v1/research"
)
BINANCE_V2_ROOT = Path("data/research/source/capital_flow/binance_breadth_free_v2")
BTC_CANONICAL_RELATIVE_PATH = Path("canonical/BTCUSDT.csv")
TARGET_CONVERSION_IDENTITY = "CAUSAL_BTC_RESEARCH_TARGET_PARQUET/1.0.0"
EPISODE_CONTRACT_IDENTITY = "REALIZED_TSUNAMI_EPISODES/1.0.0"
HORIZON_ORDER = PRIMARY_HORIZONS
ROLLING_DIRECT_ABSOLUTE_TOLERANCE = Decimal("1e-40")
TARGET_PARQUET_COMPRESSION = "zstd"
TARGET_ROW_GROUP_ROWS = 10_080
TARGET_PARTITIONING = "horizon=HHH/year=YYYY/month=MM"

ANALYTICAL_VIEW_STATUS = "VALIDATED"
TSUNAMI_CONTRACT_STATUS = "RELEASED"
RESEARCH_TARGET_MATERIALIZER_STATUS = "IMPLEMENTED"
REAL_RESEARCH_TARGETS_STATUS = "NOT_MATERIALIZED"
RESEARCH_MODELS_STATUS = "NOT_RUN"
VALIDATION_STATUS = "NOT_ACCESSED"
PAPER_TRADING_STATUS = "NOT_RUN"


class TargetState(StrEnum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_VOLATILITY_HISTORY = "INSUFFICIENT_VOLATILITY_HISTORY"
    MISSING_BTC_PRICE = "MISSING_BTC_PRICE"
    INVALID_BTC_PRICE = "INVALID_BTC_PRICE"
    ZERO_VOLATILITY = "ZERO_VOLATILITY"
    TARGET_OUTSIDE_RESEARCH_BOUNDARY = "TARGET_OUTSIDE_RESEARCH_BOUNDARY"


TARGET_STATE_CODES = {state: index for index, state in enumerate(TargetState)}


@dataclass(frozen=True, slots=True)
class BtcMinuteCandleV1:
    open_time_utc: datetime
    open: Decimal
    close: Decimal

    @property
    def close_endpoint_utc(self) -> datetime:
        return self.open_time_utc + timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class ResearchTargetRowV1:
    timestamp_utc: datetime
    horizon: timedelta
    entry_timestamp: datetime | None
    exit_timestamp: datetime | None
    entry_open: Decimal | None
    exit_close: Decimal | None
    future_log_return: Decimal | None
    sigma_h: Decimal | None
    z_score: Decimal | None
    direction_class: Direction | None
    severity_class: Severity | None
    move_flag: bool | None
    stress_flag: bool | None
    tsunami_flag: bool | None
    up_tsunami_flag: bool | None
    down_tsunami_flag: bool | None
    any_tsunami: bool | None
    target_state: TargetState

    def semantic_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["timestamp_utc"] = self.timestamp_utc.isoformat()
        payload["horizon"] = horizon_name(self.horizon)
        for key in ("entry_timestamp", "exit_timestamp"):
            value = payload[key]
            payload[key] = None if value is None else value.isoformat()
        for key in ("entry_open", "exit_close", "future_log_return", "sigma_h", "z_score"):
            value = payload[key]
            payload[key] = None if value is None else str(value)
        return payload


def horizon_name(horizon: timedelta) -> str:
    if horizon not in HORIZON_ORDER:
        raise ValueError("target horizon is not frozen")
    return f"{int(horizon / timedelta(hours=1))}h"


def _utc_minute(value: datetime) -> datetime:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.second
        or value.microsecond
    ):
        raise ValueError("timestamp must be an exact UTC minute")
    return value.astimezone(UTC)


def _finite_positive(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def validate_binance_v2_binding(identity: str, semantic_digest: str) -> None:
    if (identity, semantic_digest) != (
        BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
        BINANCE_V2_AUTHORITY_DIGEST,
    ):
        raise ValueError("Research targets require the exact validated Binance V2 authority")


class RollingCausalVolatilityV1:
    """Exact-window rolling returns with high-precision algebraic sample variance."""

    def __init__(self) -> None:
        self._previous_endpoint: datetime | None = None
        self._previous_close: Decimal | None = None
        self._returns: deque[Decimal] = deque()
        self._sum = Decimal(0)
        self._sum_squares = Decimal(0)

    def add_completed_close(self, endpoint: datetime, close: Decimal) -> None:
        endpoint = _utc_minute(endpoint)
        if not _finite_positive(close):
            self.reset()
            raise ValueError("completed close must be positive and finite")
        if self._previous_endpoint is not None:
            if endpoint != self._previous_endpoint + timedelta(minutes=1):
                self.reset()
            else:
                item = natural_log_return(self._previous_close, close)  # type: ignore[arg-type]
                with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
                    self._returns.append(item)
                    self._sum += item
                    self._sum_squares += item * item
                    if len(self._returns) > VOLATILITY_RETURN_COUNT:
                        removed = self._returns.popleft()
                        self._sum -= removed
                        self._sum_squares -= removed * removed
        self._previous_endpoint = endpoint
        self._previous_close = close

    def reset(self) -> None:
        self._previous_endpoint = None
        self._previous_close = None
        self._returns.clear()
        self._sum = Decimal(0)
        self._sum_squares = Decimal(0)

    @property
    def ready(self) -> bool:
        return len(self._returns) == VOLATILITY_RETURN_COUNT

    def sigma(self, horizon: timedelta) -> Decimal:
        if not self.ready:
            raise ValueError("insufficient volatility history")
        with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
            count = Decimal(VOLATILITY_RETURN_COUNT)
            numerator = self._sum_squares - self._sum * self._sum / count
            variance = numerator / (count - 1)
            if variance <= 0:
                raise ZeroDivisionError("zero volatility")
            minutes = Decimal(int(horizon / timedelta(minutes=1)))
            return +(variance.sqrt() * minutes.sqrt())


def direct_sigma(closes: tuple[Decimal, ...], horizon: timedelta) -> Decimal:
    if len(closes) != VOLATILITY_PRICE_COUNT or any(not _finite_positive(item) for item in closes):
        raise ValueError("direct sigma requires exactly 43,201 valid closes")
    returns = tuple(natural_log_return(left, right) for left, right in pairwise(closes))
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        count = Decimal(len(returns))
        mean = sum(returns, Decimal(0)) / count
        variance = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / (count - 1)
        if variance <= 0:
            raise ZeroDivisionError("zero volatility")
        minutes = Decimal(int(horizon / timedelta(minutes=1)))
        return +(variance.sqrt() * minutes.sqrt())


def _unavailable(
    timestamp: datetime, horizon: timedelta, state: TargetState
) -> ResearchTargetRowV1:
    return ResearchTargetRowV1(
        timestamp,
        horizon,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        state,
    )


def calculate_research_target(
    timestamp: datetime,
    horizon: timedelta,
    candles: dict[datetime, BtcMinuteCandleV1],
    rolling: RollingCausalVolatilityV1,
) -> ResearchTargetRowV1:
    """Calculate one eligible target without allowing a Validation outcome read."""
    timestamp = _utc_minute(timestamp)
    if timestamp < RESEARCH_START or timestamp > last_target_eligible_timestamp(
        ExperimentSplit.RESEARCH, horizon
    ):
        return _unavailable(timestamp, horizon, TargetState.TARGET_OUTSIDE_RESEARCH_BOUNDARY)
    if not rolling.ready:
        return _unavailable(timestamp, horizon, TargetState.INSUFFICIENT_VOLATILITY_HISTORY)
    entry_timestamp = timestamp + timedelta(minutes=1)
    exit_timestamp = timestamp + horizon
    if exit_timestamp > RESEARCH_END_EXCLUSIVE:
        raise RuntimeError("Research target attempted to read Validation price")
    entry = candles.get(entry_timestamp)
    exit_candle = candles.get(exit_timestamp - timedelta(minutes=1))
    if entry is None or exit_candle is None:
        return _unavailable(timestamp, horizon, TargetState.MISSING_BTC_PRICE)
    if not _finite_positive(entry.open) or not _finite_positive(exit_candle.close):
        return _unavailable(timestamp, horizon, TargetState.INVALID_BTC_PRICE)
    try:
        sigma_h = rolling.sigma(horizon)
    except ZeroDivisionError:
        return _unavailable(timestamp, horizon, TargetState.ZERO_VOLATILITY)
    future_return = natural_log_return(entry.open, exit_candle.close)
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        z_score = +(future_return / sigma_h)
    label = classify_standardized_return(z_score)
    return ResearchTargetRowV1(
        timestamp,
        horizon,
        entry_timestamp,
        exit_timestamp,
        entry.open,
        exit_candle.close,
        future_return,
        sigma_h,
        z_score,
        label.direction,
        label.severity,
        label.severity in (Severity.MOVE, Severity.STRESS, Severity.TSUNAMI),
        label.severity in (Severity.STRESS, Severity.TSUNAMI),
        label.severity is Severity.TSUNAMI,
        label.tsunami_direction is TsunamiDirection.UP_TSUNAMI,
        label.tsunami_direction is TsunamiDirection.DOWN_TSUNAMI,
        label.severity is Severity.TSUNAMI,
        TargetState.AVAILABLE,
    )


def realized_tsunami_episodes(
    rows: tuple[ResearchTargetRowV1, ...], horizon: timedelta
) -> tuple[TsunamiEpisodeV1, ...]:
    candidates = tuple(
        TsunamiCandidateV1(row.timestamp_utc, row.exit_timestamp, row.z_score)
        for row in rows
        if row.horizon == horizon
        and row.target_state is TargetState.AVAILABLE
        and row.tsunami_flag
        and row.exit_timestamp is not None
        and row.z_score is not None
    )
    return cluster_tsunami_episodes(candidates, horizon)


def target_contract_payload() -> dict[str, object]:
    return {
        "target_identity": RESEARCH_TARGET_IDENTITY,
        "conversion_identity": TARGET_CONVERSION_IDENTITY,
        "experiment_identity": EXPERIMENT_IDENTITY,
        "analytical_view_identity": ANALYTICAL_VIEW_IDENTITY,
        "analytical_semantic_digest": ANALYTICAL_SEMANTIC_DIGEST,
        "analytical_manifest_semantic_digest": ANALYTICAL_MANIFEST_SEMANTIC_DIGEST,
        "binance_authority_identity": BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
        "binance_authority_semantic_digest": BINANCE_V2_AUTHORITY_DIGEST,
        "research_start": RESEARCH_START.isoformat(),
        "research_end_exclusive": RESEARCH_END_EXCLUSIVE.isoformat(),
        "horizon_order": [horizon_name(item) for item in HORIZON_ORDER],
        "primary_horizon": horizon_name(PRIMARY_HORIZON),
        "target_price_semantics": TARGET_PRICE_SEMANTICS,
        "volatility_semantics": VOLATILITY_ESTIMATOR,
        "rolling_direct_absolute_tolerance": str(ROLLING_DIRECT_ABSOLUTE_TOLERANCE),
        "thresholds": {
            "direction_sigma": str(DIRECTION_SIGMA),
            "move_sigma": str(MOVE_SIGMA),
            "stress_sigma": str(STRESS_SIGMA),
            "tsunami_sigma": str(TSUNAMI_SIGMA),
        },
        "target_states": [item.value for item in TargetState],
        "boundary_ineligible_policy": "EXPLICIT_STATE_ROW_WITH_NO_PRICE_ACCESS",
        "numeric_representation": "FINITE_IEEE_754_FLOAT64_OR_NULL_FROM_DETERMINISTIC_DECIMAL",
        "partitioning": TARGET_PARTITIONING,
        "episode_contract_identity": EPISODE_CONTRACT_IDENTITY,
    }


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def target_semantic_digest(rows: tuple[ResearchTargetRowV1, ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical(row.semantic_payload()))
    return digest.hexdigest()


def _binance_manifest_binding(root: Path) -> dict[str, object]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    claimed_manifest = manifest.pop("manifest_semantic_digest", None)
    compact = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if claimed_manifest != hashlib.sha256(compact(manifest)).hexdigest():
        raise ValueError("Binance V2 manifest semantic digest mismatch")
    claimed_authority = manifest.pop("semantic_authority_digest", None)
    status = manifest.pop("status", None)
    if (
        status != "VALIDATED"
        or claimed_authority != BINANCE_V2_AUTHORITY_DIGEST
        or claimed_authority != hashlib.sha256(compact(manifest)).hexdigest()
        or manifest.get("authority_identity") != BINANCE_BREADTH_AUTHORITY_V2_IDENTITY
    ):
        raise ValueError("Binance V2 authority binding mismatch")
    details = manifest.get("symbols_detail")
    if not isinstance(details, list) or not any(
        item.get("symbol") == "BTCUSDT" for item in details
    ):
        raise ValueError("Binance V2 BTCUSDT binding missing")
    return manifest


def load_research_btcusdt_candles(
    root: Path = BINANCE_V2_ROOT,
) -> dict[datetime, BtcMinuteCandleV1]:
    """Load BTCUSDT Research candles only; never scan another symbol or Validation candle."""
    _binance_manifest_binding(root)
    expected_rows = int((RESEARCH_END_EXCLUSIVE - RESEARCH_START) / timedelta(minutes=1))
    result: dict[datetime, BtcMinuteCandleV1] = {}
    path = root / BTC_CANONICAL_RELATIVE_PATH
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        if tuple(next(reader, ())) != BINANCE_BREADTH_V2_SCHEMA:
            raise ValueError("BTCUSDT canonical schema mismatch")
        for index in range(expected_rows):
            row = next(reader, None)
            if row is None or len(row) != len(BINANCE_BREADTH_V2_SCHEMA):
                raise ValueError("BTCUSDT Research prefix incomplete")
            expected = RESEARCH_START + timedelta(minutes=index)
            timestamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
            if timestamp != expected:
                raise ValueError("BTCUSDT Research timestamp continuity mismatch")
            opened, closed = Decimal(row[1]), Decimal(row[4])
            if not _finite_positive(opened) or not _finite_positive(closed):
                raise ValueError("BTCUSDT Research authority contains invalid price")
            result[timestamp] = BtcMinuteCandleV1(timestamp, opened, closed)
    return result


def validate_analytical_source(root: Path = ANALYTICAL_ROOT) -> None:
    manifest = verify_analytical_view(root)
    if (
        manifest.get("analytical_view_identity") != ANALYTICAL_VIEW_IDENTITY
        or manifest.get("analytical_semantic_digest") != ANALYTICAL_SEMANTIC_DIGEST
        or manifest.get("manifest_semantic_digest") != ANALYTICAL_MANIFEST_SEMANTIC_DIGEST
    ):
        raise ValueError("Research targets require the exact validated analytical view")


def iter_analytical_research_timestamps(
    root: Path = ANALYTICAL_ROOT,
):  # type: ignore[no-untyped-def]
    """Yield exact timestamps from Research partitions only, with no Validation partition read."""
    import pyarrow.parquet as pq

    expected = RESEARCH_START
    current = RESEARCH_START
    while current < RESEARCH_END_EXCLUSIVE:
        month = current.strftime("%Y-%m")
        year, number = month.split("-")
        artifact = root / "partitions" / f"year={year}" / f"month={number}" / "data.parquet"
        table = pq.read_table(artifact, columns=["timestamp_utc"])
        for timestamp in table.column(0).to_pylist():
            timestamp = timestamp.astimezone(UTC)
            if timestamp != expected or timestamp >= RESEARCH_END_EXCLUSIVE:
                raise ValueError("analytical Research timestamp continuity mismatch")
            yield timestamp
            expected += timedelta(minutes=1)
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    if expected != RESEARCH_END_EXCLUSIVE:
        raise ValueError("analytical Research timeline incomplete")


def iter_research_targets_for_horizon(
    timestamps: Iterable[datetime],
    candles: dict[datetime, BtcMinuteCandleV1],
    horizon: timedelta,
):  # type: ignore[no-untyped-def]
    """Stream one horizon in timestamp order using one exact rolling volatility state."""
    rolling = RollingCausalVolatilityV1()
    previous: datetime | None = None
    for timestamp in timestamps:
        timestamp = _utc_minute(timestamp)
        if previous is not None and timestamp != previous + timedelta(minutes=1):
            raise ValueError("Research timestamps must be contiguous")
        close_candle = candles.get(timestamp - timedelta(minutes=1))
        if close_candle is not None:
            try:
                rolling.add_completed_close(timestamp, close_candle.close)
            except ValueError:
                pass
        else:
            rolling.reset()
        yield calculate_research_target(timestamp, horizon, candles, rolling)
        previous = timestamp


def research_target_selection_sql(
    root: Path,
    horizon: timedelta,
    start: datetime = RESEARCH_START,
    end_exclusive: datetime = RESEARCH_END_EXCLUSIVE,
    *,
    direction: Direction | None = None,
    severity: Severity | None = None,
) -> str:
    start, end_exclusive = _utc_minute(start), _utc_minute(end_exclusive)
    if not RESEARCH_START <= start < end_exclusive <= RESEARCH_END_EXCLUSIVE:
        raise ValueError("target selection must remain wholly inside Research")
    clauses = [
        f"timestamp_utc >= TIMESTAMPTZ '{start.isoformat()}'",
        f"timestamp_utc < TIMESTAMPTZ '{end_exclusive.isoformat()}'",
    ]
    if direction is not None:
        clauses.append(f"direction_class = '{direction.value}'")
    if severity is not None:
        clauses.append(f"severity_class = '{severity.value}'")
    glob = root / f"horizon={horizon_name(horizon)}" / "year=*" / "month=*" / "data.parquet"
    return (
        f"SELECT * FROM read_parquet('{glob.as_posix()}', hive_partitioning=false) WHERE "
        + " AND ".join(clauses)
        + " ORDER BY timestamp_utc"
    )


def feature_target_join_sql(
    analytical_root: Path, target_root: Path, horizon: timedelta, start: datetime, end: datetime
) -> str:
    target_sql = research_target_selection_sql(target_root, horizon, start, end)
    feature_glob = (
        analytical_root / "partitions" / "year=*" / "month=*" / "data.parquet"
    ).as_posix()
    return (
        f"SELECT f.*, t.* EXCLUDE (timestamp_utc) FROM read_parquet('{feature_glob}', "
        f"hive_partitioning=false) f JOIN ({target_sql}) t USING (timestamp_utc) "
        "ORDER BY timestamp_utc"
    )


def _target_schema():  # type: ignore[no-untyped-def]
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("timestamp_utc", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("horizon", pa.string(), nullable=False),
            pa.field("entry_timestamp", pa.timestamp("us", tz="UTC")),
            pa.field("exit_timestamp", pa.timestamp("us", tz="UTC")),
            pa.field("entry_open", pa.float64()),
            pa.field("exit_close", pa.float64()),
            pa.field("future_log_return", pa.float64()),
            pa.field("sigma_h", pa.float64()),
            pa.field("z_score", pa.float64()),
            pa.field("direction_class", pa.string()),
            pa.field("severity_class", pa.string()),
            pa.field("move_flag", pa.bool_()),
            pa.field("stress_flag", pa.bool_()),
            pa.field("tsunami_flag", pa.bool_()),
            pa.field("up_tsunami_flag", pa.bool_()),
            pa.field("down_tsunami_flag", pa.bool_()),
            pa.field("any_tsunami", pa.bool_()),
            pa.field("target_state", pa.string(), nullable=False),
        ],
        metadata={b"target_identity": RESEARCH_TARGET_IDENTITY.encode()},
    )


def _arrow_payload(rows: list[ResearchTargetRowV1]) -> dict[str, list[object]]:
    result: dict[str, list[object]] = {field.name: [] for field in _target_schema()}
    decimal_fields = ("entry_open", "exit_close", "future_log_return", "sigma_h", "z_score")
    for row in rows:
        result["timestamp_utc"].append(row.timestamp_utc)
        result["horizon"].append(horizon_name(row.horizon))
        result["entry_timestamp"].append(row.entry_timestamp)
        result["exit_timestamp"].append(row.exit_timestamp)
        for field in decimal_fields:
            value = getattr(row, field)
            numeric = None if value is None else float(value)
            if numeric is not None and not math.isfinite(numeric):
                raise ValueError("target numeric conversion must remain finite")
            result[field].append(numeric)
        result["direction_class"].append(
            None if row.direction_class is None else row.direction_class.value
        )
        result["severity_class"].append(
            None if row.severity_class is None else row.severity_class.value
        )
        for field in (
            "move_flag",
            "stress_flag",
            "tsunami_flag",
            "up_tsunami_flag",
            "down_tsunami_flag",
            "any_tsunami",
        ):
            result[field].append(getattr(row, field))
        result["target_state"].append(row.target_state.value)
    return result


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _partition_key(row: ResearchTargetRowV1) -> tuple[int, str]:
    return HORIZON_ORDER.index(row.horizon), row.timestamp_utc.strftime("%Y-%m")


def _verify_partition_file(path: Path, row_count: int, row_groups: int) -> None:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    if parquet.schema_arrow != _target_schema():
        raise ValueError("target partition schema mismatch")
    if parquet.metadata.num_rows != row_count:
        raise ValueError("target partition row count mismatch")
    if parquet.metadata.num_row_groups != row_groups:
        raise ValueError("target partition row-group mismatch")


def publish_research_targets(
    rows: Iterable[ResearchTargetRowV1],
    root: Path = RESEARCH_TARGET_ROOT,
    *,
    phase_hook: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Publish deterministic horizon/month Parquet partitions with the manifest last."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if root.exists():
        raise FileExistsError("Research target root is create-only")
    root.mkdir(parents=True)
    notify = phase_hook or (lambda _: None)
    current_key: tuple[int, str] | None = None
    buffer: list[ResearchTargetRowV1] = []
    partitions: list[dict[str, object]] = []
    state_counts = {state.value: 0 for state in TargetState}
    all_semantics = hashlib.sha256()

    def flush() -> None:
        nonlocal buffer
        if not buffer or current_key is None:
            return
        horizon = buffer[0].horizon
        month = current_key[1]
        year, month_number = month.split("-")
        directory = (
            root / f"horizon={horizon_name(horizon)}" / f"year={year}" / f"month={month_number}"
        )
        directory.mkdir(parents=True)
        artifact = directory / "data.parquet"
        table = pa.Table.from_pydict(_arrow_payload(buffer), schema=_target_schema())
        pq.write_table(
            table,
            artifact,
            compression=TARGET_PARQUET_COMPRESSION,
            compression_level=9,
            row_group_size=TARGET_ROW_GROUP_ROWS,
            use_dictionary=False,
            version="2.6",
        )
        semantic = target_semantic_digest(tuple(buffer))
        row_groups = math.ceil(len(buffer) / TARGET_ROW_GROUP_ROWS)
        _verify_partition_file(artifact, len(buffer), row_groups)
        partitions.append(
            {
                "horizon": horizon_name(horizon),
                "month": month,
                "relative_path": artifact.relative_to(root).as_posix(),
                "row_count": len(buffer),
                "row_groups": row_groups,
                "byte_size": artifact.stat().st_size,
                "physical_sha256": _sha_file(artifact),
                "semantic_digest": semantic,
            }
        )
        buffer = []

    previous_key: tuple[int, str] | None = None
    previous_timestamp: datetime | None = None
    for row in rows:
        key = _partition_key(row)
        if previous_key is not None and key < previous_key:
            raise ValueError("target rows must be ordered by frozen horizon then month")
        if key != current_key:
            flush()
            current_key = key
            previous_timestamp = None
        if previous_timestamp is not None and row.timestamp_utc <= previous_timestamp:
            raise ValueError("target timestamps must be strictly increasing per partition")
        previous_key, previous_timestamp = key, row.timestamp_utc
        buffer.append(row)
        state_counts[row.target_state.value] += 1
        all_semantics.update(_canonical(row.semantic_payload()))
    flush()
    if not partitions:
        raise ValueError("Research target publication requires rows")
    notify("partitions_verified")
    manifest: dict[str, object] = {
        **target_contract_payload(),
        "schema": [field.name + ":" + str(field.type) for field in _target_schema()],
        "partitions": partitions,
        "partition_count": len(partitions),
        "total_rows": sum(item["row_count"] for item in partitions),
        "state_counts": state_counts,
        "target_semantic_digest": all_semantics.hexdigest(),
        "quality": {
            "research_only": True,
            "validation_rows": 0,
            "blind_oos_rows": 0,
            "target_columns_only": True,
        },
    }
    manifest["manifest_semantic_digest"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    pending = root / "manifest.json.pending"
    pending.write_bytes(json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n")
    os.replace(pending, root / "manifest.json")
    notify("manifest_published")
    return verify_research_targets(root)


def verify_research_targets(root: Path = RESEARCH_TARGET_ROOT) -> dict[str, object]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    claimed = manifest.pop("manifest_semantic_digest", None)
    if claimed != hashlib.sha256(_canonical(manifest)).hexdigest():
        raise ValueError("target manifest digest mismatch")
    contract = target_contract_payload()
    if any(manifest.get(key) != value for key, value in contract.items()):
        raise ValueError("target contract binding mismatch")
    total = 0
    for item in manifest["partitions"]:
        artifact = root / item["relative_path"]
        if _sha_file(artifact) != item["physical_sha256"]:
            raise ValueError("target partition digest mismatch")
        _verify_partition_file(artifact, item["row_count"], item["row_groups"])
        total += item["row_count"]
    if total != manifest["total_rows"]:
        raise ValueError("target total row count mismatch")
    return {**manifest, "manifest_semantic_digest": claimed}
