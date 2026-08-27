"""Typed, versioned BTC/ETH/SOL precursor context authority."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

from app.ai.schemas import digest

from .context import (
    _FEATURE_KEYS,
    AUTHORITY_IDENTITY,
    _features,
    _source,
    validate_context_authority,
)
from .models import MarketPrecursorContextFeatureConfigV1

CONTEXT_FEATURE_CONTRACT_IDENTITY_V2 = "chappie_market_precursor_context_features/2.0.0"
CONTEXT_FEATURE_DATASET_IDENTITY_V2 = "chappie_market_precursor_context_dataset/2.0.0"
BOOLEAN_CONTEXT_FEATURES_V2 = frozenset(
    {
        "cross_market.btc_eth_direction_agreement",
        "cross_market.btc_sol_direction_agreement",
        "cross_market.all_three_direction_agreement",
    }
)
NUMERIC_CONTEXT_FEATURES_V2 = _FEATURE_KEYS - BOOLEAN_CONTEXT_FEATURES_V2


@dataclass(frozen=True, slots=True)
class MarketPrecursorContextFeatureConfigV2:
    """V1 causal calculations with explicit Boolean agreement features."""

    @property
    def warmup(self) -> int:
        return MarketPrecursorContextFeatureConfigV1().warmup

    def payload(self) -> dict[str, object]:
        v1 = MarketPrecursorContextFeatureConfigV1().payload()
        return {
            **v1,
            "identity": CONTEXT_FEATURE_CONTRACT_IDENTITY_V2,
            "numeric_features": sorted(NUMERIC_CONTEXT_FEATURES_V2),
            "boolean_features": sorted(BOOLEAN_CONTEXT_FEATURES_V2),
        }

    @property
    def contract_digest(self) -> str:
        return digest(self.payload())


@dataclass(frozen=True, slots=True)
class MarketPrecursorContextRowV2:
    close_time_ms: int
    target_symbol: str
    target_interval: str
    authority_manifest_identity: str
    authority_manifest_digest: str
    btc_source_digests: tuple[str, str]
    eth_source_digest: str
    sol_source_digest: str
    feature_contract_identity: str
    numeric_features: Mapping[str, Decimal]
    boolean_features: Mapping[str, bool]
    row_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "close_time_ms": self.close_time_ms,
            "target_symbol": self.target_symbol,
            "target_interval": self.target_interval,
            "authority_manifest_identity": self.authority_manifest_identity,
            "authority_manifest_digest": self.authority_manifest_digest,
            "btc_source_digests": list(self.btc_source_digests),
            "eth_source_digest": self.eth_source_digest,
            "sol_source_digest": self.sol_source_digest,
            "feature_contract_identity": self.feature_contract_identity,
            "numeric_features": {
                key: str(value) for key, value in sorted(self.numeric_features.items())
            },
            "boolean_features": dict(sorted(self.boolean_features.items())),
        }


@dataclass(frozen=True, slots=True)
class MarketPrecursorContextManifestV2:
    authority_manifest_digest: str
    btc_source_digests: tuple[str, str]
    eth_source_digest: str
    sol_source_digest: str
    source_start_ms: int
    source_end_ms: int
    row_count: int
    drop_counts: Mapping[str, int]
    row_sequence_digest: str
    dataset_digest: str
    rows_path: str

    def payload(self) -> dict[str, object]:
        config = MarketPrecursorContextFeatureConfigV2()
        return {
            "identity": CONTEXT_FEATURE_DATASET_IDENTITY_V2,
            "authority_manifest_identity": AUTHORITY_IDENTITY,
            "authority_manifest_digest": self.authority_manifest_digest,
            "btc_source_digests": list(self.btc_source_digests),
            "eth_source_digest": self.eth_source_digest,
            "sol_source_digest": self.sol_source_digest,
            "target_symbol": "BTCUSDT",
            "target_interval": "1m",
            "source_start_ms": self.source_start_ms,
            "source_end_ms": self.source_end_ms,
            "feature_contract_identity": CONTEXT_FEATURE_CONTRACT_IDENTITY_V2,
            "feature_contract_digest": config.contract_digest,
            "warmup": config.warmup,
            "numeric_feature_count": len(NUMERIC_CONTEXT_FEATURES_V2),
            "boolean_feature_count": len(BOOLEAN_CONTEXT_FEATURES_V2),
            "row_count": self.row_count,
            "drop_counts": dict(sorted(self.drop_counts.items())),
            "row_sequence_digest": self.row_sequence_digest,
            "dataset_digest": self.dataset_digest,
            "rows_path": self.rows_path,
        }


def _typed_features(btc, eth, sol, index):
    features = _features(btc, eth, sol, index)
    numeric = {key: value for key, value in features.items() if key in NUMERIC_CONTEXT_FEATURES_V2}
    boolean = {key: features[key] == Decimal(1) for key in sorted(BOOLEAN_CONTEXT_FEATURES_V2)}
    if (
        set(numeric) != NUMERIC_CONTEXT_FEATURES_V2
        or set(boolean) != BOOLEAN_CONTEXT_FEATURES_V2
        or any(type(value) is not Decimal or not value.is_finite() for value in numeric.values())
        or any(type(value) is not bool for value in boolean.values())
    ):
        raise ValueError("typed context feature set is invalid")
    return numeric, boolean


def _dataset_digest(authority_digest, config, sequence, drops):
    return digest(
        {
            "authority_manifest_digest": authority_digest,
            "feature_config": config.payload(),
            "row_sequence_digest": sequence,
            "drop_counts": drops,
        }
    )


class MarketPrecursorContextBuilderV2:
    def __init__(self, expected_authority_digest: str) -> None:
        self._expected_authority_digest = expected_authority_digest
        self._config = MarketPrecursorContextFeatureConfigV2()

    @property
    def config(self) -> MarketPrecursorContextFeatureConfigV2:
        return self._config

    def iter_rows(self, authority_path, btc_a, btc_b, eth, sol):
        validate_context_authority(
            authority_path, self._expected_authority_digest, btc_a, btc_b, eth, sol
        )
        btc = (*_source(btc_a, "BTCUSDT"), *_source(btc_b, "BTCUSDT"))
        eth_candles = _source(eth, "ETHUSDT")
        sol_candles = _source(sol, "SOLUSDT")
        source_digests = (
            (btc_a.metadata.candles_sha256, btc_b.metadata.candles_sha256),
            eth.metadata.candles_sha256,
            sol.metadata.candles_sha256,
        )
        for index in range(len(btc)):
            if not (
                btc[index].open_time_ms
                == eth_candles[index].open_time_ms
                == sol_candles[index].open_time_ms
            ):
                raise ValueError("context timestamp join is invalid")
            if index < self._config.warmup:
                continue
            numeric, boolean = _typed_features(btc, eth_candles, sol_candles, index)
            row = MarketPrecursorContextRowV2(
                btc[index].close_time_ms,
                "BTCUSDT",
                "1m",
                AUTHORITY_IDENTITY,
                self._expected_authority_digest,
                source_digests[0],
                source_digests[1],
                source_digests[2],
                CONTEXT_FEATURE_CONTRACT_IDENTITY_V2,
                numeric,
                boolean,
                "",
            )
            yield MarketPrecursorContextRowV2(
                row.close_time_ms,
                row.target_symbol,
                row.target_interval,
                row.authority_manifest_identity,
                row.authority_manifest_digest,
                row.btc_source_digests,
                row.eth_source_digest,
                row.sol_source_digest,
                row.feature_contract_identity,
                row.numeric_features,
                row.boolean_features,
                digest(row.payload()),
            )

    def materialize(self, authority_path, btc_a, btc_b, eth, sol, rows_path):
        rows_path = Path(rows_path)
        manifest_path = rows_path.with_suffix(rows_path.suffix + ".manifest.json")
        if rows_path.exists() or manifest_path.exists():
            raise FileExistsError("typed context output path is not clean")
        authority = validate_context_authority(
            authority_path, self._expected_authority_digest, btc_a, btc_b, eth, sol
        )
        sequence, count = sha256(), 0
        drops = {"INSUFFICIENT_CONTEXT_FEATURE_HISTORY": self._config.warmup}
        rows_temporary = manifest_temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=rows_path.parent, delete=False
            ) as output:
                rows_temporary = Path(output.name)
                for row in self.iter_rows(authority_path, btc_a, btc_b, eth, sol):
                    output.write(
                        json.dumps(
                            {**row.payload(), "row_digest": row.row_digest},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    sequence.update(row.row_digest.encode("ascii") + b"\n")
                    count += 1
                output.flush()
                os.fsync(output.fileno())
            manifest = MarketPrecursorContextManifestV2(
                self._expected_authority_digest,
                (btc_a.metadata.candles_sha256, btc_b.metadata.candles_sha256),
                eth.metadata.candles_sha256,
                sol.metadata.candles_sha256,
                authority["target"]["requested_start_ms"],
                authority["target"]["requested_end_ms"],
                count,
                drops,
                sequence.hexdigest(),
                _dataset_digest(
                    self._expected_authority_digest, self._config, sequence.hexdigest(), drops
                ),
                rows_path.name,
            )
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=rows_path.parent, delete=False
            ) as output:
                manifest_temporary = Path(output.name)
                output.write(
                    json.dumps(manifest.payload(), sort_keys=True, separators=(",", ":")) + "\n"
                )
                output.flush()
                os.fsync(output.fileno())
            os.link(rows_temporary, rows_path)
            try:
                os.link(manifest_temporary, manifest_path)
            except BaseException:
                rows_path.unlink()
                raise
            return manifest
        finally:
            if rows_temporary is not None:
                rows_temporary.unlink(missing_ok=True)
            if manifest_temporary is not None:
                manifest_temporary.unlink(missing_ok=True)


def iter_materialized_context_rows_v2(rows_path: Path) -> Iterator[MarketPrecursorContextRowV2]:
    with Path(rows_path).open(encoding="utf-8") as source:
        for line in source:
            try:
                raw = json.loads(line)
                if set(raw) != {
                    "close_time_ms",
                    "target_symbol",
                    "target_interval",
                    "authority_manifest_identity",
                    "authority_manifest_digest",
                    "btc_source_digests",
                    "eth_source_digest",
                    "sol_source_digest",
                    "feature_contract_identity",
                    "numeric_features",
                    "boolean_features",
                    "row_digest",
                }:
                    raise ValueError
                numeric_raw, boolean = raw["numeric_features"], raw["boolean_features"]
                if (
                    not isinstance(numeric_raw, dict)
                    or set(numeric_raw) != NUMERIC_CONTEXT_FEATURES_V2
                    or any(type(value) is not str for value in numeric_raw.values())
                    or not isinstance(boolean, dict)
                    or set(boolean) != BOOLEAN_CONTEXT_FEATURES_V2
                    or any(type(value) is not bool for value in boolean.values())
                ):
                    raise ValueError
                numeric = {key: Decimal(value) for key, value in numeric_raw.items()}
                if any(not value.is_finite() for value in numeric.values()):
                    raise ValueError
                row_digest = raw.pop("row_digest")
                row = MarketPrecursorContextRowV2(
                    raw["close_time_ms"],
                    raw["target_symbol"],
                    raw["target_interval"],
                    raw["authority_manifest_identity"],
                    raw["authority_manifest_digest"],
                    tuple(raw["btc_source_digests"]),
                    raw["eth_source_digest"],
                    raw["sol_source_digest"],
                    raw["feature_contract_identity"],
                    numeric,
                    boolean,
                    row_digest,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                InvalidOperation,
                json.JSONDecodeError,
            ) as error:
                raise ValueError("materialized typed context row is invalid") from error
            if digest(row.payload()) != row.row_digest:
                raise ValueError("materialized typed context row digest is invalid")
            yield row


def validate_materialized_context_dataset_v2(
    rows_path: Path, *, expected_authority_digest: str
) -> MarketPrecursorContextManifestV2:
    rows_path = Path(rows_path)
    try:
        raw = json.loads(
            rows_path.with_suffix(rows_path.suffix + ".manifest.json").read_text(encoding="utf-8")
        )
        config = MarketPrecursorContextFeatureConfigV2()
        expected_keys = set(
            MarketPrecursorContextManifestV2(
                "", ("", ""), "", "", 0, 0, 0, {}, "", "", ""
            ).payload()
        )
        if (
            set(raw) != expected_keys
            or raw["identity"] != CONTEXT_FEATURE_DATASET_IDENTITY_V2
            or raw["authority_manifest_identity"] != AUTHORITY_IDENTITY
            or raw["authority_manifest_digest"] != expected_authority_digest
            or raw["feature_contract_identity"] != CONTEXT_FEATURE_CONTRACT_IDENTITY_V2
            or raw["feature_contract_digest"] != config.contract_digest
            or raw["warmup"] != config.warmup
            or raw["numeric_feature_count"] != len(NUMERIC_CONTEXT_FEATURES_V2)
            or raw["boolean_feature_count"] != len(BOOLEAN_CONTEXT_FEATURES_V2)
            or raw["rows_path"] != rows_path.name
            or not isinstance(raw["btc_source_digests"], list)
            or len(raw["btc_source_digests"]) != 2
        ):
            raise ValueError
        sequence, count, previous = sha256(), 0, None
        for row in iter_materialized_context_rows_v2(rows_path):
            if (
                row.authority_manifest_identity != AUTHORITY_IDENTITY
                or row.authority_manifest_digest != expected_authority_digest
                or list(row.btc_source_digests) != raw["btc_source_digests"]
                or row.eth_source_digest != raw["eth_source_digest"]
                or row.sol_source_digest != raw["sol_source_digest"]
                or row.target_symbol != "BTCUSDT"
                or row.target_interval != "1m"
                or row.feature_contract_identity != CONTEXT_FEATURE_CONTRACT_IDENTITY_V2
                or (previous is not None and row.close_time_ms <= previous)
            ):
                raise ValueError
            sequence.update(row.row_digest.encode("ascii") + b"\n")
            count += 1
            previous = row.close_time_ms
        if (
            count == 0
            or count != raw["row_count"]
            or sequence.hexdigest() != raw["row_sequence_digest"]
            or _dataset_digest(
                expected_authority_digest, config, sequence.hexdigest(), raw["drop_counts"]
            )
            != raw["dataset_digest"]
        ):
            raise ValueError
        return MarketPrecursorContextManifestV2(
            expected_authority_digest,
            tuple(raw["btc_source_digests"]),
            raw["eth_source_digest"],
            raw["sol_source_digest"],
            raw["source_start_ms"],
            raw["source_end_ms"],
            count,
            raw["drop_counts"],
            sequence.hexdigest(),
            raw["dataset_digest"],
            raw["rows_path"],
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("materialized typed context dataset validation failed") from error
