from app.platform.ollama.readiness import OllamaReadinessResultV1, OllamaReadinessStateV1
from app.platform.ollama.start import OllamaReadinessCoordinatorV1, OllamaRetryPolicyV1


class P:
    def __init__(self, s):
        self.s = iter(s)

    def probe(self):
        return OllamaReadinessResultV1(next(self.s), "http://localhost:11434")


def test_ready_does_not_start():
    calls = []
    assert (
        OllamaReadinessCoordinatorV1(P([OllamaReadinessStateV1.READY]), "ollama", calls.append)
        .ensure_ready()
        .state
        == OllamaReadinessStateV1.READY
    )
    assert not calls


def test_start_then_ready():
    calls = []
    c = OllamaReadinessCoordinatorV1(
        P(
            [
                OllamaReadinessStateV1.NOT_READY,
                OllamaReadinessStateV1.NOT_READY,
                OllamaReadinessStateV1.READY,
            ]
        ),
        "ollama",
        calls.append,
        policy=OllamaRetryPolicyV1(2, 0),
    )
    assert c.ensure_ready().state == OllamaReadinessStateV1.READY
    assert calls == [["ollama", "serve"]]


def test_start_failure_and_never_ready_are_bounded():
    def bad(argv):
        raise OSError()

    assert (
        OllamaReadinessCoordinatorV1(P([OllamaReadinessStateV1.NOT_READY]), "ollama", bad)
        .ensure_ready()
        .error_category
        == "START_FAILED"
    )
    calls = []
    c = OllamaReadinessCoordinatorV1(
        P([OllamaReadinessStateV1.NOT_READY] * 3),
        "ollama",
        calls.append,
        policy=OllamaRetryPolicyV1(2, 0),
    )
    assert c.ensure_ready().error_category == "START_TIMEOUT"
    assert len(calls) == 1


def test_initial_error_fails_closed():
    assert (
        OllamaReadinessCoordinatorV1(P([OllamaReadinessStateV1.ERROR]), "ollama", lambda _: None)
        .ensure_ready()
        .state
        == OllamaReadinessStateV1.ERROR
    )
