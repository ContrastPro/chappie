from __future__ import annotations

from decimal import Decimal

from app.market.models import Candle
from app.research.intelligence.capital_rotation import (
    ASSETS,
    RELATIVE_PAIRS,
    capital_flow_proxy,
    direct_pair_cross_check,
    funding_conditioned_outcomes,
    leadership_outcomes,
    rotation_observations,
    transition_matrix,
)
from app.research.intelligence.market import ContextDataset, ContextObservation

HOUR = 3_600_000


def _candle(index: int, close: str, volume: str = "10") -> Candle:
    value = Decimal(close)
    return Candle(
        open_time_ms=index * HOUR,
        close_time_ms=(index + 1) * HOUR - 1,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal(volume),
    )


def _spot(*, gap: bool = False) -> dict[str, tuple[Candle, ...]]:
    # ETH leads BTC over each two-hour lookback; data stays entirely causal.
    values = {
        "BTC": ("100", "101", "102", "103", "104", "105", "106", "107"),
        "ETH": ("100", "103", "106", "109", "112", "115", "118", "121"),
        "SOL": ("100", "100", "101", "101", "102", "102", "103", "103"),
        "BNB": ("100", "99", "98", "97", "96", "95", "94", "93"),
    }
    return {
        asset: tuple(
            _candle(index + (1 if gap and asset == "SOL" and index >= 3 else 0), close)
            for index, close in enumerate(closes)
        )
        for asset, closes in values.items()
    }


def test_relative_strength_breadth_and_rotation_label_are_completed_candle_causal() -> None:
    observations = rotation_observations(_spot(), interval_ms=HOUR, lookback_bars=2)
    item = observations[0]
    assert item.timestamp_ms == 3 * HOUR - 1
    assert item.leader == "ETH"
    assert item.state == "ETH_LEADERSHIP"
    assert item.relative_returns_bps["BTC"] == 0
    assert item.breadth_positive == 3
    assert item.breadth_outperforming_btc == 1
    assert capital_flow_proxy(item)["meaning"].startswith("probable observable")


def test_alignment_excludes_cross_gap_rolling_state() -> None:
    # Strict segments preserve valid observations on either side, but nothing
    # may combine candles across the missing SOL timestamp.
    observations = rotation_observations(_spot(gap=True), interval_ms=HOUR, lookback_bars=2)
    assert observations
    assert [item.timestamp_ms for item in observations] == [
        3 * HOUR - 1,
        7 * HOUR - 1,
        8 * HOUR - 1,
    ]


def test_transition_matrix_and_leadership_outcomes_use_future_only_as_labels() -> None:
    observations = rotation_observations(_spot(), interval_ms=HOUR, lookback_bars=1)
    matrix = transition_matrix(observations, bars=1)
    probabilities = matrix["ETH_LEADERSHIP"]["probabilities"]  # type: ignore[index]
    assert sum(
        (Decimal(value) for value in probabilities.values() if value is not None), Decimal(0)
    ) == Decimal(1)  # type: ignore[union-attr]
    outcomes = leadership_outcomes(observations, bars=1)
    assert outcomes["ETH"]["leader_samples"] >= 1  # type: ignore[index]
    assert outcomes["ETH"]["leader_condition"]["count"] == outcomes["ETH"]["leader_samples"]  # type: ignore[index]


def test_direct_pair_cross_check_compares_derived_relative_return_without_future_data() -> None:
    spot = _spot()
    # ETHBTC follows the exact ratio of ETHUSDT/BTCUSDT, so deviation is zero.
    ethbtc = tuple(
        _candle(index, str(Decimal(eth.close) / Decimal(btc.close)))
        for index, (eth, btc) in enumerate(zip(spot["ETH"], spot["BTC"], strict=True))
    )
    result = direct_pair_cross_check(spot, {"ETHBTC": ethbtc}, interval_ms=HOUR, bars=1)
    assert result["ETHBTC"]["samples"] == len(ethbtc) - 1
    assert Decimal(result["ETHBTC"]["max_absolute_difference_bps"]) == 0  # type: ignore[arg-type]
    assert set(RELATIVE_PAIRS).issuperset({"ETH"})


def test_all_assets_are_required_for_comparable_breadth() -> None:
    incomplete = _spot()
    incomplete.pop("BNB")
    try:
        rotation_observations(incomplete, interval_ms=HOUR)
    except ValueError as error:
        assert "BTC, ETH, SOL, and BNB" in str(error)
    else:  # pragma: no cover - makes intended fail-closed contract explicit
        raise AssertionError("missing universe must fail closed")
    assert ASSETS == ("BTC", "ETH", "SOL", "BNB")


def test_funding_conditioning_is_asof_bounded_and_never_uses_future_publication() -> None:
    observations = rotation_observations(_spot(), interval_ms=HOUR, lookback_bars=1)
    funding = ContextDataset(
        "funding_rate",
        "BTCUSDT",
        "8h",
        "fixture",
        (
            ContextObservation(
                observations[-1].timestamp_ms + 1,
                observations[-1].timestamp_ms + 1,
                Decimal("0.001"),
                "fixture",
            ),
        ),
    )
    # The only observation is published after every eligible early feature;
    # there is no pre-publication forward read.
    assert funding_conditioned_outcomes(observations, funding, bars=1) == {}
