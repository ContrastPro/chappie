from __future__ import annotations

import csv
import io
import zipfile
from decimal import Decimal

import pytest

from app.research.intelligence.flow import (
    FIVE_MINUTES_MS,
    METRICS_COLUMNS,
    FuturesMetricsObservation,
    MetricsDataset,
    SpotMinuteObservation,
    aggregate_completed_spot_5m,
    build_flow_observations,
    canonicalize_metrics_rows,
    conditional_lift,
    cross_asset_flow_state,
    flow_to_payload,
    future_return_bps,
    load_flow_payload,
    load_metrics_payload,
    metrics_to_payload,
    parse_spot_minute_rows,
    parse_spot_minute_rows_excluding_invalid,
)
from app.research.intelligence.market import ContextDataset, ContextObservation
from app.tools.market_intelligence_flow_download import (
    MAX_WORKERS,
    DailyMetricsResult,
    _assemble_metrics,
    _cached_funding,
    _derivatives_source_manifest,
    _download_metrics_day,
    _funding_cache_path,
    _funding_payload,
    _load_cached_metrics,
    _progress_payload,
    _reject_cross_day_duplicates,
    _write_gzip_atomic,
)


def _metrics_row(
    time: str, *, oi: str = "100", ratio: str = "1.1", taker: str = "1.2"
) -> tuple[str, ...]:
    return (time, "BTCUSDT", oi, "10000", "1.2", "1.3", ratio, taker)


def _metrics(*items: FuturesMetricsObservation) -> MetricsDataset:
    return MetricsDataset("BTCUSDT", "fixture", items, len(items), True, ())


def _minute(
    index: int, *, quote: str = "10", buy: str = "6", close: str = "100"
) -> SpotMinuteObservation:
    return SpotMinuteObservation(
        index * 60_000,
        index * 60_000 + 59_999,
        Decimal(close),
        Decimal(quote),
        2,
        Decimal(buy),
    )


def test_unordered_metrics_are_canonicalized_and_duplicate_timestamps_fail_closed() -> None:
    rows = (
        METRICS_COLUMNS,
        _metrics_row("2024-01-01 00:10:00"),
        _metrics_row("2024-01-01 00:00:00"),
        _metrics_row("2024-01-01 00:05:00"),
    )
    dataset = canonicalize_metrics_rows(rows, symbol="BTCUSDT", source="fixture")
    assert not dataset.was_source_ordered
    assert [item.timestamp_ms for item in dataset.observations] == sorted(
        item.timestamp_ms for item in dataset.observations
    )
    assert dataset.gaps_after_ms == ()
    with pytest.raises(ValueError, match="duplicate"):
        canonicalize_metrics_rows(
            rows + (_metrics_row("2024-01-01 00:05:00"),), symbol="BTCUSDT", source="fixture"
        )


def test_metrics_gap_is_explicit_and_never_repaired() -> None:
    dataset = canonicalize_metrics_rows(
        (METRICS_COLUMNS, _metrics_row("2024-01-01 00:00:00"), _metrics_row("2024-01-01 00:10:00")),
        symbol="BTCUSDT",
        source="fixture",
    )
    assert len(dataset.gaps_after_ms) == 1


def test_completed_1m_to_5m_spot_flow_has_exact_taker_arithmetic() -> None:
    result = aggregate_completed_spot_5m(
        tuple(_minute(index) for index in range(10)), volume_lookback=1
    )
    assert len(result) == 2
    first, second = result
    assert first.quote_volume == Decimal(50)
    assert first.taker_buy_quote_volume == Decimal(30)
    assert first.taker_sell_quote_volume == Decimal(20)
    assert first.taker_buy_quote_ratio == Decimal("0.6")
    assert first.signed_taker_quote_imbalance == Decimal("0.2")
    assert first.trade_count == 10
    assert first.average_quote_per_trade == Decimal(5)
    assert second.relative_quote_volume == Decimal(1)


