"""Deterministic, test-only persisted-cache fixtures for E1 executor tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from statistics import mean, median

from app.research_cache_identity import VerifiedResearchCacheIdentityV1


@dataclass(frozen=True)
class Pair:
    event_id: str
    match_id: str | None
    asset: str
    event_ms: int
    control_asset: str | None
    control_ms: int | None
    event_value: float | None
    control_value: float | None


@dataclass(frozen=True)
class Fixture:
    asset: str
    family: str
    metric: str
    pairs: tuple[Pair, ...]
    aligned_digest: str
    outcome_digest: str
    event_match_digest: str
    identity: VerifiedResearchCacheIdentityV1
    event_reads: int = 0
    match_reads: int = 0
    outcome_reads: int = 0

    @property
    def reads(self) -> int:
        return self.event_reads + self.match_reads + self.outcome_reads

    def read_events(self, asset: str, family: str, variant: str) -> tuple[Pair, ...]:
        object.__setattr__(self, "event_reads", self.event_reads + 1)
        by_event_id: dict[str, Pair] = {}
        for pair in self.pairs:
            by_event_id.setdefault(pair.event_id, pair)
        return tuple(by_event_id.values())

    def read_matches(self, event_ids: tuple[str, ...]) -> tuple[Pair, ...]:
        object.__setattr__(self, "match_reads", self.match_reads + 1)
        return tuple(pair for pair in self.pairs if pair.match_id is not None)

    def read_outcomes(self, pairs: tuple[Pair, ...], metric: str, horizon: str) -> tuple[Pair, ...]:
        object.__setattr__(self, "outcome_reads", self.outcome_reads + 1)
        return pairs


def _identity(asset: str, outcome: str, event_match: str) -> VerifiedResearchCacheIdentityV1:
    return VerifiedResearchCacheIdentityV1(
        asset,
        "aligned-" + asset,
        "outcomes/v1",
        "outcome-config",
        outcome,
        True,
        "bridge/v3",
        "event-config",
        event_match,
        True,
    )


def btc_fixture(*, mutated: bool = False) -> Fixture:
    values = ((100.0, 20.0), (200.0, 40.0), (-50.0, -10.0))
    if mutated:
        values = ((-100.0, 20.0), (-200.0, 40.0), (50.0, -10.0))
    pairs = tuple(
        Pair(
            f"btc-event-{i}",
            f"btc-match-{i}",
            "BTCUSDT",
            timestamp,
            "BTCUSDT",
            timestamp + 1,
            event,
            control,
        )
        for i, (timestamp, (event, control)) in enumerate(
            zip((1672531200000, 1704067200000, 1735689600000), values, strict=True), start=1
        )
    ) + (
        Pair("btc-unmatched", None, "BTCUSDT", 1704153600000, None, None, None, None),
        Pair(
            "btc-unavailable",
            "btc-unavailable",
            "BTCUSDT",
            1704240000000,
            "BTCUSDT",
            1704240000001,
            None,
            None,
        ),
    )
    outcome = "outcome-btc-mutated" if mutated else "outcome-btc-baseline"
    return Fixture(
        "BTCUSDT",
        "CROWDED_EXPANSION",
        "FORWARD_RETURN_BPS",
        pairs,
        "aligned-BTCUSDT",
        outcome,
        "event-match-btc",
        _identity("BTCUSDT", outcome, "event-match-btc"),
    )


def eth_touch_fixture() -> Fixture:
    pairs = (
        Pair(
            "eth-event-1",
            "eth-match-1",
            "ETHUSDT",
            1672531200000,
            "ETHUSDT",
            1672531200001,
            1.0,
            0.0,
        ),
        Pair(
            "eth-event-2",
            "eth-match-2",
            "ETHUSDT",
            1704067200000,
            "ETHUSDT",
            1704067200001,
            0.0,
            0.0,
        ),
        Pair(
            "eth-event-3",
            "eth-match-3",
            "ETHUSDT",
            1735689600000,
            "ETHUSDT",
            1735689600001,
            1.0,
            1.0,
        ),
    )
    return Fixture(
        "ETHUSDT",
        "CROWDED_STRESS",
        "UPSIDE_100_TOUCH",
        pairs,
        "aligned-ETHUSDT",
        "outcome-eth",
        "event-match-eth",
        _identity("ETHUSDT", "outcome-eth", "event-match-eth"),
    )


def invalid_cross_asset_fixture() -> Fixture:
    base = btc_fixture()
    return replace(base, pairs=(replace(base.pairs[0], control_asset="ETHUSDT"), *base.pairs[1:]))


def bps_oracle(fixture: Fixture) -> dict[str, object]:
    pairs = [
        item
        for item in fixture.pairs
        if item.event_value is not None and item.control_value is not None
    ]
    events = [item.event_value for item in pairs]
    controls = [item.control_value for item in pairs]
    by_year: dict[int, int] = {}
    for item in pairs:
        year = datetime.fromtimestamp(item.event_ms / 1000, UTC).year
        by_year[year] = by_year.get(year, 0) + 1
    return {
        "matched": len(pairs),
        "unmatched": 1,
        "unavailable": 1,
        "event_mean": mean(events),
        "event_median": median(events),
        "control_mean": mean(controls),
        "control_median": median(controls),
        "incremental_mean": (sum(events) - sum(controls)) / len(events),
        "incremental_median": median(events) - median(controls),
        "years": by_year,
    }
