from app.agent.trader import TraderAgent
from app.core.config import Settings


class FakePollingEngine:
    def __init__(self) -> None:
        self.run_forever_calls = 0
        self.run_cycle_counts: list[int] = []
        self.stop_requests = 0

    def run_forever(self) -> None:
        self.run_forever_calls += 1

    def run_cycles(self, count: int) -> None:
        self.run_cycle_counts.append(count)

    def request_stop(self) -> None:
        self.stop_requests += 1


def test_agent_runs_continuously_and_stops() -> None:
    engine = FakePollingEngine()
    agent = TraderAgent(
        settings=Settings(trading_mode="paper"),
        polling_engine=engine,
    )

    agent.start()
    agent.stop()

    assert agent.is_running is False
    assert engine.run_forever_calls == 1
    assert engine.run_cycle_counts == []
    assert engine.stop_requests == 1


def test_agent_runs_configured_smoke_cycles() -> None:
    engine = FakePollingEngine()
    agent = TraderAgent(
        settings=Settings(smoke_run_cycles=3),
        polling_engine=engine,
    )

    agent.start()

    assert engine.run_forever_calls == 0
    assert engine.run_cycle_counts == [3]
