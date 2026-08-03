from decimal import Decimal

from app.agent.trader import TraderAgent
from app.core.config import Settings
from app.market.models import ServerTime, TickerPrice


class FakeMarketProvider:
    def get_server_time(self) -> ServerTime:
        return ServerTime(timestamp_ms=1_700_000_000_000)

    def get_ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(
            symbol=symbol,
            price=Decimal("50000.00"),
        )

    def close(self) -> None:
        pass


def test_agent_starts_and_stops() -> None:
    settings = Settings(trading_mode="paper")
    market_provider = FakeMarketProvider()

    agent = TraderAgent(
        settings=settings,
        market_provider=market_provider,
    )

    assert agent.is_running is False

    agent.start()

    assert agent.is_running is True

    agent.stop()

    assert agent.is_running is False