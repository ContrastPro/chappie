from app.agent.trader import TraderAgent
from app.core.config import Settings
from tests.fakes import FakeMarketProvider


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