def test_incomplete_1m_group_never_becomes_a_5m_observation() -> None:
    rows = tuple(_minute(index) for index in (0, 1, 2, 3, 5, 6, 7, 8, 9))
    # The parser is strict at source ingestion; a gap fails rather than creating an invented bar.
    raw = [
        (
            str(row.open_time_ms),
            "1",
            "1",
            "1",
            str(row.close),
            "1",
            str(row.close_time_ms),
            str(row.quote_volume),
            str(row.trade_count),
            "0",
            str(row.taker_buy_quote_volume),
            "0",
        )
        for row in rows
    ]
    with pytest.raises(ValueError, match="gap"):
        parse_spot_minute_rows(raw)


def test_invalid_public_spot_row_is_recorded_and_creates_no_cross_gap_baseline() -> None:
    rows = [
        (
            str(index * 60_000),
            "1",
            "1",
            "1",
            "100",
            "1",
            str(index * 60_000 + 59_999),
            "10",
            "2",
            "0",
            "6",
            "0",
        )
        for index in range(16)
    ]
    rows[5] = (*rows[5][:6], str(5 * 60_000 + 10), *rows[5][7:])
    minutes, anomalies = parse_spot_minute_rows_excluding_invalid(rows)
    assert len(minutes) == 15
    assert anomalies[0]["type"] == "INVALID_SPOT_1M_SOURCE_ROW"
    flow = aggregate_completed_spot_5m(minutes, volume_lookback=12)
    assert [item.timestamp_ms for item in flow] == [299_999, 899_999]
    assert flow[1].relative_quote_volume is None


def test_oi_changes_states_and_funding_asof_have_no_future_leakage() -> None:
    spot = aggregate_completed_spot_5m(
        tuple(_minute(index, close=str(100 + index)) for index in range(15))
    )
    metrics = _metrics(
        FuturesMetricsObservation(
            0, "BTCUSDT", Decimal(100), Decimal(1000), None, Decimal("1.2"), Decimal("1.2")
        ),
        FuturesMetricsObservation(
            FIVE_MINUTES_MS,
            "BTCUSDT",
            Decimal(101),
            Decimal(1100),
            None,
            Decimal("1.2"),
            Decimal("1.2"),
        ),
        FuturesMetricsObservation(
            2 * FIVE_MINUTES_MS,
            "BTCUSDT",
            Decimal(102),
            Decimal(1200),
            None,
            Decimal("1.2"),
            Decimal("1.2"),
        ),
    )
    funding = ContextDataset(
        "funding_rate",
        "BTCUSDT",
        "8h",
        "fixture",
        (
            ContextObservation(
                10 * FIVE_MINUTES_MS, 10 * FIVE_MINUTES_MS, Decimal("0.1"), "fixture"
            ),
        ),
    )
    rows = build_flow_observations(symbol="BTCUSDT", spot=spot, metrics=metrics, funding=funding)
    assert rows[1].oi_change_5m == Decimal(1)
    assert rows[1].price_oi_state == "PRICE_UP_OI_UP"
    assert rows[1].funding is None


def test_metrics_staleness_is_unavailable_not_arbitrarily_forward_filled() -> None:
    spot = aggregate_completed_spot_5m(tuple(_minute(index) for index in range(20)))
    metrics = _metrics(
        FuturesMetricsObservation(
            0, "BTCUSDT", Decimal(100), Decimal(1000), None, Decimal(1), Decimal(1)
        )
    )
    rows = build_flow_observations(symbol="BTCUSDT", spot=spot, metrics=metrics, funding=None)
    assert rows[0].metrics is not None
    assert rows[2].metrics is None


def test_oi_change_never_crosses_an_explicit_metrics_gap() -> None:
    spot = aggregate_completed_spot_5m(tuple(_minute(index) for index in range(30)))
    metrics = _metrics(
        FuturesMetricsObservation(0, "BTCUSDT", Decimal(100), Decimal(1), None, None, None),
        FuturesMetricsObservation(
            FIVE_MINUTES_MS, "BTCUSDT", Decimal(101), Decimal(1), None, None, None
        ),
        FuturesMetricsObservation(
            3 * FIVE_MINUTES_MS, "BTCUSDT", Decimal(103), Decimal(1), None, None, None
        ),
    )
    rows = build_flow_observations(symbol="BTCUSDT", spot=spot, metrics=metrics, funding=None)
    # The 15m source item has no exact 10m predecessor, so no change/state
    # may bridge that source discontinuity.
    after_gap = next(
        row for row in rows if row.metrics and row.metrics.timestamp_ms == 3 * FIVE_MINUTES_MS
    )
    assert after_gap.oi_change_5m is None
    assert after_gap.price_oi_state is None


