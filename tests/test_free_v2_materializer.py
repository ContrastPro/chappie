from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.research.capital_flow import free_v2_materializer as materializer
from app.research.capital_flow.free_v2_formulas import (
    CRYPTO_SYMBOLS,
    FEATURE_TIMELINE_END_EXCLUSIVE,
    FEATURE_TIMELINE_MINUTES,
    FEATURE_TIMELINE_START,
    SOURCE_STALE,
    SOURCE_UNAVAILABLE,
)
from app.research.capital_flow.free_v2_materializer import (
    AVAILABLE,
    CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY,
    FEATURE_ORDER,
    VALIDATED_AUTHORITY_BINDINGS,
    BinanceCandleV1,
    CausalValueV1,
    MaterializerInputsV1,
    SourceAuthorityBindingV1,
    dataset_contract_payload,
    materialize_timestamp,
)
from app.research.capital_flow.free_v2_publication import (
    publish_feature_dataset,
    verify_feature_dataset,
)

T = datetime(2024, 4, 1, tzinfo=UTC)
AUTH = tuple(SourceAuthorityBindingV1(*item) for item in VALIDATED_AUTHORITY_BINDINGS)


def _value(
    series: str, index: int, *, close: datetime | None = None, stale: datetime | None = None
) -> CausalValueV1:
    day = date(2023, 12, 1) + timedelta(days=index)
    return CausalValueV1(
        f"{series}-{index}", day, Decimal(index + 10), T - timedelta(days=100 - index), stale, close
    )


def _inputs(*, quote: bool = True, stale_stress: bool = False) -> MaterializerInputsV1:
    binance = {}
    for number, symbol in enumerate(CRYPTO_SYMBOLS, 1):
        rows = {}
        for offset in range(62):
            opened = T - timedelta(minutes=61 - offset)
            rows[opened] = BinanceCandleV1(
                symbol,
                opened,
                Decimal(100 + number + offset),
                Decimal(number) if quote else None,
                f"{symbol}-{offset}",
            )
        binance[symbol] = rows
    sessions = []
    for index in range(60):
        close = T - timedelta(days=60 - index, hours=4)
        sessions.append(_value("NASDAQCOM", index, close=close))
        binance["BTCUSDT"][close - timedelta(minutes=1)] = BinanceCandleV1(
            "BTCUSDT",
            close - timedelta(minutes=1),
            Decimal(200 + index),
            Decimal(1),
            f"btc-session-{index}",
        )
    stablecoins = {
        symbol: tuple(
            CausalValueV1(
                f"{symbol}-{i}",
                date(2024, 3, 20) + timedelta(days=i),
                Decimal(100 + i),
                T - timedelta(days=12 - i),
            )
            for i in range(12)
        )
        for symbol in ("USDT", "USDC", "DAI")
    }
    fred = {
        "NASDAQCOM": tuple(sessions),
        "VIXCLS": tuple(_value("VIXCLS", i) for i in range(60)),
        "DEXJPUS": tuple(_value("DEXJPUS", i) for i in range(6)),
        "DTWEXBGS": tuple(_value("DTWEXBGS", i) for i in range(6)),
        "DGS2": tuple(_value("DGS2", i) for i in range(6)),
        "DGS10": tuple(_value("DGS10", i) for i in range(6)),
        "STLFSI4": (_value("STLFSI4", 1, stale=T if stale_stress else None),),
    }
    return MaterializerInputsV1(AUTH, binance, stablecoins, fred)


def test_dataset_identity_order_and_timeline_are_frozen() -> None:
    payload = dataset_contract_payload(AUTH)
    assert payload["dataset_identity"] == CAPITAL_FLOW_FREE_V2_FEATURE_DATASET_IDENTITY
    assert tuple(payload["feature_order"]) == FEATURE_ORDER
    assert len(FEATURE_ORDER) == 15
    assert FEATURE_ORDER[1:3] == (
        "breadth.crypto_dispersion_60m",
        "breadth.crypto_alt_outperform_btc_share_60m",
    )
    assert payload["timeline_minutes"] == FEATURE_TIMELINE_MINUTES == 1_270_080
    assert FEATURE_TIMELINE_START == datetime(2024, 1, 1, tzinfo=UTC)
    assert FEATURE_TIMELINE_END_EXCLUSIVE == datetime(2026, 6, 1, tzinfo=UTC)


