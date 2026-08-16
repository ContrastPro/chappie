from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.market.models import Candle
from app.strategy_history import (
    MAX_PAGE_SIZE,
    HistoricalDataset,
    HistoricalDatasetMetadata,
    PublicKlineDownloader,
    candles_sha256,
    load_dataset,
    utc_date_to_ms,
    validate_candles,
    write_dataset_atomic,
)
from app.strategy_lab import StrategyComparisonConfig
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
