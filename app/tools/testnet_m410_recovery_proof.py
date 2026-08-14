"""Fresh-process, read-only durable recovery proof for the M4.10 smoke.

Importing this module performs no I/O.  It deliberately has no exchange transport,
credential, submission, cancellation, or settlement dependency.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from time import time_ns
from typing import TextIO
from urllib.parse import quote
from uuid import uuid4

from app.persistence.canonical import canonical_json, canonical_sha256, deserialize_decimal
from app.persistence.exceptions import PersistenceSerializationError

AUTHORIZATION_ENV = "M4_10_RECOVERY_PROOF_AUTHORIZED"
AUTHORIZATION_VALUE = "YES"
DATABASE_PATH = Path("data/m4_10_testnet_smoke.sqlite3")
EVIDENCE_PATH = Path("logs/m4_10_recovery_proof.log")
EXPECTED_DATABASE_SHA256 = "1d3df2d7a07d236a7054319f52c64b72550a5bcb9818796cb376b2a11ac8ae90"
POLICY_ID = "zero-commission-asset-neutral/v1"
EXPECTED_BASE = Decimal("0.00016")
EXPECTED_QUOTE = Decimal("10.109264")
EXPECTED_USDT = Decimal("14.890736")
EXPECTED_MIGRATIONS = (
    (1, "001_initial.sql", "1c44bf2c187c26b01837787b50bca5bf952a7cd9fb9ed17b8377952e090fa433"),
    (
        2,
        "002_exchange_execution.sql",
        "c833d949fd1fab7b4a90990ff3a1a743be655ada7b1fd162a51866a478f7040c",
    ),
    (
        3,
        "003_exchange_reconciliation.sql",
        "05e19fb9ee43f992f6efb15cab537b253d27aa2ea636024cc5743852cf0a4bb1",
    ),
    (
        4,
        "004_exchange_settlement.sql",
        "4fb52f23e9c4fde653d26170a88c6d8e006062653188b3380ac57a0fec410a4e",
    ),
    (
        5,
        "005_phase3_smoke_authority.sql",
        "57c1422ffa74e7f0975ba2d271793088da591ab843f4380d79e1bbcb7d8f5345",
    ),
    (
        6,
        "006_zero_commission_resolution.sql",
        "7b6543b489fd9ca67435093e3e34f5fde48101803e65cd502ae5c1004af80f1d",
    ),
)


class RecoveryProofError(RuntimeError):
    """Base recovery-proof error."""


class RecoveryProofBlocked(RecoveryProofError):
    """Authorization or durable proof prerequisites are absent."""


class RecoveryProofFailed(RecoveryProofError):
    """Durable state does not prove the accepted M4.10 result."""


class RecoveryProofOutcome(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RecoveryProofResult:
    outcome: RecoveryProofOutcome
    run_id: str
    observed_at_ms: int
    database_sha256_before: str
    database_sha256_after: str
    schema_version: int
    authority_id: str
    command_key: str
    client_order_id: str
    exchange_order_id: str
    exchange_trade_id: str
    resolution_id: str
    portfolio_version: int
    managed_btc: Decimal
    managed_usdt: Decimal
    fees: Decimal
    turnover: Decimal
    order_status: str
    authority_count: int
    command_count: int
    order_count: int
    submission_attempt_count: int
    trade_count: int
    blocked_settlement_count: int
    resolution_count: int
    portfolio_transition_count: int
    pending_settlement_count: int
    unresolved_blocked_count: int

    def render(self) -> str:
        fields = (
            ("run_id", self.run_id),
            ("observed_at_ms", self.observed_at_ms),
            ("database_sha256_before", self.database_sha256_before),
            ("database_sha256_after", self.database_sha256_after),
            ("schema_version", self.schema_version),
            ("authority_id", self.authority_id),
            ("durable_command_key", self.command_key),
            ("client_order_id", self.client_order_id),
            ("exchange_order_id", self.exchange_order_id),
            ("exchange_trade_id", self.exchange_trade_id),
            ("policy_id", POLICY_ID),
            ("resolution_id", self.resolution_id),
            ("portfolio_version", self.portfolio_version),
            ("managed_btc", self.managed_btc),
            ("managed_usdt", self.managed_usdt),
            ("fees", self.fees),
            ("turnover", self.turnover),
            ("order_status", self.order_status),
            ("authority_count", self.authority_count),
            ("command_count", self.command_count),
            ("order_count", self.order_count),
            ("submission_attempt_count", self.submission_attempt_count),
            ("trade_count", self.trade_count),
            ("blocked_settlement_count", self.blocked_settlement_count),
            ("resolution_count", self.resolution_count),
            ("portfolio_transition_count", self.portfolio_transition_count),
            ("pending_settlement_count", self.pending_settlement_count),
            ("unresolved_blocked_count", self.unresolved_blocked_count),
            ("global_settlement_status", "SYNCHRONIZED"),
            ("outcome", self.outcome.value),
        )
        return "".join(f"{key}={value}\n" for key, value in fields)


@dataclass(frozen=True, slots=True)
class _ObservedPortfolio:
    canonical_payload: str
    digest: str
    portfolio_id: str
    version: int
    base_balance: Decimal
    quote_balance: Decimal
    total_fees: Decimal
    turnover: Decimal
    trade_count: int
    applied_fill_count: int


def _portfolio_state(payload: str, digest: str) -> _ObservedPortfolio:
    try:
        value = json.loads(payload)
        canonical = canonical_json(value)
        expected = canonical_sha256(value, algorithm_version="portfolio-state/v1")
        result = _ObservedPortfolio(
            canonical,
            expected,
            value["portfolio_id"],
            value["version"],
            deserialize_decimal(value["base_balance"]["$decimal"]),
            deserialize_decimal(value["quote_balance"]["$decimal"]),
            deserialize_decimal(value["total_fees"]["$decimal"]),
            deserialize_decimal(value["turnover"]["$decimal"]),
            value["trade_count"],
            value["applied_fill_count"],
        )
    except (json.JSONDecodeError, KeyError, TypeError, PersistenceSerializationError) as error:
        raise RecoveryProofFailed("Portfolio payload is invalid") from error
    if canonical != payload or expected != digest:
        raise RecoveryProofFailed("Portfolio payload digest is invalid")
    return result


def validate_startup(environ: Mapping[str, str], *, database: Path, evidence: Path) -> None:
    """Apply non-I/O authorization first, then path collision guards."""
    if environ.get(AUTHORIZATION_ENV) != AUTHORIZATION_VALUE:
        raise RecoveryProofBlocked("recovery proof authorization is absent or invalid")
    if evidence.exists():
        raise RecoveryProofBlocked("recovery proof evidence already exists")
    if not database.is_file():
        raise RecoveryProofBlocked("Phase 3 smoke database is absent")
    _reject_sidecars(database)


def _sidecars(database: Path) -> tuple[Path, Path]:
    return (
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    )


def _reject_sidecars(database: Path) -> None:
    if any(path.exists() for path in _sidecars(database)):
        raise RecoveryProofBlocked("recovery proof requires absent WAL/SHM sidecars")


def _hit(injector: Callable[[str], None] | None, boundary: str) -> None:
    if injector is not None:
        injector(boundary)


def _close_readonly(connection: sqlite3.Connection) -> None:
    connection.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _one(connection: sqlite3.Connection, table: str) -> sqlite3.Row:
    rows = connection.execute(f"SELECT * FROM {table}").fetchall()
    if len(rows) != 1:
        raise RecoveryProofFailed(f"{table} must contain exactly one row")
    return rows[0]


def _json_payload(row: sqlite3.Row, payload: str, digest: str, algorithm: str) -> dict:
    try:
        value = json.loads(row[payload])
    except (json.JSONDecodeError, TypeError) as error:
        raise RecoveryProofFailed(f"invalid {payload}") from error
    if (
        canonical_json(value) != row[payload]
        or canonical_sha256(value, algorithm_version=algorithm) != row[digest]
    ):
        raise RecoveryProofFailed(f"{payload} canonical digest mismatch")
    return value


def _columns_match(payload: dict, row: sqlite3.Row, names: tuple[str, ...]) -> None:
    if any(payload.get(name) != row[name] for name in names):
        raise RecoveryProofFailed("canonical payload conflicts with durable columns")


def _validate_migrations(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    if tuple(tuple(row) for row in rows) != EXPECTED_MIGRATIONS:
        raise RecoveryProofFailed("database is not exactly schema v6")


def _economic_slot(root: str, account: str) -> str:
    return canonical_sha256(
        {
            "account_fingerprint": account,
            "execution_root_id": root,
            "slot": "first-managed-btcusdt-buy",
        },
        algorithm_version="phase3-economic-slot/v1",
    )


def _client_order_id(account: str, command: str) -> str:
    envelope = {
        "algorithm_version": "exchange-client-order/v1",
        "payload": {
            "account_fingerprint": account,
            "durable_command_key": command,
            "environment": "testnet",
            "venue": "binance-spot",
        },
    }
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).digest()
    return "ctt1t-" + base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:26]


def _verify(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    observed_at_ms: int,
    checksum: str,
    failure_injector: Callable[[str], None] | None = None,
):
    _validate_migrations(connection)
    authority = _one(connection, "exchange_smoke_authorities")
    authority_event = _one(connection, "exchange_smoke_authority_events")
    root = _one(connection, "exchange_execution_roots")
    session = _one(connection, "exchange_sessions")
    control = _one(connection, "exchange_control_state")
    portfolio_root = _one(connection, "exchange_portfolio_roots")
    portfolio_current = _one(connection, "exchange_portfolio_current")
    order = _one(connection, "exchange_orders")
    attempt = _one(connection, "exchange_submission_attempts")
    trade = _one(connection, "exchange_trades")
    blocked = _one(connection, "exchange_trade_settlements")
    resolution = _one(connection, "exchange_trade_settlement_resolutions")
    reconciliation = _one(connection, "exchange_reconciliation_current")
    counts = {
        "authority": connection.execute(
            "SELECT count(*) FROM exchange_smoke_authorities"
        ).fetchone()[0],
        "command": connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0],
        "order": connection.execute("SELECT count(*) FROM exchange_orders").fetchone()[0],
        "attempt": connection.execute(
            "SELECT count(*) FROM exchange_submission_attempts"
        ).fetchone()[0],
        "trade": connection.execute("SELECT count(*) FROM exchange_trades").fetchone()[0],
        "block": connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='BLOCKED_UNSUPPORTED_FEE'"
        ).fetchone()[0],
        "resolution": connection.execute(
            "SELECT count(*) FROM exchange_trade_settlement_resolutions"
        ).fetchone()[0],
        "settled": connection.execute(
            "SELECT count(*) FROM exchange_trade_settlements WHERE status='SETTLED'"
        ).fetchone()[0],
    }
    pending = connection.execute(
        "SELECT count(*) FROM exchange_trades t LEFT JOIN exchange_trade_settlements s ON "
        "s.venue=t.venue AND s.environment=t.environment AND "
        "s.account_fingerprint=t.account_fingerprint AND s.symbol=t.symbol AND "
        "s.exchange_trade_id=t.exchange_trade_id WHERE s.settlement_id IS NULL"
    ).fetchone()[0]
    unresolved = connection.execute(
        "SELECT count(*) FROM exchange_trade_settlements b LEFT JOIN "
        "exchange_trade_settlement_resolutions r ON r.blocked_settlement_id=b.settlement_id "
        "WHERE b.status='BLOCKED_UNSUPPORTED_FEE' AND r.resolution_id IS NULL"
    ).fetchone()[0]
    if counts != {
        "authority": 1,
        "command": 1,
        "order": 1,
        "attempt": 1,
        "trade": 1,
        "block": 1,
        "resolution": 1,
        "settled": 0,
    }:
        raise RecoveryProofFailed("durable exactly-once count matrix is invalid")
    _hit(failure_injector, "after_cardinality_queries")

    root_payload = {
        "execution_root_id": root["execution_root_id"],
        "venue": root["venue"],
        "environment": root["environment"],
        "account_fingerprint": root["account_fingerprint"],
        "symbol": root["symbol"],
        "interval": root["interval"],
        "configuration_fingerprint": root["configuration_fingerprint"],
        "created_at_ms": root["created_at_ms"],
        "model_version": root["model_version"],
    }
    if (
        canonical_sha256(root_payload, algorithm_version=root["model_version"])
        != root["state_digest"]
    ):
        raise RecoveryProofFailed("execution root digest is invalid")

    authority_payload = _json_payload(
        authority, "payload", "payload_digest", authority["policy_version"]
    )
    authority_names = (
        "authority_id",
        "execution_root_id",
        "session_id",
        "account_fingerprint",
        "managed_portfolio_id",
        "venue",
        "environment",
        "symbol",
        "side",
        "control_generation",
        "reconciliation_generation",
        "operator_run_id",
        "economic_slot_key",
        "policy_version",
        "rules_fingerprint",
        "config_fingerprint",
        "risk_envelope_digest",
        "balance_scope_digest",
        "consumed_durable_command_key",
        "created_at_ms",
        "consumed_at_ms",
        "status",
    )
    _columns_match(authority_payload, authority, authority_names)
    event_payload = _json_payload(
        authority_event, "payload", "payload_digest", "phase3-authority-event/v1"
    )
    _columns_match(
        event_payload,
        authority_event,
        (
            "authority_id",
            "event_ordinal",
            "event_type",
            "durable_command_key",
            "operator_run_id",
            "occurred_at_ms",
        ),
    )
    if event_payload.get("policy_version") != authority["policy_version"]:
        raise RecoveryProofFailed("authority event policy lineage is invalid")
    expected_event_id = canonical_sha256(
        {"authority_id": authority["authority_id"], "event_ordinal": 1},
        algorithm_version="phase3-authority-event-id/v1",
    )
    if (
        authority["status"] != "CONSUMED"
        or authority["side"] != "BUY"
        or authority_event["event_id"] != expected_event_id
        or authority_event["event_type"] != "CONSUMED"
        or authority_event["event_ordinal"] != 1
        or authority_event["authority_id"] != authority["authority_id"]
        or authority["venue"] != "binance-spot"
        or authority["environment"] != "testnet"
        or authority["symbol"] != "BTCUSDT"
        or authority["policy_version"] != "m4.10-phase3-smoke/v1"
        or authority["economic_slot_key"]
        != _economic_slot(authority["execution_root_id"], authority["account_fingerprint"])
        or authority["control_generation"] != 0
        or authority["reconciliation_generation"] != 1
        or authority["created_at_ms"] != authority["consumed_at_ms"]
    ):
        raise RecoveryProofFailed("Phase 3 authority/audit chain is invalid")

    envelope = json.loads(authority["risk_envelope_payload"])
    balance = json.loads(authority["balance_scope_payload"])
    expected_envelope = {
        "managed_quote_allocation": {"$decimal": "25"},
        "max_order_notional": {"$decimal": "12"},
        "max_submitted_notional": {"$decimal": "12"},
        "max_turnover": {"$decimal": "12"},
        "max_managed_position_notional": {"$decimal": "12"},
        "min_retained_reserve": {"$decimal": "13"},
        "max_submissions": 1,
        "max_non_terminal": 1,
        "max_in_flight": 1,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "assumptions_fingerprint": envelope.get("assumptions_fingerprint"),
    }
    if (
        canonical_sha256(envelope, algorithm_version=authority["policy_version"])
        != authority["risk_envelope_digest"]
        or canonical_sha256(balance, algorithm_version="phase3-balance-scope/v1")
        != authority["balance_scope_digest"]
        or envelope != expected_envelope
        or balance.get("account_fingerprint") != authority["account_fingerprint"]
        or balance.get("managed_quote_allocation") != {"$decimal": "25"}
        or balance.get("btc_free") != {"$decimal": "1"}
        or balance.get("btc_locked") != {"$decimal": "0"}
        or balance.get("usdt_free") != {"$decimal": "10000"}
        or balance.get("usdt_locked") != {"$decimal": "0"}
        or balance.get("interpretation") != "btc-external-unmanaged;usdt-managed-allocation-only"
    ):
        raise RecoveryProofFailed("Phase 3 authority envelope is invalid")
    _hit(failure_injector, "after_authority_queries")

    command = json.loads(order["command_payload"])
    if (
        canonical_json(command) != order["command_payload"]
        or command.get("durable_command_key") != order["durable_command_key"]
        or command.get("stable_decision_key") != order["stable_decision_key"]
        or command.get("decision_slot_key") != authority["economic_slot_key"]
        or command.get("correlation_id") != authority["operator_run_id"]
        or command.get("instrument_rules_fingerprint") != authority["rules_fingerprint"]
        or command.get("execution_configuration_fingerprint") != authority["config_fingerprint"]
        or command.get("side") != "BUY"
        or command.get("stream_key") != {"symbol": "BTCUSDT", "interval": "1m"}
        or command.get("base_quantity") != {"$decimal": "0.00016"}
        or command.get("approved_notional") != {"$decimal": "10.109264"}
        or order["client_order_id"]
        != _client_order_id(order["account_fingerprint"], order["durable_command_key"])
        or authority["consumed_durable_command_key"] != order["durable_command_key"]
        or authority_event["durable_command_key"] != order["durable_command_key"]
    ):
        raise RecoveryProofFailed("command payload is not canonical")
    order_state = _json_payload(order, "state_payload", "state_digest", "exchange-order/v1")
    expected_order_state = {
        "durable_command_key": order["durable_command_key"],
        "client_order_id": order["client_order_id"],
        "account_fingerprint": order["account_fingerprint"],
        "symbol": order["symbol"],
        "interval": order["interval"],
        "side": order["side"],
        "original_base_quantity": {"$decimal": order["original_quantity"]},
        "status": order["status"],
        "created_at_ms": order["created_at_ms"],
        "exchange_order_id": order["exchange_order_id"],
        "cumulative_executed_base_quantity": {"$decimal": order["cumulative_executed_base"]},
        "cumulative_quote_quantity": {"$decimal": order["cumulative_quote"]},
        "submitted_at_ms": order["submitted_at_ms"],
        "acknowledged_at_ms": order["acknowledged_at_ms"],
        "latest_exchange_update_ms": order["latest_exchange_update_ms"],
        "environment": order["environment"],
        "venue": order["venue"],
        "model_version": "exchange-order/v1",
    }
    if order_state != expected_order_state:
        raise RecoveryProofFailed("order state payload conflicts with projection")
    events = connection.execute(
        "SELECT * FROM exchange_order_events ORDER BY order_version"
    ).fetchall()
    if len(events) != order["version"] or [r["order_version"] for r in events] != list(
        range(1, order["version"] + 1)
    ):
        raise RecoveryProofFailed("order event chain is incomplete")
    expected_statuses = (
        (None, "PREPARED"),
        ("PREPARED", "SUBMITTING"),
        ("SUBMITTING", "ACKNOWLEDGED"),
        ("ACKNOWLEDGED", "FILLED"),
    )
    previous_time = -1
    for event, (previous, current) in zip(events, expected_statuses, strict=True):
        payload = _json_payload(event, "payload", "payload_digest", "exchange-order-event/v1")
        expected_event_id = canonical_sha256(
            {
                "durable_command_key": order["durable_command_key"],
                "order_version": event["order_version"],
            },
            algorithm_version="exchange-order-event-id/v1",
        )
        expected_event_payload = {
            "durable_command_key": event["durable_command_key"],
            "order_version": event["order_version"],
            "previous_status": event["previous_status"],
            "new_status": event["new_status"],
            "source": event["source"],
            "reason_code": event["reason_code"],
            "exchange_order_id": event["exchange_order_id"],
            "cumulative_executed_base": {"$decimal": event["cumulative_executed_base"]},
            "cumulative_quote": {"$decimal": event["cumulative_quote"]},
            "exchange_timestamp_ms": event["exchange_timestamp_ms"],
            "occurred_at_ms": event["occurred_at_ms"],
        }
        if (
            payload != expected_event_payload
            or event["event_id"] != expected_event_id
            or event["durable_command_key"] != order["durable_command_key"]
            or (event["previous_status"], event["new_status"]) != (previous, current)
            or event["occurred_at_ms"] < previous_time
        ):
            raise RecoveryProofFailed("order lifecycle event chronology is invalid")
        previous_time = event["occurred_at_ms"]
    if sum(event["new_status"] == "SUBMITTING" for event in events) != 1:
        raise RecoveryProofFailed("order must contain exactly one SUBMITTING transition")

    attempt_values = {
        "durable_command_key": attempt["durable_command_key"],
        "client_order_id": attempt["client_order_id"],
        "attempt_ordinal": attempt["attempt_ordinal"],
        "approved_notional": deserialize_decimal(attempt["approved_notional"]),
        "started_at_ms": attempt["started_at_ms"],
    }
    attempt_digest = canonical_sha256(attempt_values, algorithm_version="submission-attempt/v1")
    if attempt["attempt_id"] != attempt_digest or attempt["payload_digest"] != attempt_digest:
        raise RecoveryProofFailed("submission attempt digest is invalid")
    if (
        attempt["client_order_id"] != order["client_order_id"]
        or attempt["started_at_ms"] != order["submitted_at_ms"]
        or attempt["approved_notional"] != order["approved_notional"]
        or events[0]["occurred_at_ms"] != order["created_at_ms"]
        or events[1]["occurred_at_ms"] != attempt["started_at_ms"]
    ):
        raise RecoveryProofFailed("submission attempt/order chronology is invalid")
    _hit(failure_injector, "after_order_queries")

    try:
        trade_payload = json.loads(trade["payload"])
    except (json.JSONDecodeError, TypeError) as error:
        raise RecoveryProofFailed("invalid trade payload") from error
    identity = trade_payload.get("identity", {})
    stream = trade_payload.get("stream_key", {})
    if (
        canonical_json(trade_payload) != trade["payload"]
        or canonical_sha256(trade["payload"], algorithm_version="exchange-trade/v1")
        != trade["payload_digest"]
        or any(
            identity.get(name) != trade[name]
            for name in (
                "venue",
                "environment",
                "account_fingerprint",
                "symbol",
                "exchange_order_id",
                "exchange_trade_id",
            )
        )
        or stream != {"symbol": trade["symbol"], "interval": trade["interval"]}
        or any(
            trade_payload.get(name) != trade[name]
            for name in (
                "client_order_id",
                "side",
                "commission_asset",
                "traded_at_ms",
            )
        )
        or trade_payload.get("is_maker") != bool(trade["is_maker"])
        or trade_payload.get("quantity") != {"$decimal": trade["quantity"]}
        or trade_payload.get("price") != {"$decimal": trade["price"]}
        or trade_payload.get("quote_quantity") != {"$decimal": trade["quote_quantity"]}
        or trade_payload.get("commission") != {"$decimal": trade["commission"]}
        or trade["traded_at_ms"] != order["latest_exchange_update_ms"]
        or trade["traded_at_ms"] > events[-1]["occurred_at_ms"]
    ):
        raise RecoveryProofFailed("trade payload conflicts with durable columns")
    _hit(failure_injector, "after_trade_queries")

    blocked_payload = _json_payload(
        blocked, "record_payload", "record_digest", blocked["model_version"]
    )
    _columns_match(
        blocked_payload,
        blocked,
        (
            "settlement_id",
            "exchange_trade_id",
            "durable_command_key",
            "reconciliation_id",
            "reconciliation_generation",
            "status",
        ),
    )
    expected_settlement_id = canonical_sha256(
        {
            "venue": trade["venue"],
            "environment": trade["environment"],
            "account": trade["account_fingerprint"],
            "symbol": trade["symbol"],
            "trade": trade["exchange_trade_id"],
        },
        algorithm_version="exchange-settlement-id/v1",
    )
    resolution_payload = _json_payload(
        resolution, "record_payload", "record_digest", resolution["model_version"]
    )
    if resolution_payload != {
        "resolution_id": resolution["resolution_id"],
        "fill_id": resolution["fill_id"],
        "blocked_settlement_id": resolution["blocked_settlement_id"],
        "portfolio_id": resolution["portfolio_id"],
        "session_id": resolution["session_id"],
        "exchange_trade_id": resolution["exchange_trade_id"],
        "durable_command_key": resolution["durable_command_key"],
        "reconciliation_id": resolution["reconciliation_id"],
        "reconciliation_generation": resolution["reconciliation_generation"],
        "policy_id": resolution["policy_id"],
        "reason_code": resolution["reason_code"],
        "version_before": resolution["portfolio_version_before"],
        "version_after": resolution["portfolio_version_after"],
        "created_at_ms": resolution["created_at_ms"],
        "model_version": resolution["model_version"],
    }:
        raise RecoveryProofFailed("resolution payload conflicts with durable columns")
    expected_resolution_id = canonical_sha256(
        {"blocked_settlement_id": blocked["settlement_id"], "policy": POLICY_ID},
        algorithm_version="exchange-settlement-resolution-id/v1",
    )
    expected_fill_id = canonical_sha256(
        {
            "resolution_id": expected_resolution_id,
            "exchange_trade_id": trade["exchange_trade_id"],
        },
        algorithm_version="exchange-resolution-fill-id/v1",
    )
    if (
        blocked["settlement_id"] != expected_settlement_id
        or resolution["resolution_id"] != expected_resolution_id
        or resolution["fill_id"] != expected_fill_id
        or blocked["portfolio_id"] != portfolio_root["portfolio_id"]
        or blocked["session_id"] != session["session_id"]
        or blocked["exchange_trade_id"] != trade["exchange_trade_id"]
        or blocked["durable_command_key"] != order["durable_command_key"]
        or blocked["portfolio_version_before"] != 0
        or blocked["portfolio_version_after"] != 0
        or blocked["before_digest"] != portfolio_root["state_digest"]
        or blocked["after_state_payload"] is not None
        or blocked["after_state_digest"] is not None
        or blocked["settled_at_ms"] < events[-1]["occurred_at_ms"]
        or blocked["reconciliation_id"] != reconciliation["latest_reconciliation_id"]
        or blocked["reconciliation_generation"] != reconciliation["generation"]
        or resolution["portfolio_version_before"] != 0
        or resolution["portfolio_version_after"] != 1
        or resolution["created_at_ms"] < blocked["settled_at_ms"]
    ):
        raise RecoveryProofFailed("settlement/resolution durable identity is invalid")
    _hit(failure_injector, "after_settlement_queries")

    root_state = _portfolio_state(portfolio_root["state_payload"], portfolio_root["state_digest"])
    state = _portfolio_state(portfolio_current["state_payload"], portfolio_current["state_digest"])
    resolution_state = _portfolio_state(
        resolution["after_state_payload"], resolution["after_state_digest"]
    )
    if (
        root_state.digest != portfolio_root["state_digest"]
        or state.digest != portfolio_current["state_digest"]
        or resolution_state.digest != resolution["after_state_digest"]
        or state.canonical_payload != resolution["after_state_payload"]
        or resolution["before_digest"] != portfolio_root["state_digest"]
        or root_state.version != 0
        or root_state.base_balance != 0
        or root_state.quote_balance != 25
        or root_state.total_fees != 0
        or root_state.turnover != 0
        or root_state.trade_count != 0
        or root_state.applied_fill_count != 0
        or portfolio_current["portfolio_id"] != portfolio_root["portfolio_id"]
        or portfolio_current["portfolio_version"] != 1
        or portfolio_current["last_settlement_seq"] != blocked["settlement_seq"]
        or portfolio_root["config_fingerprint"] != authority["config_fingerprint"]
        or root["configuration_fingerprint"] != authority["config_fingerprint"]
    ):
        raise RecoveryProofFailed("Portfolio digest/version chain is invalid")
    _hit(failure_injector, "after_portfolio_queries")

    reconciliation_payload = _json_payload(
        reconciliation, "state_payload", "state_digest", reconciliation["model_version"]
    )
    _columns_match(
        reconciliation_payload,
        reconciliation,
        (
            "execution_root_id",
            "status",
            "generation",
            "latest_reconciliation_id",
            "updated_at_ms",
            "model_version",
        ),
    )
    coverage = connection.execute(
        "SELECT * FROM exchange_reconciliation_coverage WHERE reconciliation_id=?",
        (reconciliation["latest_reconciliation_id"],),
    ).fetchone()
    if coverage is None:
        raise RecoveryProofFailed("latest reconciliation coverage is absent")
    coverage_payload = _json_payload(
        coverage, "evidence_payload", "evidence_digest", coverage["algorithm_version"]
    )
    _columns_match(
        coverage_payload,
        coverage,
        (
            "reconciliation_id",
            "execution_root_id",
            "session_id",
            "account_fingerprint",
            "generation",
            "coverage_start_ms",
            "coverage_end_ms",
            "trade_scope",
            "algorithm_version",
        ),
    )
    run = connection.execute(
        "SELECT * FROM exchange_reconciliation_runs WHERE reconciliation_id=?",
        (reconciliation["latest_reconciliation_id"],),
    ).fetchone()
    if run is None:
        raise RecoveryProofFailed("latest reconciliation run is absent")
    try:
        run_payload = json.loads(run["result_payload"])
    except (json.JSONDecodeError, TypeError) as error:
        raise RecoveryProofFailed("latest reconciliation result payload is invalid") from error
    if (
        canonical_json(run_payload) != run["result_payload"]
        or canonical_sha256(run["result_payload"], algorithm_version=run["model_version"])
        != run["result_digest"]
        or run["reconciliation_id"] != reconciliation["latest_reconciliation_id"]
        or run["generation_after"] != reconciliation["generation"]
        or run["outcome"] != "SAFE"
        or run["execution_root_id"] != root["execution_root_id"]
        or run["session_id"] != session["session_id"]
        or run["account_fingerprint"] != root["account_fingerprint"]
        or run["completed_at_ms"] != reconciliation["updated_at_ms"]
        or coverage["generation"] != reconciliation["generation"]
        or coverage["execution_root_id"] != root["execution_root_id"]
        or coverage["session_id"] != session["session_id"]
        or coverage["account_fingerprint"] != root["account_fingerprint"]
        or coverage["coverage_start_ms"] > root["created_at_ms"]
        or coverage["coverage_end_ms"] < run["completed_at_ms"]
    ):
        raise RecoveryProofFailed("latest reconciliation run digest/lineage is invalid")
    all_runs = connection.execute(
        "SELECT * FROM exchange_reconciliation_runs ORDER BY generation_after"
    ).fetchall()
    all_coverage = connection.execute(
        "SELECT * FROM exchange_reconciliation_coverage ORDER BY generation"
    ).fetchall()
    if (
        len(all_runs) != 2
        or len(all_coverage) != 2
        or [(row["generation_before"], row["generation_after"]) for row in all_runs]
        != [(0, 1), (1, 2)]
        or [row["generation"] for row in all_coverage] != [1, 2]
    ):
        raise RecoveryProofFailed("reconciliation history cardinality/continuity is invalid")
    for historical_run, historical_coverage in zip(all_runs, all_coverage, strict=True):
        try:
            historical_result = json.loads(historical_run["result_payload"])
            historical_evidence = json.loads(historical_coverage["evidence_payload"])
        except (json.JSONDecodeError, TypeError) as error:
            raise RecoveryProofFailed("reconciliation history payload is invalid") from error
        if (
            canonical_json(historical_result) != historical_run["result_payload"]
            or canonical_sha256(
                historical_run["result_payload"],
                algorithm_version=historical_run["model_version"],
            )
            != historical_run["result_digest"]
            or canonical_json(historical_evidence) != historical_coverage["evidence_payload"]
            or canonical_sha256(
                historical_evidence,
                algorithm_version=historical_coverage["algorithm_version"],
            )
            != historical_coverage["evidence_digest"]
            or historical_run["reconciliation_id"] != historical_coverage["reconciliation_id"]
            or historical_run["generation_after"] != historical_coverage["generation"]
            or historical_run["outcome"] != "SAFE"
            or not historical_coverage["lower_bound_complete"]
            or not historical_coverage["upper_bound_complete"]
        ):
            raise RecoveryProofFailed("reconciliation history digest/coverage is invalid")
    _hit(failure_injector, "after_reconciliation_queries")

    lineage = (
        authority["execution_root_id"],
        authority["session_id"],
        authority["account_fingerprint"],
        authority["consumed_durable_command_key"],
    )
    if (
        lineage
        != (
            root["execution_root_id"],
            session["session_id"],
            root["account_fingerprint"],
            order["durable_command_key"],
        )
        or order["session_id"] != session["session_id"]
        or order["execution_root_id"] != root["execution_root_id"]
        or portfolio_root["execution_root_id"] != root["execution_root_id"]
        or resolution["blocked_settlement_id"] != blocked["settlement_id"]
        or resolution["durable_command_key"] != order["durable_command_key"]
        or resolution["exchange_trade_id"] != trade["exchange_trade_id"]
        or trade["durable_command_key"] != order["durable_command_key"]
        or attempt["durable_command_key"] != order["durable_command_key"]
        or resolution["session_id"] != session["session_id"]
        or resolution["reconciliation_id"] != reconciliation["latest_reconciliation_id"]
        or resolution["reconciliation_generation"] != reconciliation["generation"]
        or resolution["account_fingerprint"] != root["account_fingerprint"]
        or resolution["venue"] != root["venue"]
        or resolution["environment"] != root["environment"]
    ):
        raise RecoveryProofFailed("durable identity lineage is inconsistent")

    if (
        control["kill_switch_state"] != "ACTIVE"
        or reconciliation["status"] != "SAFE"
        or reconciliation["generation"] != control["reconciliation_generation"]
        or not coverage["lower_bound_complete"]
        or not coverage["upper_bound_complete"]
        or order["status"] != "FILLED"
        or order["terminal"] != 1
        or order["side"] != "BUY"
        or deserialize_decimal(order["original_quantity"]) != EXPECTED_BASE
        or deserialize_decimal(order["cumulative_executed_base"]) != EXPECTED_BASE
        or deserialize_decimal(order["cumulative_quote"]) != EXPECTED_QUOTE
        or deserialize_decimal(trade["quantity"]) != EXPECTED_BASE
        or deserialize_decimal(trade["quote_quantity"]) != EXPECTED_QUOTE
        or not deserialize_decimal(trade["commission"]).is_zero()
        or trade["commission_asset"] != "BTC"
        or blocked["status"] != "BLOCKED_UNSUPPORTED_FEE"
        or resolution["policy_id"] != POLICY_ID
        or resolution["reason_code"] != "exact_zero_commission_asset_neutral"
        or state.version != 1
        or state.base_balance != EXPECTED_BASE
        or state.quote_balance != EXPECTED_USDT
        or state.total_fees != 0
        or state.turnover != EXPECTED_QUOTE
        or state.trade_count != 1
        or state.applied_fill_count != 1
        or deserialize_decimal(session["executed_turnover"]) != EXPECTED_QUOTE
        or session["submission_count"] != 1
        or session["submitted_notional"] != order["approved_notional"]
        or session["execution_failure_count"] != 0
        or session["generation"] != 2
        or session["state"] != "ACTIVE"
        or session["model_version"] != "execution-session/v1"
        or root["model_version"] != "exchange-execution-root/v1"
        or portfolio_root["model_version"] != "exchange-portfolio-root/v1"
        or pending != 0
        or unresolved != 0
    ):
        raise RecoveryProofFailed("durable economic state is not globally synchronized")
    _hit(failure_injector, "after_economic_validation")

    return RecoveryProofResult(
        RecoveryProofOutcome.PASS,
        run_id,
        observed_at_ms,
        checksum,
        checksum,
        6,
        authority["authority_id"],
        order["durable_command_key"],
        order["client_order_id"],
        order["exchange_order_id"],
        trade["exchange_trade_id"],
        resolution["resolution_id"],
        state.version,
        state.base_balance,
        state.quote_balance,
        state.total_fees,
        state.turnover,
        order["status"],
        counts["authority"],
        counts["command"],
        counts["order"],
        1,
        1,
        1,
        1,
        1,
        pending,
        unresolved,
    )


def verify_database(
    database: Path,
    *,
    expected_sha256: str,
    run_id: str,
    observed_at_ms: int,
    failure_injector: Callable[[str], None] | None = None,
) -> RecoveryProofResult:
    """Validate one immutable SQLite snapshot and prove it was not mutated."""
    _reject_sidecars(database)
    _hit(failure_injector, "before_checksum_before")
    before = _sha256(database)
    _hit(failure_injector, "after_checksum_before")
    if before != expected_sha256:
        raise RecoveryProofBlocked("database checksum does not match authorized durable truth")
    uri = f"file:{quote(str(database.resolve()), safe='/')}?mode=ro&immutable=1"
    connection: sqlite3.Connection | None = None
    try:
        _hit(failure_injector, "before_open")
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RecoveryProofBlocked("SQLite query-only mode could not be enforced")
        _hit(failure_injector, "after_open")
        _hit(failure_injector, "before_validation")
        with localcontext() as context:
            context.prec = 100
            result = _verify(
                connection,
                run_id=run_id,
                observed_at_ms=observed_at_ms,
                checksum=before,
                failure_injector=failure_injector,
            )
        _hit(failure_injector, "after_validation")
    except sqlite3.Error as error:
        raise RecoveryProofFailed("read-only recovery database validation failed") from error
    finally:
        if connection is not None:
            try:
                _hit(failure_injector, "before_close")
            finally:
                _close_readonly(connection)
            _hit(failure_injector, "after_close")
    _hit(failure_injector, "before_checksum_after")
    after = _sha256(database)
    _hit(failure_injector, "after_checksum_after")
    if after != before:
        raise RecoveryProofFailed("database changed during read-only recovery proof")
    _reject_sidecars(database)
    return result


def _write_payload(target: TextIO, payload: str) -> int:
    return target.write(payload)


def write_evidence(
    path: Path,
    result: RecoveryProofResult,
    *,
    failure_injector: Callable[[str], None] | None = None,
) -> None:
    """Publish a fully durable sanitized PASS file with an atomic exclusive link."""
    payload = result.render()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _hit(failure_injector, "before_evidence_open")
        with temporary.open("x", encoding="utf-8", newline="\n") as target:
            _hit(failure_injector, "before_evidence_write")
            written = _write_payload(target, payload)
            _hit(failure_injector, "after_evidence_write")
            target.flush()
            _hit(failure_injector, "before_evidence_fsync")
            os.fsync(target.fileno())
            _hit(failure_injector, "after_evidence_fsync")
        if written != len(payload) or temporary.read_text(encoding="utf-8") != payload:
            raise RecoveryProofFailed("recovery proof evidence write was incomplete")
        _hit(failure_injector, "before_evidence_publish")
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise RecoveryProofBlocked("recovery proof evidence already exists") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    try:
        validate_startup(os.environ, database=DATABASE_PATH, evidence=EVIDENCE_PATH)
        result = verify_database(
            DATABASE_PATH,
            expected_sha256=EXPECTED_DATABASE_SHA256,
            run_id=str(uuid4()),
            observed_at_ms=time_ns() // 1_000_000,
        )
        write_evidence(EVIDENCE_PATH, result)
    except RecoveryProofBlocked as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    except (RecoveryProofFailed, OSError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    print(result.render(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
