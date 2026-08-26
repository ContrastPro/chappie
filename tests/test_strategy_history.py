from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

from app.market.models import Candle
from app.research.strategy.history import (
    MAX_CANDLES_PER_ACQUISITION,
    MAX_PAGE_SIZE,
    HistoricalDataset,
    HistoricalDatasetMetadata,
    PublicKlineDownloader,
    candles_sha256,
    download_segmented,
    load_dataset,
    merge_historical_datasets,
    plan_historical_segments,
    utc_date_to_ms,
    validate_candles,
    write_dataset_atomic,
)
from app.research.strategy.lab import StrategyComparisonConfig
from app.tools import strategy_evaluate
from app.tools.strategy_evaluate import (
    aggregate,
    evaluate,
    report_payload,
    write_report_atomic,
)


def _raw(open_time: int, close: str = "100") -> list[object]:
    return [open_time, close, "101", "99", close, "1", open_time + 299_999]


def _candle(index: int, close: str = "100") -> Candle:
    price = Decimal(close)
    return Candle(
        open_time_ms=index * 300_000,
        close_time_ms=index * 300_000 + 299_999,
        open=price,
        high=price + Decimal(1),
        low=price - Decimal(1),
        close=price,
        volume=Decimal(1),
    )


def _dataset(candles: tuple[Candle, ...]) -> HistoricalDataset:
    return HistoricalDataset(
        HistoricalDatasetMetadata(
            "BTCUSDT",
            "5m",
            candles[0].open_time_ms,
            candles[-1].open_time_ms + 300_000,
            candles[0].open_time_ms,
            candles[-1].open_time_ms,
            len(candles),
            "2026-01-01T00:00:00+00:00",
            "fixture",
            candles_sha256(candles),
        ),
        candles,
    )


def test_downloader_paginates_and_emits_deterministic_dataset() -> None:
    requests: list[httpx.Request] = []
    first = [_raw(index * 300_000) for index in range(MAX_PAGE_SIZE)]
    second = [_raw(MAX_PAGE_SIZE * 300_000)]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=first if len(requests) == 1 else second, request=request)

    with httpx.Client(
        base_url="https://public.test", transport=httpx.MockTransport(respond)
    ) as client:
        result = PublicKlineDownloader(client).download(
            symbol="btcusdt", interval="5m", start_ms=0, end_ms=(MAX_PAGE_SIZE + 1) * 300_000
        )

    assert len(requests) == 2
    assert result.metadata.symbol == "BTCUSDT"
    assert result.metadata.candle_count == MAX_PAGE_SIZE + 1
    assert result.metadata.candles_sha256 == candles_sha256(result.candles)
    assert dict(requests[0].url.params)["startTime"] == "0"
    assert dict(requests[1].url.params)["startTime"] == str(MAX_PAGE_SIZE * 300_000)


