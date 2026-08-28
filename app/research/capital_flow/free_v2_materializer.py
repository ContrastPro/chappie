"""Deterministic, causal materialization for the active Capital Flow Free V2 catalogue."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from .binance_breadth_authority_v2 import (
    BINANCE_BREADTH_AUTHORITY_V2_IDENTITY,
    BINANCE_BREADTH_V2_ROOT,
    BINANCE_BREADTH_V2_SCHEMA,
    verify_binance_breadth_v2,
)
from .defillama_stablecoin_publication import (
    DEFILLAMA_PUBLICATION_ROOT,
    verify_defillama_stablecoin_authority,
)
from .fred_alfred_v2 import FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY
from .fred_alfred_v2_availability import count_us_federal_business_days
from .fred_alfred_v2_publication import (
    FRED_ALFRED_V2_PUBLICATION_ROOT,
    verify_fred_alfred_v2_authority,
)
from .free_v2 import FREE_FEATURE_CATALOGUE_V2_IDENTITY
from .free_v2_formulas import (
    CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY,
    FEATURE_TIMELINE_END_EXCLUSIVE,
    FEATURE_TIMELINE_MINUTES,
    FEATURE_TIMELINE_START,
    FEATURE_UNAVAILABLE_STATES,
    INSUFFICIENT_HISTORY,
    INVALID_SOURCE_SESSION,
    MISSING_REQUIRED_SYMBOL,
    SOURCE_STALE,
    SOURCE_UNAVAILABLE,
    WINDOW_INCOMPLETE,
    FeatureFormulaUnavailable,
    btc_quote_volume_share,
    crypto_60m_return,
    crypto_alt_outperform_share,
    crypto_population_dispersion,
    crypto_positive_share,
    five_observation_log_return,
    nasdaq_previous_session_return,
    natural_log_return,
    pearson_population,
    quote_volume_60m,
    stablecoin_log_change,
    treasury_change_bp,
    validate_feature_timestamp,
    vix_point_change,
    vix_zscore_60,
)
from .nasdaq_calendar import nasdaq_regular_session_close

CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY = "chappie_capital_flow_free_v2_feature_dataset/1.0.0"
AVAILABLE = "AVAILABLE"
BINANCE_V2_AUTHORITY_DIGEST = "d2f2a63c86ba438cc8c4bca7b68d13fdf71d66e52b83aa3aa1f6a8729a5f581a"
DEFILLAMA_AUTHORITY_IDENTITY = "chappie_defillama_stablecoin_historical_authority/1.0.0"
DEFILLAMA_AUTHORITY_DIGEST = "b61cdc6bc06a56137994e3045c884f1862ef6135436f9a70332ccf2a40115b7e"
FRED_V2_AUTHORITY_DIGEST = "8a0f32f797395f54b167d9a549b889cfc0cd4e11f28e01a90544bb6be326d6fa"
VALIDATED_AUTHORITY_BINDINGS = (
    (BINANCE_BREADTH_AUTHORITY_V2_IDENTITY, BINANCE_V2_AUTHORITY_DIGEST),
    (DEFILLAMA_AUTHORITY_IDENTITY, DEFILLAMA_AUTHORITY_DIGEST),
    (FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY, FRED_V2_AUTHORITY_DIGEST),
)
FEATURE_ORDER = (
    "breadth.crypto_positive_share_60m",
    "breadth.crypto_dispersion_60m",
    "breadth.crypto_alt_outperform_btc_share_60m",
    "breadth.crypto_btc_quote_volume_share_60m",
    "stablecoin.aggregate_market_cap_change_1d",
    "stablecoin.aggregate_market_cap_change_7d",
    "equity.nasdaq_previous_session_return",
    "risk.vix_change_1d",
    "risk.vix_zscore_60sessions",
    "fx.usdjpy_return_5d",
    "usd.broad_index_return_5d",
    "rates.us2y_change_5d",
    "rates.us10y_change_5d",
    "stress.stlfsi4_level",
    "coupling.btc_nasdaq_correlation_20sessions",
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc(value: datetime) -> datetime:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.second
        or value.microsecond
    ):
        raise ValueError("timestamp must be an exact UTC minute boundary")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SourceAuthorityBindingV1:
    identity: str
    semantic_digest: str

    def __post_init__(self) -> None:
        if not self.identity or len(self.semantic_digest) != 64:
            raise ValueError("invalid source authority binding")


@dataclass(frozen=True, slots=True)
class CausalValueV1:
    reference_identity: str
    reference_date: date
    value: Decimal
    usable_at_utc: datetime
    stale_after_utc: datetime | None = None
    source_session_close_utc: datetime | None = None

    def __post_init__(self) -> None:
        _utc(self.usable_at_utc)
        if self.stale_after_utc is not None:
            _utc(self.stale_after_utc)
        if self.source_session_close_utc is not None:
            _utc(self.source_session_close_utc)
        if not self.reference_identity or not self.value.is_finite():
            raise ValueError("invalid causal value")


@dataclass(frozen=True, slots=True)
class BinanceCandleV1:
    symbol: str
    open_time_utc: datetime
    close: Decimal
    quote_volume: Decimal | None
    source_identity: str

    def __post_init__(self) -> None:
        _utc(self.open_time_utc)
        if self.close <= 0 or not self.close.is_finite():
            raise ValueError("invalid Binance close")
        if self.quote_volume is not None and (
            self.quote_volume < 0 or not self.quote_volume.is_finite()
        ):
            raise ValueError("invalid Binance quote volume")

    @property
    def close_boundary_utc(self) -> datetime:
        return self.open_time_utc + timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class MaterializerInputsV1:
    authorities: tuple[SourceAuthorityBindingV1, ...]
    binance: Mapping[str, Mapping[datetime, BinanceCandleV1]]
    stablecoins: Mapping[str, Sequence[CausalValueV1]]
    fred: Mapping[str, Sequence[CausalValueV1]]

    def __post_init__(self) -> None:
        if tuple((item.identity, item.semantic_digest) for item in self.authorities) != (
            VALIDATED_AUTHORITY_BINDINGS
        ):
            raise ValueError("active Free V2 requires exact validated authority bindings")


@dataclass(frozen=True, slots=True)
class CapitalFlowFeatureValueV2:
    feature_name: str
    family: str
    timestamp_utc: datetime
    value: Decimal | None
    state: str
    unit: str
    formula_identity: str
    source_authority_bindings: tuple[SourceAuthorityBindingV1, ...]
    input_causal_identities: tuple[str, ...]
    window_identity: str
    window_start_utc: datetime | None
    window_end_utc: datetime
    feature_digest: str

    def __post_init__(self) -> None:
        _utc(self.timestamp_utc)
        if self.state == AVAILABLE:
            if self.value is None or not self.value.is_finite():
                raise ValueError("available feature requires finite value")
        elif self.state not in FEATURE_UNAVAILABLE_STATES or self.value is not None:
            raise ValueError("unavailable feature requires a frozen state and no value")
        if self.feature_digest != _digest(self.semantic_payload()):
            raise ValueError("feature digest mismatch")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "feature_name": self.feature_name,
            "formula_identity": self.formula_identity,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "value": None if self.value is None else str(self.value),
            "state": self.state,
            "unit": self.unit,
            "source_authorities": [
                (x.identity, x.semantic_digest) for x in self.source_authority_bindings
            ],
            "input_causal_identities": list(self.input_causal_identities),
            "window_identity": self.window_identity,
            "window_start_utc": None
            if self.window_start_utc is None
            else self.window_start_utc.isoformat(),
            "window_end_utc": self.window_end_utc.isoformat(),
        }


_META = {
    name: (
        name.split(".", 1)[0],
        f"CAPITAL_FLOW_FREE_V2/{name.upper().replace('.', '_')}/1.0.0",
        unit,
    )
    for name, unit in zip(
        FEATURE_ORDER,
        (
            "share_[0,1]",
            "log_return_dispersion",
            "share_[0,1]",
            "share_[0,1]",
            "dimensionless_log_return",
            "dimensionless_log_return",
            "dimensionless_log_return",
            "VIX_index_points",
            "dimensionless_zscore",
            "dimensionless_log_return",
            "dimensionless_log_return",
            "basis_points",
            "basis_points",
            "STLFSI4_index_level",
            "dimensionless_correlation_[-1,1]",
        ),
        strict=True,
    )
}


def _make(
    name: str,
    at: datetime,
    authorities: tuple[SourceAuthorityBindingV1, ...],
    ids: Sequence[str],
    window: str,
    start: datetime | None,
    value: Decimal | None = None,
    state: str = AVAILABLE,
) -> CapitalFlowFeatureValueV2:
    family, formula, unit = _META[name]
    values = {
        "feature_name": name,
        "family": family,
        "timestamp_utc": at,
        "value": value,
        "state": state,
        "unit": unit,
        "formula_identity": formula,
        "source_authority_bindings": authorities,
        "input_causal_identities": tuple(ids),
        "window_identity": window,
        "window_start_utc": start,
        "window_end_utc": at,
    }
    provisional = object.__new__(CapitalFlowFeatureValueV2)
    for key, item in values.items():
        object.__setattr__(provisional, key, item)
    return CapitalFlowFeatureValueV2(
        **values, feature_digest=_digest(provisional.semantic_payload())
    )


def _eligible(values: Sequence[CausalValueV1], at: datetime) -> list[CausalValueV1]:
    return sorted(
        (x for x in values if x.usable_at_utc <= at),
        key=lambda x: (x.reference_date, x.usable_at_utc, x.reference_identity),
    )


def _latest(values: Sequence[CausalValueV1], at: datetime) -> CausalValueV1:
    candidates = _eligible(values, at)
    if not candidates:
        raise FeatureFormulaUnavailable(SOURCE_UNAVAILABLE)
    item = candidates[-1]
    if item.stale_after_utc is not None and at >= item.stale_after_utc:
        raise FeatureFormulaUnavailable(SOURCE_STALE)
    return item


def verify_real_source_authorities(
    *,
    binance_root: Path = BINANCE_BREADTH_V2_ROOT,
    defillama_root: Path = DEFILLAMA_PUBLICATION_ROOT,
    fred_root: Path = FRED_ALFRED_V2_PUBLICATION_ROOT,
) -> tuple[SourceAuthorityBindingV1, ...]:
    """Reload-verify the three exact durable authorities required by active Free V2."""
    binance = verify_binance_breadth_v2(binance_root)
    defillama = verify_defillama_stablecoin_authority(defillama_root)
    fred = verify_fred_alfred_v2_authority(fred_root)
    actual = (
        (BINANCE_BREADTH_AUTHORITY_V2_IDENTITY, binance.semantic_authority_digest),
        (DEFILLAMA_AUTHORITY_IDENTITY, defillama.semantic_authority_digest),
        (FRED_ALFRED_GLOBAL_RISK_AUTHORITY_V2_IDENTITY, fred.semantic_authority_digest),
    )
    if actual != VALIDATED_AUTHORITY_BINDINGS:
        raise ValueError("validated source authority identity/digest mismatch")
    return tuple(SourceAuthorityBindingV1(*item) for item in actual)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("authority timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _fred_stale_after(series_id: str, reference_date: date) -> datetime:
    if series_id == "STLFSI4":
        return datetime.combine(reference_date + timedelta(days=10), time.min, UTC)
    candidate = reference_date
    while (
        candidate < FEATURE_TIMELINE_END_EXCLUSIVE.date()
        and count_us_federal_business_days(reference_date, candidate) <= 3
    ):
        candidate += timedelta(days=1)
    return datetime.combine(candidate, time.min, UTC)


def _load_fred_values(root: Path, timestamp: datetime) -> dict[str, tuple[CausalValueV1, ...]]:
    result: dict[str, tuple[CausalValueV1, ...]] = {}
    for series_id in ("NASDAQCOM", "VIXCLS", "DEXJPUS", "DTWEXBGS", "DGS2", "DGS10", "STLFSI4"):
        path = root / "canonical" / f"canonical_{series_id.lower()}.ndjson"
        values: list[CausalValueV1] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            usable = _parse_utc(item["usable_at_utc"])
            effective = (
                None
                if item["effective_until_utc"] is None
                else _parse_utc(item["effective_until_utc"])
            )
            if (
                usable > timestamp
                or (effective is not None and timestamp >= effective)
                or item["value"] is None
                or item["value_state"] != "OBSERVED_VALUE"
            ):
                continue
            reference = date.fromisoformat(item["reference_date"])
            values.append(
                CausalValueV1(
                    item["selected_vintage_identity"],
                    reference,
                    Decimal(item["value"]),
                    usable,
                    _fred_stale_after(series_id, reference),
                    nasdaq_regular_session_close(reference) if series_id == "NASDAQCOM" else None,
                )
            )
        result[series_id] = tuple(
            sorted(values, key=lambda item: (item.reference_date, item.usable_at_utc))
        )
    return result


def _load_fred_values_range(
    root: Path, start: datetime, end_exclusive: datetime
) -> dict[str, tuple[CausalValueV1, ...]]:
    """Load every PIT value whose effective interval overlaps an execution chunk."""
    result: dict[str, tuple[CausalValueV1, ...]] = {}
    for series_id in ("NASDAQCOM", "VIXCLS", "DEXJPUS", "DTWEXBGS", "DGS2", "DGS10", "STLFSI4"):
        path = root / "canonical" / f"canonical_{series_id.lower()}.ndjson"
        values: list[CausalValueV1] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            usable = _parse_utc(item["usable_at_utc"])
            effective = (
                None
                if item["effective_until_utc"] is None
                else _parse_utc(item["effective_until_utc"])
            )
            if (
                usable >= end_exclusive
                or (effective is not None and effective <= start)
                or item["value"] is None
                or item["value_state"] != "OBSERVED_VALUE"
            ):
                continue
            reference = date.fromisoformat(item["reference_date"])
            values.append(
                CausalValueV1(
                    item["selected_vintage_identity"],
                    reference,
                    Decimal(item["value"]),
                    usable,
                    _fred_stale_after(series_id, reference),
                    nasdaq_regular_session_close(reference) if series_id == "NASDAQCOM" else None,
                )
            )
        result[series_id] = tuple(
            sorted(values, key=lambda item: (item.reference_date, item.usable_at_utc))
        )
    return result


def _load_stablecoin_values(
    root: Path, timestamp: datetime
) -> dict[str, tuple[CausalValueV1, ...]]:
    result: dict[str, tuple[CausalValueV1, ...]] = {}
    for symbol in ("USDT", "USDC", "DAI"):
        values: list[CausalValueV1] = []
        path = root / f"canonical_{symbol.lower()}.ndjson"
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            usable = _parse_utc(item["usable_at_utc"])
            if usable > timestamp:
                continue
            reference_at = _parse_utc(item["reference_timestamp_utc"])
            values.append(
                CausalValueV1(
                    _digest((symbol, item["reference_timestamp_utc"], item["market_cap_usd"])),
                    reference_at.date(),
                    Decimal(item["market_cap_usd"]),
                    usable,
                    reference_at + timedelta(hours=48),
                )
            )
        result[symbol] = tuple(values)
    return result


def _load_binance_values(
    root: Path, timestamp: datetime, fred: Mapping[str, Sequence[CausalValueV1]]
) -> dict[str, dict[datetime, BinanceCandleV1]]:
    required = {timestamp - timedelta(minutes=offset) for offset in range(61, 0, -1)}
    nasdaq = _eligible(fred.get("NASDAQCOM", ()), timestamp)
    if len(nasdaq) >= 21:
        required.update(
            item.source_session_close_utc - timedelta(minutes=1)  # type: ignore[operator]
            for item in nasdaq[-21:]
        )
    minimum, maximum = min(required), max(required)
    result: dict[str, dict[datetime, BinanceCandleV1]] = {}
    from .free_v2_formulas import CRYPTO_SYMBOLS

    for symbol in CRYPTO_SYMBOLS:
        selected: dict[datetime, BinanceCandleV1] = {}
        with (root / "canonical" / f"{symbol}.csv").open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != BINANCE_BREADTH_V2_SCHEMA:
                raise ValueError("Binance V2 canonical schema mismatch")
            for row in reader:
                opened = datetime.fromtimestamp(int(row["open_time_ms"]) / 1_000, UTC)
                if opened < minimum:
                    continue
                if opened > maximum:
                    break
                if opened in required:
                    selected[opened] = BinanceCandleV1(
                        symbol,
                        opened,
                        Decimal(row["close"]),
                        Decimal(row["quote_volume"]),
                        _digest((BINANCE_V2_AUTHORITY_DIGEST, symbol, row["open_time_ms"])),
                    )
        result[symbol] = selected
    return result


def load_real_materializer_inputs(
    timestamp: datetime,
    *,
    binance_root: Path = BINANCE_BREADTH_V2_ROOT,
    defillama_root: Path = DEFILLAMA_PUBLICATION_ROOT,
    fred_root: Path = FRED_ALFRED_V2_PUBLICATION_ROOT,
    verified_authorities: tuple[SourceAuthorityBindingV1, ...] | None = None,
) -> MaterializerInputsV1:
    """Verify and load only the bounded real authority rows needed at one timestamp."""
    at = validate_feature_timestamp(timestamp)
    authorities = verified_authorities or verify_real_source_authorities(
        binance_root=binance_root, defillama_root=defillama_root, fred_root=fred_root
    )
    fred = _load_fred_values(fred_root, at)
    return MaterializerInputsV1(
        authorities,
        _load_binance_values(binance_root, at, fred),
        _load_stablecoin_values(defillama_root, at),
        fred,
    )


def load_real_materializer_chunk_inputs(
    start: datetime,
    end_exclusive: datetime,
    authorities: tuple[SourceAuthorityBindingV1, ...],
    *,
    binance_root: Path = BINANCE_BREADTH_V2_ROOT,
    defillama_root: Path = DEFILLAMA_PUBLICATION_ROOT,
    fred_root: Path = FRED_ALFRED_V2_PUBLICATION_ROOT,
) -> MaterializerInputsV1:
    """Load one bounded execution chunk after authorities were verified once."""
    first = validate_feature_timestamp(start)
    if end_exclusive <= first or end_exclusive > FEATURE_TIMELINE_END_EXCLUSIVE:
        raise ValueError("invalid materializer chunk")
    last = end_exclusive - timedelta(minutes=1)
    fred = _load_fred_values_range(fred_root, first, end_exclusive)
    stablecoins = _load_stablecoin_values(defillama_root, last)
    lower = first - timedelta(minutes=61)
    session_opens = {
        item.source_session_close_utc - timedelta(minutes=1)  # type: ignore[operator]
        for item in fred.get("NASDAQCOM", ())
        if item.source_session_close_utc is not None and item.usable_at_utc <= last
    }
    last_session_open = max(session_opens, default=lower)
    result: dict[str, dict[datetime, BinanceCandleV1]] = {}
    from .free_v2_formulas import CRYPTO_SYMBOLS

    for symbol in CRYPTO_SYMBOLS:
        selected: dict[datetime, BinanceCandleV1] = {}
        with (binance_root / "canonical" / f"{symbol}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != BINANCE_BREADTH_V2_SCHEMA:
                raise ValueError("Binance V2 canonical schema mismatch")
            for row in reader:
                opened = datetime.fromtimestamp(int(row["open_time_ms"]) / 1_000, UTC)
                needed = lower <= opened < end_exclusive or (
                    symbol == "BTCUSDT" and opened in session_opens
                )
                if needed:
                    selected[opened] = BinanceCandleV1(
                        symbol,
                        opened,
                        Decimal(row["close"]),
                        Decimal(row["quote_volume"]),
                        _digest((BINANCE_V2_AUTHORITY_DIGEST, symbol, row["open_time_ms"])),
                    )
                if opened >= end_exclusive and (symbol != "BTCUSDT" or opened > last_session_open):
                    break
        result[symbol] = selected
    return MaterializerInputsV1(authorities, result, stablecoins, fred)


def materialize_timestamp(
    inputs: MaterializerInputsV1, timestamp: datetime
) -> tuple[CapitalFlowFeatureValueV2, ...]:
    """Materialize exactly 15 ordered values at one non-blind decision boundary."""
    at = validate_feature_timestamp(timestamp)
    if len(inputs.authorities) != 3:
        raise ValueError("three validated authority bindings required")
    results: list[CapitalFlowFeatureValueV2] = []

    def emit(
        name: str, fn: object, ids: Sequence[str], window: str, start: datetime | None
    ) -> None:
        try:
            results.append(_make(name, at, inputs.authorities, ids, window, start, value=fn()))  # type: ignore[operator]
        except FeatureFormulaUnavailable as error:
            results.append(
                _make(name, at, inputs.authorities, ids, window, start, state=error.reason)
            )

    symbols = tuple(inputs.binance)
    closes: dict[str, Decimal] = {}
    volumes: dict[str, Decimal] = {}
    crypto_ids: list[str] = []
    volume_ids: list[str] = []
    crypto_error: str | None = None
    volume_error: str | None = None
    from .free_v2_formulas import CRYPTO_SYMBOLS

    if set(symbols) != set(CRYPTO_SYMBOLS):
        crypto_error = MISSING_REQUIRED_SYMBOL
    else:
        for symbol in CRYPTO_SYMBOLS:
            candles = inputs.binance[symbol]
            window = {
                t: candles[t - timedelta(minutes=1)]
                for t in (at - timedelta(minutes=offset) for offset in range(60, -1, -1))
                if t - timedelta(minutes=1) in candles
            }
            try:
                closes[symbol] = crypto_60m_return(
                    {x.close_boundary_utc: x.close for x in window.values()}, at
                )
                crypto_ids.extend(x.source_identity for x in window.values())
                volume_ids.extend(
                    x.source_identity
                    for x in window.values()
                    if x.close_boundary_utc > at - timedelta(minutes=60)
                )
                volumes[symbol] = quote_volume_60m(
                    {
                        x.close_boundary_utc: x.quote_volume
                        for x in window.values()
                        if x.quote_volume is not None
                        and x.close_boundary_utc > at - timedelta(minutes=60)
                    },
                    at,
                )
            except FeatureFormulaUnavailable as error:
                crypto_error = crypto_error or error.reason
            if any(x.quote_volume is None for x in window.values()):
                volume_error = SOURCE_UNAVAILABLE

    def crypto(calc: object) -> Decimal:
        if crypto_error:
            raise FeatureFormulaUnavailable(crypto_error)
        return calc()  # type: ignore[operator]

    emit(
        FEATURE_ORDER[0],
        lambda: crypto(lambda: crypto_positive_share(closes)),
        crypto_ids,
        "EXACT_60M",
        at - timedelta(minutes=60),
    )
    emit(
        FEATURE_ORDER[1],
        lambda: crypto(lambda: crypto_population_dispersion(closes)),
        crypto_ids,
        "EXACT_60M",
        at - timedelta(minutes=60),
    )
    emit(
        FEATURE_ORDER[2],
        lambda: crypto(lambda: crypto_alt_outperform_share(closes)),
        crypto_ids,
        "EXACT_60M",
        at - timedelta(minutes=60),
    )
    emit(
        FEATURE_ORDER[3],
        lambda: (
            (_ for _ in ()).throw(FeatureFormulaUnavailable(volume_error))
            if volume_error
            else crypto(lambda: btc_quote_volume_share(volumes))
        ),
        volume_ids,
        "EXACT_60_CANDLES",
        at - timedelta(minutes=60),
    )

    for name, lag in ((FEATURE_ORDER[4], 1), (FEATURE_ORDER[5], 7)):
        ids: list[str] = []

        def stable(lag: int = lag, identities: list[str] = ids) -> Decimal:
            maps = {
                s: {x.reference_date: x for x in _eligible(inputs.stablecoins.get(s, ()), at)}
                for s in ("USDT", "USDC", "DAI")
            }
            common = (
                set.intersection(*(set(x) for x in maps.values())) if all(maps.values()) else set()
            )
            if not common:
                raise FeatureFormulaUnavailable(SOURCE_UNAVAILABLE)
            day = max(common)
            if any(
                maps[s][day].stale_after_utc is not None and at >= maps[s][day].stale_after_utc
                for s in maps
            ):
                raise FeatureFormulaUnavailable(SOURCE_STALE)
            prior = day - timedelta(days=lag)
            if any(prior not in maps[s] for s in maps):
                raise FeatureFormulaUnavailable(WINDOW_INCOMPLETE)
            current = {s: maps[s][day].value for s in maps}
            old = {s: maps[s][prior].value for s in maps}
            identities.extend(maps[s][d].reference_identity for s in maps for d in (prior, day))
            return stablecoin_log_change(current, old)

        emit(name, stable, ids, f"COMMON_D_MINUS_{lag}D", None)

    def series(name: str, count: int) -> list[CausalValueV1]:
        vals = _eligible(inputs.fred.get(name, ()), at)
        if not vals:
            raise FeatureFormulaUnavailable(SOURCE_UNAVAILABLE)
        if vals[-1].stale_after_utc is not None and at >= vals[-1].stale_after_utc:
            raise FeatureFormulaUnavailable(SOURCE_STALE)
        if len(vals) < count:
            raise FeatureFormulaUnavailable(INSUFFICIENT_HISTORY)
        return vals[-count:]

    specs = (
        (FEATURE_ORDER[6], "NASDAQCOM", 2, nasdaq_previous_session_return),
        (FEATURE_ORDER[7], "VIXCLS", 2, vix_point_change),
        (FEATURE_ORDER[8], "VIXCLS", 60, vix_zscore_60),
        (FEATURE_ORDER[9], "DEXJPUS", 6, five_observation_log_return),
        (FEATURE_ORDER[10], "DTWEXBGS", 6, five_observation_log_return),
        (FEATURE_ORDER[11], "DGS2", 6, treasury_change_bp),
        (FEATURE_ORDER[12], "DGS10", 6, treasury_change_bp),
    )
    for name, sid, count, calc in specs:
        ids: list[str] = []

        def slow(
            sid: str = sid,
            count: int = count,
            calc: object = calc,
            identities: list[str] = ids,
        ) -> Decimal:
            vals = series(sid, count)
            identities.extend(x.reference_identity for x in vals)
            numbers = [x.value for x in vals]
            return calc(numbers[0], numbers[-1]) if count == 2 else calc(numbers)  # type: ignore[operator]

        emit(name, slow, ids, f"{sid}_{count}_VALID_OBSERVATIONS", None)
    stress_ids: list[str] = []

    def stress() -> Decimal:
        x = _latest(inputs.fred.get("STLFSI4", ()), at)
        stress_ids.append(x.reference_identity)
        return x.value

    emit(FEATURE_ORDER[13], stress, stress_ids, "LATEST_CAUSAL_NON_STALE", None)
    coupling_ids: list[str] = []

    def coupling() -> Decimal:
        sessions = series("NASDAQCOM", 21)
        if any(x.source_session_close_utc is None for x in sessions):
            raise FeatureFormulaUnavailable(INVALID_SOURCE_SESSION)
        btc = []
        for x in sessions:
            opened = x.source_session_close_utc - timedelta(minutes=1)  # type: ignore[operator]
            candle = inputs.binance.get("BTCUSDT", {}).get(opened)
            if candle is None:
                raise FeatureFormulaUnavailable(WINDOW_INCOMPLETE)
            btc.append(candle.close)
            coupling_ids.extend((x.reference_identity, candle.source_identity))
        return pearson_population(
            [natural_log_return(btc[i], btc[i + 1]) for i in range(20)],
            [natural_log_return(sessions[i].value, sessions[i + 1].value) for i in range(20)],
        )

    emit(FEATURE_ORDER[14], coupling, coupling_ids, "21_SESSIONS_TO_20_RETURNS", None)
    return tuple(results)


def dataset_contract_payload(authorities: Sequence[SourceAuthorityBindingV1]) -> dict[str, object]:
    if tuple((item.identity, item.semantic_digest) for item in authorities) != (
        VALIDATED_AUTHORITY_BINDINGS
    ):
        raise ValueError("feature dataset requires exact validated authority bindings")
    return {
        "dataset_identity": CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
        "catalogue_identity": FREE_FEATURE_CATALOGUE_V2_IDENTITY,
        "formula_identity": CAPITAL_FLOW_FREE_V2_FEATURE_FORMULAS_IDENTITY,
        "feature_order": list(FEATURE_ORDER),
        "timeline_start": FEATURE_TIMELINE_START.isoformat(),
        "timeline_end_exclusive": FEATURE_TIMELINE_END_EXCLUSIVE.isoformat(),
        "timeline_minutes": FEATURE_TIMELINE_MINUTES,
        "source_authorities": [(x.identity, x.semantic_digest) for x in authorities],
    }
