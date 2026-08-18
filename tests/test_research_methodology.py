from app.research_methodology import (
    METHODOLOGY_NAME,
    METHODOLOGY_VERSION,
    config_payload,
    methodology_digest,
)


def test_contract_is_stable_and_sensitive():
    assert (METHODOLOGY_NAME, METHODOLOGY_VERSION) == ("compact_matched_event_research", "1.0.0")
    assert methodology_digest() == methodology_digest()
    changed = config_payload()
    changed["local_window_radius"] = 25
    assert methodology_digest(changed) != methodology_digest()
