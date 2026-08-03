from app.agent.trader import TraderAgent
from app.core.config import Settings


def test_agent_starts_and_stops() -> None:
    settings = Settings(trading_mode="paper")
    agent = TraderAgent(settings=settings)

    assert agent.is_running is False

    agent.start()

    assert agent.is_running is True

    agent.stop()

    assert agent.is_running is False