def test_spot_only_flow_preserves_participation_and_marks_derivatives_unavailable() -> None:
    spot = aggregate_completed_spot_5m(tuple(_minute(index) for index in range(15)))
    no_derivatives = MetricsDataset("SOLUSDT", "NOT_REQUESTED", (), 0, True, ())
    rows = build_flow_observations(
        symbol="SOLUSDT", spot=spot, metrics=no_derivatives, funding=None
    )

    assert len(rows) == len(spot)
    assert all(row.metrics is None and row.funding is None for row in rows)
    assert all(row.oi_change_5m is None and row.price_oi_state is None for row in rows)
    assert rows[-1].spot.signed_taker_quote_imbalance == Decimal("0.2")


def test_nonpositive_source_open_interest_leaves_price_oi_state_unavailable() -> None:
    spot = aggregate_completed_spot_5m(tuple(_minute(index) for index in range(10)))
    metrics = _metrics(
        FuturesMetricsObservation(0, "BTCUSDT", Decimal(0), Decimal(1), None, None, None),
        FuturesMetricsObservation(
            FIVE_MINUTES_MS, "BTCUSDT", Decimal(1), Decimal(1), None, None, None
        ),
    )
    rows = build_flow_observations(symbol="BTCUSDT", spot=spot, metrics=metrics, funding=None)
    assert rows[1].price_oi_state is None


def test_cross_asset_alignment_and_future_labels_are_causal() -> None:
    spot = aggregate_completed_spot_5m(
        tuple(_minute(index, close=str(100 + index)) for index in range(20))
    )
    metrics = _metrics(
        *(
            FuturesMetricsObservation(
                index * FIVE_MINUTES_MS,
                "BTCUSDT",
                Decimal(100 + index),
                Decimal(1000),
                None,
                Decimal(1),
                Decimal("1.2"),
            )
            for index in range(5)
        )
    )
    btc = build_flow_observations(symbol="BTCUSDT", spot=spot, metrics=metrics, funding=None)
    eth = build_flow_observations(symbol="BTCUSDT", spot=spot, metrics=metrics, funding=None)
    assert cross_asset_flow_state(btc[1], eth[1]) in {"BROAD_CRYPTO_INFLOW_PROXY", "MIXED_FLOW"}
    assert future_return_bps(btc, 0, 1) is not None
    assert future_return_bps(btc, len(btc) - 1, 1) is None


def test_conditional_lift_uses_same_period_baseline_and_temporal_splits() -> None:
    output = conditional_lift(
        ((1704067200000, "A", Decimal(100)), (1704153600000, "B", Decimal(0))),
        threshold_bps=Decimal(20),
    )
    assert output["states"]["A"]["baseline_probability"] == "0.5"  # type: ignore[index]
    assert output["states"]["A"]["absolute_lift"] == "0.5"  # type: ignore[index]


def test_flow_payload_is_deterministic_and_tampering_fails_closed() -> None:
    spot = aggregate_completed_spot_5m(tuple(_minute(index) for index in range(5)))
    rows = build_flow_observations(
        symbol="BTCUSDT",
        spot=spot,
        metrics=_metrics(
            FuturesMetricsObservation(0, "BTCUSDT", Decimal(1), Decimal(1), None, None, None)
        ),
        funding=None,
    )
    payload = flow_to_payload(rows)
    assert payload["format"] == "chappie-flow-positioning/v1"
    assert load_flow_payload(payload) == rows
    payload["rows"][0]["close"] = "999"  # type: ignore[index]
    with pytest.raises(ValueError, match="digest"):
        load_flow_payload(payload)


