CREATE TABLE exchange_execution_roots (
    execution_root_id TEXT PRIMARY KEY,
    venue TEXT NOT NULL CHECK(venue = 'binance-spot'),
    environment TEXT NOT NULL CHECK(environment = 'testnet'),
    account_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    model_version TEXT NOT NULL,
    configuration_fingerprint TEXT NOT NULL CHECK(length(configuration_fingerprint) = 64),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64),
    UNIQUE(venue, environment, account_fingerprint, symbol, interval)
);

CREATE TABLE exchange_sessions (
    session_id TEXT PRIMARY KEY,
    execution_root_id TEXT NOT NULL REFERENCES exchange_execution_roots(execution_root_id),
    account_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('ACTIVE','CLOSED')),
    authorized_at_ms INTEGER NOT NULL CHECK(authorized_at_ms >= 0),
    submitted_notional TEXT NOT NULL,
    executed_turnover TEXT NOT NULL,
    submission_count INTEGER NOT NULL CHECK(submission_count >= 0),
    execution_failure_count INTEGER NOT NULL CHECK(execution_failure_count >= 0),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    model_version TEXT NOT NULL
);

CREATE TABLE exchange_control_state (
    execution_root_id TEXT PRIMARY KEY REFERENCES exchange_execution_roots(execution_root_id),
    kill_switch_state TEXT NOT NULL CHECK(kill_switch_state IN ('ACTIVE','CLEARED')),
    control_generation INTEGER NOT NULL CHECK(control_generation >= 0),
    reconciliation_generation INTEGER NOT NULL CHECK(reconciliation_generation >= 0),
    reason_code TEXT NOT NULL,
    audit_identifier TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0),
    model_version TEXT NOT NULL
);

CREATE TABLE exchange_orders (
    durable_command_key TEXT PRIMARY KEY CHECK(length(durable_command_key) = 64),
    stable_decision_key TEXT NOT NULL CHECK(length(stable_decision_key) = 64),
    client_order_id TEXT NOT NULL UNIQUE,
    execution_root_id TEXT NOT NULL REFERENCES exchange_execution_roots(execution_root_id),
    session_id TEXT NOT NULL REFERENCES exchange_sessions(session_id),
    account_fingerprint TEXT NOT NULL,
    venue TEXT NOT NULL CHECK(venue = 'binance-spot'),
    environment TEXT NOT NULL CHECK(environment = 'testnet'),
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    original_quantity TEXT NOT NULL,
    approved_notional TEXT NOT NULL,
    status TEXT NOT NULL,
    exchange_order_id TEXT NULL,
    cumulative_executed_base TEXT NOT NULL,
    cumulative_quote TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    submitted_at_ms INTEGER NULL,
    acknowledged_at_ms INTEGER NULL,
    latest_exchange_update_ms INTEGER NULL,
    terminal INTEGER NOT NULL CHECK(terminal IN (0,1)),
    version INTEGER NOT NULL CHECK(version >= 1),
    command_payload TEXT NOT NULL,
    state_payload TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64),
    UNIQUE(execution_root_id, exchange_order_id)
);

CREATE TABLE exchange_order_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    order_version INTEGER NOT NULL CHECK(order_version >= 1),
    previous_status TEXT NULL,
    new_status TEXT NOT NULL,
    source TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    exchange_order_id TEXT NULL,
    cumulative_executed_base TEXT NOT NULL,
    cumulative_quote TEXT NOT NULL,
    exchange_timestamp_ms INTEGER NULL,
    occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms >= 0),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    UNIQUE(durable_command_key, order_version)
);

CREATE TABLE exchange_submission_attempts (
    attempt_id TEXT PRIMARY KEY,
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    attempt_ordinal INTEGER NOT NULL CHECK(attempt_ordinal = 1),
    client_order_id TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL CHECK(started_at_ms >= 0),
    approved_notional TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    UNIQUE(durable_command_key, attempt_ordinal)
);

CREATE TABLE exchange_trades (
    venue TEXT NOT NULL,
    environment TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    client_order_id TEXT NOT NULL,
    interval TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    quote_quantity TEXT NOT NULL,
    commission TEXT NOT NULL,
    commission_asset TEXT NOT NULL,
    traded_at_ms INTEGER NOT NULL CHECK(traded_at_ms >= 0),
    is_maker INTEGER NULL CHECK(is_maker IN (0,1)),
    settlement_status TEXT NOT NULL CHECK(settlement_status IN
        ('PENDING','SETTLED','BLOCKED_UNSUPPORTED_FEE','RECONCILIATION_REQUIRED')),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    PRIMARY KEY(venue, environment, account_fingerprint, symbol, exchange_trade_id)
);

CREATE TABLE exchange_reconciliation_records (
    reconciliation_id TEXT PRIMARY KEY,
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    status TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64)
);

CREATE TRIGGER exchange_order_events_no_update BEFORE UPDATE ON exchange_order_events BEGIN
    SELECT RAISE(ABORT, 'exchange order events are append-only');
END;
CREATE TRIGGER exchange_order_events_no_delete BEFORE DELETE ON exchange_order_events BEGIN
    SELECT RAISE(ABORT, 'exchange order events are append-only');
END;
CREATE TRIGGER exchange_trades_no_update BEFORE UPDATE ON exchange_trades BEGIN
    SELECT RAISE(ABORT, 'exchange trades are append-only');
END;
CREATE TRIGGER exchange_trades_no_delete BEFORE DELETE ON exchange_trades BEGIN
    SELECT RAISE(ABORT, 'exchange trades are append-only');
END;
