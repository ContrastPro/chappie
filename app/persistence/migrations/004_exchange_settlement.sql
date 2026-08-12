CREATE TABLE exchange_portfolio_roots (
    portfolio_id TEXT PRIMARY KEY,
    execution_root_id TEXT NOT NULL UNIQUE REFERENCES exchange_execution_roots(execution_root_id),
    state_payload TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64),
    config_fingerprint TEXT NOT NULL CHECK(length(config_fingerprint) = 64),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    model_version TEXT NOT NULL
);

CREATE UNIQUE INDEX exchange_orders_decision_slot
ON exchange_orders(json_extract(command_payload, '$.decision_slot_key'));

CREATE TABLE exchange_portfolio_current (
    portfolio_id TEXT PRIMARY KEY REFERENCES exchange_portfolio_roots(portfolio_id),
    portfolio_version INTEGER NOT NULL CHECK(portfolio_version >= 0),
    state_payload TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64),
    last_settlement_seq INTEGER NULL,
    CHECK((portfolio_version = 0 AND last_settlement_seq IS NULL) OR
          (portfolio_version > 0 AND last_settlement_seq IS NOT NULL))
);

CREATE TABLE exchange_reconciliation_coverage (
    reconciliation_id TEXT PRIMARY KEY REFERENCES exchange_reconciliation_runs(reconciliation_id),
    execution_root_id TEXT NOT NULL REFERENCES exchange_execution_roots(execution_root_id),
    session_id TEXT NOT NULL REFERENCES exchange_sessions(session_id),
    account_fingerprint TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    coverage_start_ms INTEGER NOT NULL CHECK(coverage_start_ms >= 0),
    coverage_end_ms INTEGER NOT NULL CHECK(coverage_end_ms >= coverage_start_ms),
    lower_bound_complete INTEGER NOT NULL CHECK(lower_bound_complete IN (0,1)),
    upper_bound_complete INTEGER NOT NULL CHECK(upper_bound_complete IN (0,1)),
    trade_scope TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    evidence_payload TEXT NOT NULL,
    evidence_digest TEXT NOT NULL CHECK(length(evidence_digest) = 64),
    UNIQUE(execution_root_id,generation)
);

CREATE TRIGGER exchange_reconciliation_coverage_no_update
BEFORE UPDATE ON exchange_reconciliation_coverage BEGIN
    SELECT RAISE(ABORT, 'exchange reconciliation coverage is append-only');
END;
CREATE TRIGGER exchange_reconciliation_coverage_no_delete
BEFORE DELETE ON exchange_reconciliation_coverage BEGIN
    SELECT RAISE(ABORT, 'exchange reconciliation coverage is append-only');
END;

CREATE TABLE exchange_trade_settlements (
    settlement_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL UNIQUE,
    portfolio_id TEXT NOT NULL REFERENCES exchange_portfolio_roots(portfolio_id),
    session_id TEXT NOT NULL REFERENCES exchange_sessions(session_id),
    venue TEXT NOT NULL,
    environment TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    reconciliation_id TEXT NOT NULL REFERENCES exchange_reconciliation_runs(reconciliation_id),
    reconciliation_generation INTEGER NOT NULL CHECK(reconciliation_generation > 0),
    fill_id TEXT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('SETTLED','BLOCKED_UNSUPPORTED_FEE')),
    portfolio_version_before INTEGER NOT NULL CHECK(portfolio_version_before >= 0),
    portfolio_version_after INTEGER NOT NULL,
    before_digest TEXT NOT NULL CHECK(length(before_digest) = 64),
    after_state_payload TEXT NULL,
    after_state_digest TEXT NULL,
    settled_at_ms INTEGER NOT NULL CHECK(settled_at_ms >= 0),
    model_version TEXT NOT NULL,
    record_payload TEXT NOT NULL,
    record_digest TEXT NOT NULL CHECK(length(record_digest) = 64),
    UNIQUE(venue,environment,account_fingerprint,symbol,exchange_trade_id),
    CHECK(
      (status='SETTLED' AND fill_id IS NOT NULL AND
       portfolio_version_after=portfolio_version_before+1 AND
       after_state_payload IS NOT NULL AND length(after_state_digest)=64)
      OR
      (status='BLOCKED_UNSUPPORTED_FEE' AND fill_id IS NULL AND
       portfolio_version_after=portfolio_version_before AND
       after_state_payload IS NULL AND after_state_digest IS NULL)
    )
);

CREATE UNIQUE INDEX exchange_portfolio_settlement_version
ON exchange_trade_settlements(portfolio_id,portfolio_version_after)
WHERE status='SETTLED';

CREATE TRIGGER exchange_trade_settlements_no_update
BEFORE UPDATE ON exchange_trade_settlements BEGIN
    SELECT RAISE(ABORT, 'exchange trade settlements are append-only');
END;
CREATE TRIGGER exchange_trade_settlements_no_delete
BEFORE DELETE ON exchange_trade_settlements BEGIN
    SELECT RAISE(ABORT, 'exchange trade settlements are append-only');
END;