class _Response:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls = 0

    def get(self, _path: str) -> _Response:
        self.calls += 1
        return self.responses.pop(0)


def _metrics_archive(*rows: tuple[str, ...]) -> bytes:
    output = io.StringIO()
    csv.writer(output).writerows((METRICS_COLUMNS, *rows))
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("BTCUSDT-metrics.csv", output.getvalue())
    return raw.getvalue()


def test_daily_metrics_cache_reuses_verified_artifact_and_preserves_canonical_order(
    tmp_path,
) -> None:
    client = _Client(
        [
            _Response(
                200,
                _metrics_archive(
                    _metrics_row("2024-01-01 00:05:00"),
                    _metrics_row("2024-01-01 00:00:00"),
                ),
            )
        ]
    )
    first = _download_metrics_day(client, tmp_path, "BTCUSDT", "2024-01-01")
    assert first.status == "VERIFIED"
    assert not first.reused
    assert client.calls == 1
    assert first.dataset is not None and not first.dataset.was_source_ordered
    second = _download_metrics_day(client, tmp_path, "BTCUSDT", "2024-01-01")
    assert second.status == "VERIFIED"
    assert second.reused
    assert client.calls == 1
    assert _load_cached_metrics(tmp_path, "BTCUSDT", "2024-01-01") == second
    payload = metrics_to_payload(second.dataset)  # type: ignore[arg-type]
    assert payload["format"] == "chappie-usdm-metrics/v1"
    assert load_metrics_payload(payload) == second.dataset  # type: ignore[arg-type]


def test_funding_cache_is_identity_and_digest_checked_and_reusable(tmp_path) -> None:
    funding = ContextDataset(
        "funding_rate",
        "SOLUSDT",
        "8h",
        "fixture",
        (
            ContextObservation(0, 0, Decimal("0.001"), "fixture"),
            ContextObservation(
                8 * 60 * 60 * 1_000, 8 * 60 * 60 * 1_000, Decimal("-0.001"), "fixture"
            ),
        ),
    )
    path = _funding_cache_path(tmp_path, "SOLUSDT", "2023-01-01", "2023-01-02")
    _write_gzip_atomic(path, _funding_payload(funding))
    assert (
        _cached_funding(cache_root=tmp_path, symbol="SOLUSDT", start="2023-01-01", end="2023-01-02")
        == funding
    )
    assert (
        _cached_funding(cache_root=tmp_path, symbol="BNBUSDT", start="2023-01-01", end="2023-01-02")
        is None
    )


def test_derivatives_manifest_is_verified_and_preserves_gap_and_source_status(tmp_path) -> None:
    metrics = canonicalize_metrics_rows(
        (
            METRICS_COLUMNS,
            _metrics_row("2024-01-01 00:05:00"),
            _metrics_row("2024-01-01 00:15:00"),
        ),
        symbol="BTCUSDT",
        source="fixture",
    )
    funding = ContextDataset(
        "funding_rate",
        "BTCUSDT",
        "8h",
        "fixture",
        (ContextObservation(0, 0, Decimal("0.001"), "fixture"),),
    )
    flow = build_flow_observations(
        symbol="BTCUSDT",
        spot=aggregate_completed_spot_5m(tuple(_minute(index) for index in range(10))),
        metrics=metrics,
        funding=funding,
    )
    manifest = _derivatives_source_manifest(
        symbol="BTCUSDT",
        start="2024-01-01",
        end="2024-01-02",
        metrics=metrics,
        funding=funding,
        daily_results={
            "2024-01-01": DailyMetricsResult("2024-01-01", "VERIFIED", metrics, "source", 10, True)
        },
        days=("2024-01-01",),
        cross_day_anomalies=({"type": "CROSS_DAY_DUPLICATE_METRICS_TIMESTAMP"},),
        flow=flow,
        metrics_path=tmp_path / "metrics.json.gz",
        funding_path=tmp_path / "funding.json.gz",
        flow_path=tmp_path / "flow.json.gz",
    )
    assert manifest["state"] == "VERIFIED"
    assert manifest["metrics"]["gap_count"] == 1  # type: ignore[index]
    assert manifest["metrics"]["segment_count"] == 2  # type: ignore[index]
    assert manifest["metrics"]["source_anomalies"] == [  # type: ignore[index]
        {"type": "CROSS_DAY_DUPLICATE_METRICS_TIMESTAMP"}
    ]