def test_active_materializer_rejects_binance_v1_and_wrong_digests() -> None:
    values = _inputs()
    wrong = (
        SourceAuthorityBindingV1("chappie_binance_breadth_historical_authority/1.0.0", "0" * 64),
        *AUTH[1:],
    )
    with pytest.raises(ValueError, match="exact validated authority"):
        MaterializerInputsV1(wrong, values.binance, values.stablecoins, values.fred)


def test_real_authority_verification_requires_exact_three_digests(monkeypatch) -> None:
    monkeypatch.setattr(
        materializer,
        "verify_binance_breadth_v2",
        lambda _: SimpleNamespace(semantic_authority_digest=VALIDATED_AUTHORITY_BINDINGS[0][1]),
    )
    monkeypatch.setattr(
        materializer,
        "verify_defillama_stablecoin_authority",
        lambda _: SimpleNamespace(semantic_authority_digest=VALIDATED_AUTHORITY_BINDINGS[1][1]),
    )
    monkeypatch.setattr(
        materializer,
        "verify_fred_alfred_v2_authority",
        lambda _: SimpleNamespace(semantic_authority_digest=VALIDATED_AUTHORITY_BINDINGS[2][1]),
    )
    assert (
        tuple(
            (item.identity, item.semantic_digest)
            for item in materializer.verify_real_source_authorities()
        )
        == VALIDATED_AUTHORITY_BINDINGS
    )
    monkeypatch.setattr(
        materializer,
        "verify_binance_breadth_v2",
        lambda _: SimpleNamespace(semantic_authority_digest="0" * 64),
    )
    with pytest.raises(ValueError, match="identity/digest"):
        materializer.verify_real_source_authorities()


def test_binance_v2_loader_reads_direct_quote_volume_not_base_volume(tmp_path) -> None:
    root = tmp_path / "binance-v2"
    canonical = root / "canonical"
    canonical.mkdir(parents=True)
    for symbol in CRYPTO_SYMBOLS:
        lines = [",".join(materializer.BINANCE_BREADTH_V2_SCHEMA)]
        for offset in range(61, 0, -1):
            opened = T - timedelta(minutes=offset)
            lines.append(f"{int(opened.timestamp() * 1000)},9,11,8,10,2.5,999.125")
        (canonical / f"{symbol}.csv").write_text("\n".join(lines) + "\n")
    loaded = materializer._load_binance_values(root, T, {})
    candle = loaded["BTCUSDT"][T - timedelta(minutes=1)]
    assert candle.close == Decimal(10)
    assert candle.quote_volume == Decimal("999.125")
    assert candle.quote_volume != Decimal("2.5")


def test_all_15_fixture_features_are_available_causal_finite_and_deterministic() -> None:
    first = materialize_timestamp(_inputs(), T)
    second = materialize_timestamp(_inputs(), T)
    assert tuple(x.feature_name for x in first) == FEATURE_ORDER
    assert first == second
    assert all(x.state == AVAILABLE and x.value is not None and x.value.is_finite() for x in first)
    assert all(len(x.feature_digest) == 64 for x in first)
    assert all(identity for x in first for identity in x.input_causal_identities)
    with pytest.raises(FrozenInstanceError):
        first[0].state = SOURCE_UNAVAILABLE  # type: ignore[misc]


def test_completed_candle_boundary_and_quote_volume_fail_closed() -> None:
    rows = materialize_timestamp(_inputs(quote=False), T)
    assert rows[3].state == SOURCE_UNAVAILABLE and rows[3].value is None
    broken = _inputs()
    del broken.binance["ETHUSDT"][T - timedelta(minutes=61)]  # type: ignore[index]
    assert materialize_timestamp(broken, T)[0].state != AVAILABLE


def test_staleness_transition_is_explicit() -> None:
    assert materialize_timestamp(_inputs(stale_stress=True), T)[13].state == SOURCE_STALE


def test_publication_round_trip_manifest_last_create_only_and_tamper(tmp_path) -> None:
    rows = materialize_timestamp(_inputs(), T)
    phases = []
    result = publish_feature_dataset(rows, AUTH, tmp_path, phase_hook=phases.append)
    assert phases == ["artifacts_verified", "artifacts_published", "manifest_published"]
    assert result.row_count == 15
    assert verify_feature_dataset(tmp_path, AUTH) == result
    with pytest.raises(FileExistsError):
        publish_feature_dataset(rows, AUTH, tmp_path)
    artifact = tmp_path / "features.ndjson"
    artifact.write_text(artifact.read_text() + json.dumps({"tamper": True}) + "\n")
    with pytest.raises(ValueError, match="artifact digest mismatch"):
        verify_feature_dataset(tmp_path, AUTH)
