from app.research_anomalies import anomaly_ledger_payload, quality_status, split_historical_rows


def _row(open_time: int, close_time: int | None = None) -> list[str]:
    return [
        str(open_time),
        "100",
        "101",
        "99",
        "100",
        "1",
        str(close_time if close_time is not None else open_time + 3_599_999),
    ]


def test_anomalies_split_segments_without_repair_and_ledger_is_deterministic() -> None:
    result = split_historical_rows(
        (_row(0), _row(3_600_000, 3_600_001), _row(7_200_000), _row(14_400_000)),
        source="test",
        symbol="BTCUSDT",
        interval="1h",
    )
    assert [len(segment) for segment in result.segments] == [1, 1, 1]
    assert [item.anomaly_type for item in result.anomalies] == [
        "NONCANONICAL_CLOSE_TIME",
        "MISSING_CANDLE_GAP",
    ]
    assert anomaly_ledger_payload(result) == anomaly_ledger_payload(result)
    assert quality_status(result) == "RESEARCH_DATA_QUALITY_INSUFFICIENT"


def test_clean_rows_remain_one_strict_segment() -> None:
    result = split_historical_rows(
        tuple(_row(index * 3_600_000) for index in range(30)),
        source="test",
        symbol="BTCUSDT",
        interval="1h",
    )
    assert len(result.segments) == 1
    assert not result.anomalies
    assert quality_status(result) == "RESEARCH_DATA_QUALITY_ACCEPTED"
