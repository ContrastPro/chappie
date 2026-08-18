from app.market_intelligence_universe import (
    ASSETS,
    GROUPS,
    MEMBERS,
    DerivativesPoint,
    PricePoint,
    build_derivatives_context,
    build_universe_observations,
    transition_matrix,
    universe_digest,
    universe_payload,
)


def _points(asset_offset: float, *, segment: int = 0):
    return tuple(
        PricePoint(index * 300_000, 100 + asset_offset + index, segment) for index in range(290)
    )


def test_universe_contract_membership_groups_and_digest_are_stable():
    assert ASSETS == ("BTC", "ETH", "SOL", "BNB", "AVAX", "XRP", "LINK", "DOGE")
    assert GROUPS == {member.asset: member.group for member in MEMBERS}
    assert universe_digest() == universe_digest()
    changed = universe_payload()
    changed["primary_benchmark"] = "ETH"
    assert universe_digest(changed) != universe_digest()


def test_exact_timestamp_relative_strength_breadth_and_missing_secondary_behavior():
    observations = build_universe_observations(
        {"BTC": _points(0), "ETH": _points(10), "SOL": _points(20)}
    )
    last = observations[-1]
    assert last.available_assets == ("BTC", "ETH", "SOL")
    assert last.ratios_btc["ETH"] == (100 + 10 + 289) / (100 + 289)
    assert last.breadth_denominator_6h == 3
    assert "DOGE" not in last.returns_6h_bps
    assert "DOGE" not in last.ratios_btc
    assert last.breadth_positive_6h <= last.breadth_denominator_6h
    assert "HIGH_BETA_L1" in last.group_median_6h_bps


def test_prelisting_missing_and_segment_boundary_do_not_forward_fill_returns():
    btc = _points(0)
    eth = tuple(
        PricePoint(index * 300_000, 110 + index, 0 if index < 150 else 1)
        for index in range(100, 290)
    )
    observations = build_universe_observations({"BTC": btc, "ETH": eth})
    by_time = {item.timestamp_ms: item for item in observations}
    # ETH is absent before listing and does not contribute to denominator.
    assert by_time[120 * 300_000].breadth_denominator_6h == 1
    # At the explicit segment boundary it has no six-hour return from segment 0.
    assert "ETH" not in by_time[221 * 300_000].returns_6h_bps
    assert "ETH" in by_time[222 * 300_000].returns_6h_bps


def test_repeated_build_is_deterministic_and_rotation_is_descriptive():
    series = {"BTC": _points(0), "ETH": _points(10), "DOGE": _points(30)}
    first = build_universe_observations(series)
    assert first == build_universe_observations(series)
    assert first[-1].leadership in {
        "BTC_DOMINANT",
        "ETH_ROTATION",
        "HIGH_BETA_ROTATION",
        "SPECULATIVE_ROTATION",
        "BROAD_RISK_ON",
        "BROAD_RISK_OFF",
        "MIXED",
    }
    assert transition_matrix(first)


def test_derivatives_context_is_exact_timestamp_and_coverage_aware():
    context = build_derivatives_context(
        {
            "BTC": (
                DerivativesPoint(0, 1, 0.001, "FUTURES_BUYING", "LONG_BIASED"),
                DerivativesPoint(300_000, -1, -0.001, "FUTURES_SELLING", "EXTREME_LONG"),
            ),
            "ETH": (DerivativesPoint(0, 2, 0.002, "FUTURES_BUYING", "NEUTRAL"),),
            "SOL": (DerivativesPoint(0, None, None, None, None),),
            # BNB has no source observation and must not count as negative.
        }
    )
    first, second = context
    assert first.available_assets == ("BTC", "ETH", "SOL")
    assert first.core_available == 2 and first.high_beta_available == 1
    assert first.oi_expansion_fraction == 1
    assert first.funding_positive_fraction == 1
    assert first.futures_buy_fraction == 1
    assert first.leverage_state == "LEVERAGE_EXPANDING"
    assert first.funding_state == "FUNDING_POSITIVE_BROAD"
    assert first.futures_flow_state == "FUTURES_BUY_PRESSURE"
    assert second.available_assets == ("BTC",)
    assert second.oi_expansion_fraction == 0
    assert second.funding_positive_fraction == 0
