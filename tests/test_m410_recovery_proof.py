import ast
import hashlib
import json
import shutil
import sqlite3
import threading
import time
from decimal import ROUND_DOWN, localcontext
from pathlib import Path

import pytest

from app.persistence.canonical import canonical_json, canonical_sha256
from app.tools import testnet_m410_recovery_proof as proof

PRODUCTION = Path("data/m4_10_testnet_smoke.sqlite3")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def resolved_database(tmp_path: Path) -> Path:
    assert _sha(PRODUCTION) == proof.EXPECTED_DATABASE_SHA256
    target = tmp_path / "resolved.sqlite3"
    shutil.copyfile(PRODUCTION, target)
    return target


def _verify(path: Path):
    return proof.verify_database(
        path, expected_sha256=_sha(path), run_id="offline-proof", observed_at_ms=1
    )


def test_resolved_production_shape_passes_without_mutation(resolved_database: Path) -> None:
    before = _sha(resolved_database)
    first = _verify(resolved_database)
    second = _verify(resolved_database)

    assert first.outcome is proof.RecoveryProofOutcome.PASS
    assert second.command_key == first.command_key
    assert first.managed_btc == proof.EXPECTED_BASE
    assert first.managed_usdt == proof.EXPECTED_USDT
    assert first.turnover == proof.EXPECTED_QUOTE
    assert _sha(resolved_database) == before
    assert not resolved_database.with_name(resolved_database.name + "-wal").exists()
    assert not resolved_database.with_name(resolved_database.name + "-shm").exists()


def test_authorization_is_checked_before_any_path_inspection(monkeypatch, tmp_path: Path) -> None:
    class ExplodingPath(type(Path())):
        def exists(self):
            raise AssertionError("path inspected before authorization")

    path = ExplodingPath(tmp_path / "never")
    with pytest.raises(proof.RecoveryProofBlocked, match="authorization"):
        proof.validate_startup({}, database=path, evidence=path)


def test_authorized_startup_requires_database(tmp_path: Path) -> None:
    with pytest.raises(proof.RecoveryProofBlocked, match="database is absent"):
        proof.validate_startup(
            {proof.AUTHORIZATION_ENV: proof.AUTHORIZATION_VALUE},
            database=tmp_path / "missing.sqlite3",
            evidence=tmp_path / "proof.log",
        )


def test_authorized_startup_accepts_clean_paths(resolved_database: Path, tmp_path: Path) -> None:
    proof.validate_startup(
        {proof.AUTHORIZATION_ENV: proof.AUTHORIZATION_VALUE},
        database=resolved_database,
        evidence=tmp_path / "proof.log",
    )


def test_malformed_portfolio_payload_is_typed() -> None:
    with pytest.raises(proof.RecoveryProofFailed, match="Portfolio payload"):
        proof._portfolio_state("{", "0" * 64)


@pytest.mark.parametrize(
    "environment",
    [
        {"M4_10_PHASE3_AUTHORIZED": "YES"},
        {"M4_10_PHASE4_AUTHORIZED": "YES"},
        {"M4_10_PHASE4_REMEDIATION_AUTHORIZED": "YES"},
        {proof.AUTHORIZATION_ENV: "yes"},
    ],
)
def test_other_authorizations_cannot_enable_proof(environment, tmp_path: Path) -> None:
    with pytest.raises(proof.RecoveryProofBlocked, match="authorization"):
        proof.validate_startup(environment, database=tmp_path, evidence=tmp_path / "evidence")


def test_evidence_collision_blocks_before_database_open(
    monkeypatch, resolved_database: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "proof.log"
    evidence.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("DB opened"))
    with pytest.raises(proof.RecoveryProofBlocked, match="already exists"):
        proof.validate_startup(
            {proof.AUTHORIZATION_ENV: proof.AUTHORIZATION_VALUE},
            database=resolved_database,
            evidence=evidence,
        )


def test_wrong_authorized_checksum_blocks_before_sqlite_open(
    monkeypatch, resolved_database: Path
) -> None:
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("DB opened"))
    with pytest.raises(proof.RecoveryProofBlocked, match="checksum"):
        proof.verify_database(
            resolved_database, expected_sha256="0" * 64, run_id="x", observed_at_ms=0
        )