@pytest.mark.parametrize(
    ("candles", "message"),
    [
        ((_candle(0), _candle(0)), "duplicate"),
        ((_candle(1), _candle(0)), "out of order"),
        ((_candle(0), _candle(2)), "gap"),
        (
            (
                Candle(
                    open_time_ms=0,
                    close_time_ms=1,
                    open=Decimal(100),
                    high=Decimal(101),
                    low=Decimal(99),
                    close=Decimal(100),
                    volume=Decimal(1),
                ),
            ),
            "close time",
        ),
    ],
)
def test_dataset_integrity_rejects_duplicate_gap_order_and_malformed_candles(
    candles: tuple[Candle, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_candles(candles, "5m")


def test_downloader_rejects_duplicate_page_data() -> None:
    payload = [_raw(0), _raw(0)]
    with (
        httpx.Client(
            base_url="https://public.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload, request=request)
            ),
        ) as client,
        pytest.raises(ValueError, match="duplicate"),
    ):
        PublicKlineDownloader(client).download(
            symbol="BTCUSDT", interval="5m", start_ms=0, end_ms=600_000
        )


def test_dataset_round_trip_digest_is_stable_and_tampering_fails(tmp_path: Path) -> None:
    dataset = _dataset(tuple(_candle(index) for index in range(30)))
    path = tmp_path / "candles.json"
    write_dataset_atomic(path, dataset)
    assert load_dataset(path) == dataset
    with pytest.raises(FileExistsError, match="already exists"):
        write_dataset_atomic(path, dataset)
    assert candles_sha256(dataset.candles) == candles_sha256(dataset.candles)
    path.write_text(
        path.read_text().replace('"close": "100"', '"close": "100.1"', 1), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="digest|OHLC"):
        load_dataset(path)


def test_evaluation_independent_windows_reset_capital_and_export(tmp_path: Path) -> None:
    first = _dataset(tuple(_candle(index, str(100 + (index % 3))) for index in range(40)))
    second = _dataset(tuple(_candle(index, str(200 + (index % 4))) for index in range(40)))
    first_path, second_path = tmp_path / "one.json", tmp_path / "two.json"
    write_dataset_atomic(first_path, first)
    write_dataset_atomic(second_path, second)
    evaluations = evaluate((first_path, second_path), StrategyComparisonConfig())
    assert all(
        result.starting_equity == Decimal(10000) for item in evaluations for result in item.results
    )
    repeated = evaluate((first_path, second_path), StrategyComparisonConfig())
    assert evaluations == repeated
    summary = aggregate(evaluations)
    assert {
        "ema_crossover",
        "trend_filtered",
        "breakout",
        "mean_reversion",
        "cost_aware_momentum",
        "regime_mean_reversion",
        "multi_timeframe_trend",
        "volatility_continuation",
        "trend_retracement_continuation",
        "compression_expansion_breakout",
    } == set(summary)
    assert all(values["total_windows"] == 2 for values in summary.values())
    report_path = tmp_path / "result.json"
    write_report_atomic(report_path, report_payload(evaluations))
    assert '"format": "chappie-strategy-evaluation/v1"' in report_path.read_text(encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"datasets": ["one.json", "two.json"]}', encoding="utf-8")
    assert strategy_evaluate.main(["--manifest", str(manifest)]) == 0


def test_dates_are_utc_calendar_dates_only() -> None:
    assert utc_date_to_ms("2026-06-01") == int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1_000)
    with pytest.raises(ValueError):
        utc_date_to_ms("2026-06-01T12:00:00")


def test_segment_planner_has_exact_contiguous_coverage_at_and_over_cap() -> None:
    minute = 60_000
    below = plan_historical_segments(
        symbol="BTCUSDT",
        interval="1m",
        start_ms=0,
        end_ms=(MAX_CANDLES_PER_ACQUISITION - 1) * minute,
    )
    one = plan_historical_segments(
        symbol="btcusdt", interval="1m", start_ms=0, end_ms=MAX_CANDLES_PER_ACQUISITION * minute
    )
    two = plan_historical_segments(
        symbol="BTCUSDT",
        interval="1m",
        start_ms=0,
        end_ms=(MAX_CANDLES_PER_ACQUISITION + 1) * minute,
    )
    large = plan_historical_segments(
        symbol="BTCUSDT", interval="1m", start_ms=0, end_ms=172_800 * minute
    )
    assert [(item.start_ms, item.end_ms, item.candle_count) for item in one] == [
        (0, MAX_CANDLES_PER_ACQUISITION * minute, MAX_CANDLES_PER_ACQUISITION)
    ]
    assert [item.candle_count for item in below] == [MAX_CANDLES_PER_ACQUISITION - 1]
    assert [item.candle_count for item in two] == [MAX_CANDLES_PER_ACQUISITION, 1]
    assert [item.candle_count for item in large] == [MAX_CANDLES_PER_ACQUISITION, 72_800]
    assert all(left.end_ms == right.start_ms for left, right in pairwise(large))