def test_transient_retry_and_permanent_missing_archive_are_classified(tmp_path) -> None:
    client = _Client(
        [
            _Response(500),
            _Response(200, _metrics_archive(_metrics_row("2024-01-02 00:00:00"))),
        ]
    )
    assert _download_metrics_day(client, tmp_path, "BTCUSDT", "2024-01-02").status == "VERIFIED"
    assert client.calls == 2
    assert (
        _download_metrics_day(_Client([_Response(404)]), tmp_path, "BTCUSDT", "2024-01-03").status
        == "MISSING_ARCHIVE"
    )


def test_assembly_is_deterministic_rejects_cross_day_duplicates_and_reports_gaps() -> None:
    first = canonicalize_metrics_rows(
        (METRICS_COLUMNS, _metrics_row("2024-01-01 00:00:00")), symbol="BTCUSDT", source="a"
    )
    second = canonicalize_metrics_rows(
        (METRICS_COLUMNS, _metrics_row("2024-01-01 00:10:00")), symbol="BTCUSDT", source="b"
    )
    assembled = _assemble_metrics(
        (
            DailyMetricsResult("2024-01-02", "VERIFIED", second, "b", 1, False),
            DailyMetricsResult("2024-01-01", "VERIFIED", first, "a", 1, False),
        ),
        symbol="BTCUSDT",
    )
    assert len(assembled.gaps_after_ms) == 1
    duplicate = DailyMetricsResult("2024-01-03", "VERIFIED", first, "c", 1, False)
    with pytest.raises(ValueError, match="duplicate"):
        _assemble_metrics(
            (DailyMetricsResult("2024-01-01", "VERIFIED", first, "a", 1, False), duplicate),
            symbol="BTCUSDT",
        )


def test_cross_day_duplicate_is_recorded_and_both_daily_inputs_fail_closed() -> None:
    dataset = canonicalize_metrics_rows(
        (METRICS_COLUMNS, _metrics_row("2024-01-01 00:00:00")), symbol="BTCUSDT", source="a"
    )
    results, anomalies = _reject_cross_day_duplicates(
        (
            DailyMetricsResult("2024-01-01", "VERIFIED", dataset, "a", 1, False),
            DailyMetricsResult("2024-01-02", "VERIFIED", dataset, "b", 1, False),
        )
    )
    assert [item.status for item in results] == ["INVALID_SOURCE", "INVALID_SOURCE"]
    assert anomalies[0]["type"] == "CROSS_DAY_DUPLICATE_METRICS_TIMESTAMP"


def test_progress_payload_is_deterministic_and_worker_cap_is_modest() -> None:
    assert MAX_WORKERS == 8
    progress = _progress_payload(
        (
            DailyMetricsResult("2024-01-02", "MISSING_ARCHIVE", None, None, 0, False),
            DailyMetricsResult("2024-01-01", "VERIFIED", None, "digest", 1, True),
        )
    )
    assert progress["coverage_percent"] == "50.0"
    assert [item["date"] for item in progress["days"]] == ["2024-01-01", "2024-01-02"]  # type: ignore[index]


def test_progress_manifest_keeps_unfinished_requested_days_explicit() -> None:
    progress = _progress_payload(
        (DailyMetricsResult("2024-01-01", "VERIFIED", None, "digest", 1, True),),
        requested_days=("2024-01-01", "2024-01-02"),
    )
    assert progress["coverage_percent"] == "50.0"
    assert progress["counts"] == {
        "VERIFIED": 1,
        "MISSING_ARCHIVE": 0,
        "TRANSIENT_FAILURE": 0,
        "INVALID_SOURCE": 0,
        "PENDING": 1,
    }
    assert progress["days"][1]["status"] == "PENDING"  # type: ignore[index]