@pytest.mark.parametrize(
    ("table", "trigger", "mutation"),
    [
        (
            "exchange_smoke_authority_events",
            "exchange_smoke_authority_events_no_update",
            "UPDATE exchange_smoke_authority_events SET payload_digest='" + "0" * 64 + "'",
        ),
        (
            "exchange_order_events",
            "exchange_order_events_no_delete",
            "DELETE FROM exchange_order_events WHERE order_version=1",
        ),
        (
            "exchange_trades",
            "exchange_trades_no_update",
            "UPDATE exchange_trades SET payload_digest='" + "0" * 64 + "'",
        ),
        (
            "exchange_trade_settlements",
            "exchange_trade_settlements_no_update",
            "UPDATE exchange_trade_settlements SET record_digest='" + "0" * 64 + "'",
        ),
        (
            "exchange_trade_settlement_resolutions",
            "exchange_trade_settlement_resolutions_no_update",
            "UPDATE exchange_trade_settlement_resolutions SET record_digest='" + "0" * 64 + "'",
        ),
    ],
)
def test_corrupt_append_only_truth_fails_closed(
    resolved_database: Path, table: str, trigger: str, mutation: str
) -> None:
    connection = sqlite3.connect(resolved_database)
    connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(mutation)
    connection.commit()
    connection.close()

    with pytest.raises(proof.RecoveryProofFailed):
        _verify(resolved_database)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (
            ["DELETE FROM schema_migrations WHERE version=6"],
            "schema v6",
        ),
        (
            ["UPDATE exchange_execution_roots SET state_digest='" + "0" * 64 + "'"],
            "root digest",
        ),
        (
            [
                "DROP TRIGGER exchange_smoke_authority_events_no_update",
                "UPDATE exchange_smoke_authority_events SET payload='{}'",
            ],
            "payload",
        ),
        (
            ["UPDATE exchange_submission_attempts SET payload_digest='" + "0" * 64 + "'"],
            "attempt digest",
        ),
        (
            [
                "DROP TRIGGER exchange_trades_no_update",
                "UPDATE exchange_trades SET payload='{'",
            ],
            "trade payload",
        ),
        (
            [
                "DROP TRIGGER exchange_reconciliation_coverage_no_delete",
                "DELETE FROM exchange_reconciliation_coverage",
            ],
            "coverage is absent",
        ),
        (
            [
                "DROP TRIGGER exchange_reconciliation_runs_no_delete",
                "DELETE FROM exchange_reconciliation_runs",
            ],
            "run is absent",
        ),
        (
            ["UPDATE exchange_orders SET session_id='wrong'"],
            "identity lineage",
        ),
    ],
)
def test_independent_digest_and_lineage_boundaries_fail_closed(
    resolved_database: Path, setup: list[str], expected: str
) -> None:
    connection = sqlite3.connect(resolved_database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        for statement in setup:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(proof.RecoveryProofFailed, match=expected):
        _verify(resolved_database)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE exchange_orders SET cumulative_quote='10'",
        "UPDATE exchange_sessions SET executed_turnover='0'",
        "UPDATE exchange_portfolio_current SET state_digest='" + "0" * 64 + "'",
        "UPDATE exchange_reconciliation_current SET status='REQUIRED'",
        "UPDATE exchange_control_state SET kill_switch_state='CLEARED'",
        "DELETE FROM exchange_submission_attempts",
    ],
)
def test_projection_count_and_economic_corruption_fails_closed(
    resolved_database: Path, mutation: str
) -> None:
    connection = sqlite3.connect(resolved_database)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    with pytest.raises(proof.RecoveryProofFailed):
        _verify(resolved_database)


@pytest.mark.parametrize("crash_boundary", ["after_open", "after_validation"])
def test_base_exception_closes_read_only_connection(
    resolved_database: Path, crash_boundary: str
) -> None:
    class Crash(BaseException):
        pass

    with pytest.raises(Crash):
        proof.verify_database(
            resolved_database,
            expected_sha256=_sha(resolved_database),
            run_id="crash",
            observed_at_ms=0,
            failure_injector=lambda boundary: (
                (_ for _ in ()).throw(Crash(boundary)) if boundary == crash_boundary else None
            ),
        )
    connection = sqlite3.connect(resolved_database)
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
    connection.close()


def test_evidence_is_exclusive_sanitized_and_pass_is_last(
    resolved_database: Path, tmp_path: Path
) -> None:
    result = _verify(resolved_database)
    evidence = tmp_path / "proof.log"
    proof.write_evidence(evidence, result)
    text = evidence.read_text(encoding="utf-8")

    assert text.endswith("outcome=PASS\n")
    assert "secret" not in text.lower()
    assert "api_key" not in text.lower()
    with pytest.raises(proof.RecoveryProofBlocked, match="already exists"):
        proof.write_evidence(evidence, result)