def test_merge_is_canonical_and_rejects_non_contiguous_or_incompatible_segments() -> None:
    all_candles = tuple(_candle(index) for index in range(6))
    first, second = _dataset(all_candles[:3]), _dataset(all_candles[3:])
    merged = merge_historical_datasets((first, second))
    assert merged.candles == all_candles
    assert merged.metadata.candles_sha256 == candles_sha256(all_candles)
    assert merged.metadata.candles_sha256 == _dataset(all_candles).metadata.candles_sha256

    gap = _dataset((_candle(4), _candle(5)))
    overlap = _dataset((_candle(2), _candle(3)))
    with pytest.raises(ValueError, match="contiguous"):
        merge_historical_datasets((first, gap))
    with pytest.raises(ValueError, match="contiguous"):
        merge_historical_datasets((first, overlap))
    with pytest.raises(ValueError, match="contiguous"):
        merge_historical_datasets((second, first))
    wrong_symbol = HistoricalDataset(
        HistoricalDatasetMetadata(
            "ETHUSDT",
            second.metadata.interval,
            second.metadata.requested_start_ms,
            second.metadata.requested_end_ms,
            second.metadata.actual_first_open_ms,
            second.metadata.actual_last_open_ms,
            second.metadata.candle_count,
            second.metadata.downloaded_at_utc,
            second.metadata.source,
            second.metadata.candles_sha256,
        ),
        second.candles,
    )
    with pytest.raises(ValueError, match="compatibility"):
        merge_historical_datasets((first, wrong_symbol))
    wrong_source = HistoricalDataset(replace(second.metadata, source="other"), second.candles)
    with pytest.raises(ValueError, match="compatibility"):
        merge_historical_datasets((first, wrong_source))


def test_segmented_download_validates_every_segment_and_returns_bounded_provenance() -> None:
    class FakeDownloader:
        def __init__(self, *, invalid_start: bool = False) -> None:
            self.invalid_start = invalid_start
            self.calls: list[tuple[int, int]] = []

        def download(
            self, *, symbol: str, interval: str, start_ms: int, end_ms: int
        ) -> HistoricalDataset:
            self.calls.append((start_ms, end_ms))
            candles = tuple(
                _candle(index) for index in range(start_ms // 300_000, end_ms // 300_000)
            )
            dataset = _dataset(candles)
            if self.invalid_start:
                return HistoricalDataset(
                    HistoricalDatasetMetadata(
                        dataset.metadata.symbol,
                        dataset.metadata.interval,
                        start_ms + 300_000,
                        end_ms,
                        dataset.metadata.actual_first_open_ms,
                        dataset.metadata.actual_last_open_ms,
                        dataset.metadata.candle_count,
                        dataset.metadata.downloaded_at_utc,
                        dataset.metadata.source,
                        dataset.metadata.candles_sha256,
                    ),
                    dataset.candles,
                )
            return dataset

    downloader = FakeDownloader()
    acquisition = download_segmented(
        downloader, symbol="BTCUSDT", interval="5m", start_ms=0, end_ms=1_500_000, max_candles=2
    )
    assert downloader.calls == [(0, 600_000), (600_000, 1_200_000), (1_200_000, 1_500_000)]
    assert acquisition.dataset.metadata.candle_count == 5
    assert acquisition.provenance_payload()["segment_count"] == 3
    assert len(acquisition.provenance_payload()["segments"]) == 3
    with pytest.raises(ValueError, match="planned range"):
        download_segmented(
            FakeDownloader(invalid_start=True),
            symbol="BTCUSDT",
            interval="5m",
            start_ms=0,
            end_ms=600_000,
            max_candles=2,
        )
    with pytest.raises(ValueError, match="capacity"):
        download_segmented(
            FakeDownloader(),
            symbol="BTCUSDT",
            interval="5m",
            start_ms=0,
            end_ms=600_000,
            max_candles=MAX_CANDLES_PER_ACQUISITION + 1,
        )
