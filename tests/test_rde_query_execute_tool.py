"""Research CLI validates typed artifacts without a storage adapter."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.ai.rde_capabilities import ExistingEventResearchQueryV1
from app.tools.rde_query_execute import main
from tests.rde_executor_fixture_support import btc_fixture


def _write_query(path: Path) -> None:
    fixture = btc_fixture()
    query = ExistingEventResearchQueryV1(
        "btc-fixture-query",
        fixture.asset,
        fixture.family,
        "BASE",
        fixture.metric,
        "6h",
        "GREATER_THAN_MATCHED_CONTROL",
        "MATCHED_CONTROL",
        fixture.identity.research_cache_identity_digest,
        fixture.event_match_digest,
        fixture.outcome_digest,
    )
    path.write_text(json.dumps(asdict(query)), encoding="utf-8")


def test_dry_run_with_unverified_artifact_fails_machine_readably(tmp_path: Path, capsys) -> None:
    query = tmp_path / "query.json"
    _write_query(query)
    assert main(["--query", str(query), "--dry-run", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "VERIFIED_CACHE_IDENTITY_INVALID"
    assert payload["capability"] == "EXECUTABLE_EXISTING_CACHE"
    assert payload["executable"] is True


def test_invalid_artifacts_fail_closed(tmp_path: Path, capsys) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    assert main(["--query", str(invalid), "--dry-run", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "QUERY_ARTIFACT_INVALID"