def test_sqlite_error_and_sidecar_fail_closed(resolved_database: Path, tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not sqlite")
    with pytest.raises(proof.RecoveryProofFailed, match="database validation"):
        _verify(invalid)

    sidecar = resolved_database.with_name(resolved_database.name + "-wal")
    sidecar.touch()
    with pytest.raises(proof.RecoveryProofBlocked, match="WAL/SHM"):
        _verify(resolved_database)


def test_sidecar_blocks_before_checksum_or_sqlite_open(
    monkeypatch, resolved_database: Path
) -> None:
    resolved_database.with_name(resolved_database.name + "-shm").touch()
    monkeypatch.setattr(proof, "_sha256", lambda path: pytest.fail("checksum read"))
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("SQLite opened"))
    with pytest.raises(proof.RecoveryProofBlocked, match="WAL/SHM"):
        proof.verify_database(
            resolved_database,
            expected_sha256=proof.EXPECTED_DATABASE_SHA256,
            run_id="blocked",
            observed_at_ms=0,
        )


def test_open_uses_immutable_read_only_uri_and_query_only(
    monkeypatch, resolved_database: Path
) -> None:
    real_connect = sqlite3.connect
    observed: dict[str, object] = {}

    def connect(database, *args, **kwargs):
        observed["database"] = database
        observed["uri"] = kwargs.get("uri")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    _verify(resolved_database)
    assert "mode=ro" in observed["database"]
    assert "immutable=1" in observed["database"]
    assert observed["uri"] is True


def _rewrite_payload(
    database: Path, table: str, payload_column: str, digest_column: str, algorithm: str, change
) -> None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(f"SELECT * FROM {table}").fetchone()
        payload = json.loads(row[payload_column])
        change(payload)
        encoded = canonical_json(payload)
        digest = canonical_sha256(payload, algorithm_version=algorithm)
        connection.execute(
            f"UPDATE {table} SET {payload_column}=?,{digest_column}=?", (encoded, digest)
        )
        connection.commit()
    finally:
        connection.close()


def test_consistent_but_false_payloads_fail_lineage_checks(resolved_database: Path) -> None:
    connection = sqlite3.connect(resolved_database)
    connection.execute("DROP TRIGGER exchange_smoke_authority_events_no_update")
    connection.commit()
    connection.close()
    _rewrite_payload(
        resolved_database,
        "exchange_smoke_authority_events",
        "payload",
        "payload_digest",
        "phase3-authority-event/v1",
        lambda payload: payload.update(policy_version="wrong"),
    )
    with pytest.raises(proof.RecoveryProofFailed, match="policy lineage"):
        _verify(resolved_database)


def test_consistently_redigested_authority_slot_fails_closed(resolved_database: Path) -> None:
    connection = sqlite3.connect(resolved_database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("DROP TRIGGER exchange_smoke_authorities_no_update")
        authority = connection.execute("SELECT * FROM exchange_smoke_authorities").fetchone()
        payload = json.loads(authority["payload"])
        payload["economic_slot_key"] = "f" * 64
        encoded = canonical_json(payload)
        digest = canonical_sha256(payload, algorithm_version=authority["policy_version"])
        connection.execute(
            "UPDATE exchange_smoke_authorities SET economic_slot_key=?,payload=?,payload_digest=?",
            ("f" * 64, encoded, digest),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(proof.RecoveryProofFailed, match="authority/audit chain"):
        _verify(resolved_database)


def test_consistently_redigested_event_chronology_fails_closed(resolved_database: Path) -> None:
    connection = sqlite3.connect(resolved_database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("DROP TRIGGER exchange_order_events_no_update")
        row = connection.execute(
            "SELECT * FROM exchange_order_events WHERE order_version=2"
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["occurred_at_ms"] = 0
        encoded = canonical_json(payload)
        digest = canonical_sha256(payload, algorithm_version="exchange-order-event/v1")
        connection.execute(
            "UPDATE exchange_order_events SET occurred_at_ms=0,payload=?,payload_digest=? "
            "WHERE order_version=2",
            (encoded, digest),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(proof.RecoveryProofFailed, match="chronology"):
        _verify(resolved_database)


def test_synthetic_duplicate_event_rows_are_never_implicitly_selected(
    resolved_database: Path,
) -> None:
    connection = sqlite3.connect(resolved_database)
    try:
        row = connection.execute(
            "SELECT * FROM exchange_order_events WHERE order_version=4"
        ).fetchone()
        values = list(row)
        values[0] = None
        values[1] = "f" * 64
        values[3] = 5
        connection.execute(
            "INSERT INTO exchange_order_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(proof.RecoveryProofFailed, match="event chain"):
        _verify(resolved_database)


def test_envelope_command_and_resolution_conflicts_fail_closed(resolved_database: Path) -> None:
    connection = sqlite3.connect(resolved_database)
    try:
        connection.execute("DROP TRIGGER exchange_smoke_authorities_no_update")
        connection.execute(
            "UPDATE exchange_smoke_authorities SET risk_envelope_digest='" + "0" * 64 + "'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(proof.RecoveryProofFailed, match="canonical payload"):
        _verify(resolved_database)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (["UPDATE exchange_orders SET command_payload=command_payload || ' '"], "not canonical"),
        (
            [
                "DROP TRIGGER exchange_trade_settlement_resolutions_no_update",
                (
                    "UPDATE exchange_trade_settlement_resolutions SET record_payload='{}',"
                    "record_digest='387313d11aceb6f97309a2c48d79b2b58e99677633977187f27c6ffaa2c906a9'"
                ),
            ],
            "resolution payload",
        ),
        (
            [
                "DROP TRIGGER exchange_reconciliation_runs_no_update",
                "UPDATE exchange_reconciliation_runs SET result_payload='{'",
            ],
            "result payload",
        ),
        (
            [
                "DROP TRIGGER exchange_reconciliation_runs_no_update",
                "UPDATE exchange_reconciliation_runs SET result_digest='" + "0" * 64 + "'",
            ],
            "run digest",
        ),
    ],
)
def test_canonical_command_resolution_and_run_conflicts_fail_closed(
    resolved_database: Path, setup: list[str], expected: str
) -> None:
    connection = sqlite3.connect(resolved_database)
    try:
        for statement in setup:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(proof.RecoveryProofFailed, match=expected):
        _verify(resolved_database)


def test_checksum_change_during_observation_fails_closed(resolved_database: Path) -> None:
    def mutate(boundary: str) -> None:
        if boundary == "after_validation":
            with resolved_database.open("ab") as target:
                target.write(b"changed")

    with pytest.raises(proof.RecoveryProofFailed, match="changed during"):
        proof.verify_database(
            resolved_database,
            expected_sha256=_sha(resolved_database),
            run_id="mutated",
            observed_at_ms=0,
            failure_injector=mutate,
        )


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (
            ["UPDATE schema_migrations SET checksum='" + "0" * 64 + "' WHERE version=6"],
            "schema v6",
        ),
        (
            ["UPDATE exchange_orders SET client_order_id='ctt1t-aaaaaaaaaaaaaaaaaaaaaaaaaa'"],
            "command",
        ),
        (["UPDATE exchange_orders SET exchange_order_id='other'"], "order state"),
        (["UPDATE exchange_orders SET status='ACKNOWLEDGED'"], "order state"),
        (
            [
                "DROP TRIGGER exchange_trades_no_update",
                "UPDATE exchange_trades SET commission='0.00001'",
            ],
            "trade payload",
        ),
        (
            [
                "DROP TRIGGER exchange_trade_settlement_resolutions_no_update",
                "PRAGMA ignore_check_constraints=ON",
                "UPDATE exchange_trade_settlement_resolutions SET policy_id='wrong'",
            ],
            "resolution payload",
        ),
        (["UPDATE exchange_portfolio_current SET portfolio_version=2"], "Portfolio"),
        (["UPDATE exchange_sessions SET generation=3"], "globally synchronized"),
        (["UPDATE exchange_reconciliation_current SET generation=3"], "durable identity"),
        (
            [
                "DROP TRIGGER exchange_trade_settlement_resolutions_no_delete",
                "DELETE FROM exchange_trade_settlement_resolutions",
            ],
            "exactly one row",
        ),
    ],
)
def test_final_corruption_matrix_fails_closed(
    resolved_database: Path, setup: list[str], expected: str
) -> None:
    connection = sqlite3.connect(resolved_database)
    try:
        for statement in setup:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(proof.RecoveryProofFailed, match=expected):
        _verify(resolved_database)


def test_decimal_context_and_signed_zero_do_not_change_result(resolved_database: Path) -> None:
    baseline = _verify(resolved_database)
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_DOWN
        constrained = _verify(resolved_database)
    assert constrained == baseline
    evidence = constrained.render()
    assert "fees=0\n" in evidence
    assert "fees=-0" not in evidence


@pytest.mark.parametrize(
    "exception_type",
    [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit],
)
@pytest.mark.parametrize(
    "boundary",
    [
        "before_checksum_before",
        "after_checksum_before",
        "before_open",
        "after_open",
        "before_validation",
        "after_cardinality_queries",
        "after_authority_queries",
        "after_order_queries",
        "after_trade_queries",
        "after_settlement_queries",
        "after_portfolio_queries",
        "after_reconciliation_queries",
        "after_economic_validation",
        "after_validation",
        "before_close",
        "after_close",
        "before_checksum_after",
        "after_checksum_after",
    ],
)
def test_all_verifier_baseexceptions_leave_no_sidecars(
    resolved_database: Path, exception_type: type[BaseException], boundary: str
) -> None:
    def crash(observed: str) -> None:
        if observed == boundary:
            raise exception_type(boundary)

    with pytest.raises(exception_type):
        proof.verify_database(
            resolved_database,
            expected_sha256=_sha(resolved_database),
            run_id="crash",
            observed_at_ms=0,
            failure_injector=crash,
        )
    assert not resolved_database.with_name(resolved_database.name + "-wal").exists()
    assert not resolved_database.with_name(resolved_database.name + "-shm").exists()


@pytest.mark.parametrize(
    "exception_type",
    [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit],
)
@pytest.mark.parametrize(
    "boundary",
    [
        "before_evidence_open",
        "before_evidence_write",
        "after_evidence_write",
        "before_evidence_fsync",
        "after_evidence_fsync",
        "before_evidence_publish",
    ],
)
def test_evidence_baseexception_never_publishes_pass(
    resolved_database: Path,
    tmp_path: Path,
    boundary: str,
    exception_type: type[BaseException],
) -> None:
    result = _verify(resolved_database)
    evidence = tmp_path / "proof.log"

    def crash(observed: str) -> None:
        if observed == boundary:
            raise exception_type(boundary)

    with pytest.raises(exception_type):
        proof.write_evidence(evidence, result, failure_injector=crash)
    assert not evidence.exists()
    assert not tuple(tmp_path.glob("*.tmp"))


def test_evidence_publish_race_has_exactly_one_valid_winner(
    resolved_database: Path, tmp_path: Path
) -> None:
    result = _verify(resolved_database)
    evidence = tmp_path / "proof.log"
    outcomes: list[str] = []

    def actor() -> None:
        try:
            proof.write_evidence(evidence, result)
            outcomes.append("PASS")
        except proof.RecoveryProofBlocked:
            outcomes.append("BLOCKED")

    actors = [threading.Thread(target=actor) for _ in range(2)]
    for actor_thread in actors:
        actor_thread.start()
    for actor_thread in actors:
        actor_thread.join()
    assert sorted(outcomes) == ["BLOCKED", "PASS"]
    assert evidence.read_text(encoding="utf-8").endswith("outcome=PASS\n")
    assert not tuple(tmp_path.glob("*.tmp"))


def test_short_evidence_write_cannot_publish_pass(
    monkeypatch, resolved_database: Path, tmp_path: Path
) -> None:
    result = _verify(resolved_database)
    evidence = tmp_path / "proof.log"

    def short_write(target, payload: str) -> int:
        shortened = payload[: -len("outcome=PASS\n")]
        target.write(shortened)
        return len(shortened)

    monkeypatch.setattr(proof, "_write_payload", short_write)
    with pytest.raises(proof.RecoveryProofFailed, match="incomplete"):
        proof.write_evidence(evidence, result)
    assert not evidence.exists()
    assert not tuple(tmp_path.glob("*.tmp"))


def test_checksum_baseexceptions_never_leave_open_resource(
    monkeypatch, resolved_database: Path
) -> None:
    original = proof._sha256
    calls = 0

    def crash_after(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GeneratorExit("checksum-after")
        return original(path)

    monkeypatch.setattr(proof, "_sha256", crash_after)
    with pytest.raises(GeneratorExit):
        proof.verify_database(
            resolved_database,
            expected_sha256=original(resolved_database),
            run_id="checksum-crash",
            observed_at_ms=0,
        )
    connection = sqlite3.connect(resolved_database)
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
    connection.close()


@pytest.mark.parametrize(
    "exception_type", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_exception_during_sqlite_open_acquires_no_resource(
    monkeypatch, resolved_database: Path, exception_type: type[BaseException]
) -> None:
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(exception_type("open")),
    )
    with pytest.raises(exception_type):
        _verify(resolved_database)
    assert not resolved_database.with_name(resolved_database.name + "-wal").exists()


@pytest.mark.parametrize(
    "exception_type", [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_exception_during_close_occurs_after_real_close(
    monkeypatch, resolved_database: Path, exception_type: type[BaseException]
) -> None:
    original = proof._close_readonly

    def close_then_crash(connection) -> None:
        original(connection)
        raise exception_type("close")

    monkeypatch.setattr(proof, "_close_readonly", close_then_crash)
    with pytest.raises(exception_type):
        _verify(resolved_database)
    connection = sqlite3.connect(resolved_database)
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
    connection.close()


def test_representative_proof_is_bounded_and_repeatable(resolved_database: Path) -> None:
    started = time.monotonic()
    results = [_verify(resolved_database) for _ in range(20)]
    elapsed = time.monotonic() - started
    assert all(result == results[0] for result in results)
    assert elapsed < 2
    assert not resolved_database.with_name(resolved_database.name + "-wal").exists()


def test_malformed_shared_payload_helper_is_typed(resolved_database: Path) -> None:
    connection = sqlite3.connect(resolved_database)
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM exchange_smoke_authorities").fetchone()
    connection.close()
    values = dict(row)
    values["payload"] = "{"

    class Row(dict):
        pass

    with pytest.raises(proof.RecoveryProofFailed, match="invalid payload"):
        proof._json_payload(Row(values), "payload", "payload_digest", values["policy_version"])


def test_main_outcomes_are_typed_and_never_fall_through(monkeypatch, capsys) -> None:
    result = _verify(PRODUCTION)
    monkeypatch.setattr(
        proof,
        "validate_startup",
        lambda *args, **kwargs: (_ for _ in ()).throw(proof.RecoveryProofBlocked("no")),
    )
    assert proof.main() == 2
    assert "BLOCKED" in capsys.readouterr().err

    monkeypatch.setattr(proof, "validate_startup", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        proof,
        "verify_database",
        lambda *args, **kwargs: (_ for _ in ()).throw(proof.RecoveryProofFailed("bad")),
    )
    assert proof.main() == 1
    assert "FAILED" in capsys.readouterr().err

    monkeypatch.setattr(proof, "verify_database", lambda *args, **kwargs: result)
    monkeypatch.setattr(proof, "write_evidence", lambda *args, **kwargs: None)
    assert proof.main() == 0
    assert "outcome=PASS" in capsys.readouterr().out


def test_module_has_no_transport_or_mutating_runtime_dependencies() -> None:
    source = Path(proof.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "httpx",
        "socket",
        "app.main",
        "app.binance",
        "app.exchange_runtime",
        "app.exchange_execution.phase3",
        "app.exchange_execution.durable",
        "app.exchange_execution.factories",
        "app.persistence.sqlite",
        "app.tools.testnet_phase3",
        "app.tools.testnet_phase4",
        "app.tools.testnet_phase4_remediate_zero_fee",
    }
    assert imports.isdisjoint(forbidden)
    assert "/api/v3/order" not in source
    assert "submit_market_order" not in source
    assert "test_market_order" not in source
    assert "settle_pending" not in source
    assert "resolve_zero_fee" not in source
    assert "BINANCE" not in source
    for mutating_keyword in (
        'execute("INSERT ',
        'execute("UPDATE ',
        'execute("DELETE ',
        'execute("BEGIN ',
        'execute("CREATE ',
        'execute("ALTER ',
        'execute("DROP ',
        'execute("VACUUM',
        'execute("REINDEX',
        'execute("ATTACH ',
    ):
        assert mutating_keyword not in source


def test_production_artifact_is_still_unchanged() -> None:
    assert _sha(PRODUCTION) == proof.EXPECTED_DATABASE_SHA256
    assert not Path("logs/m4_10_recovery_proof.log").exists()
    assert not Path(str(PRODUCTION) + "-wal").exists()
    assert not Path(str(PRODUCTION) + "-shm").exists()
