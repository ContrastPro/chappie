CREATE TABLE exchange_managed_sell_authorities (
    authority_id TEXT PRIMARY KEY,
    execution_root_id TEXT NOT NULL REFERENCES exchange_execution_roots(execution_root_id),
    session_id TEXT NOT NULL REFERENCES exchange_sessions(session_id),
    account_fingerprint TEXT NOT NULL,
    managed_portfolio_id TEXT NOT NULL REFERENCES exchange_portfolio_roots(portfolio_id),
    venue TEXT NOT NULL CHECK(venue='binance-spot'),
    environment TEXT NOT NULL CHECK(environment='testnet'),
    symbol TEXT NOT NULL CHECK(symbol='BTCUSDT'),
    side TEXT NOT NULL CHECK(side='SELL'),
    order_type TEXT NOT NULL CHECK(order_type='MARKET'),
    predecessor_portfolio_version INTEGER NOT NULL CHECK(predecessor_portfolio_version>=1),
    predecessor_portfolio_digest TEXT NOT NULL CHECK(length(predecessor_portfolio_digest)=64),
    exact_managed_quantity TEXT NOT NULL,
    expected_submission_count INTEGER NOT NULL CHECK(expected_submission_count=1),
    permitted_submission_count INTEGER NOT NULL
        CHECK(permitted_submission_count=2),
    control_generation INTEGER NOT NULL CHECK(control_generation>=0),
    reconciliation_generation INTEGER NOT NULL CHECK(reconciliation_generation>0),
    settlement_state TEXT NOT NULL CHECK(settlement_state='SYNCHRONIZED'),
    settlement_state_digest TEXT NOT NULL CHECK(length(settlement_state_digest)=64),
    source_buy_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    source_buy_trade_id TEXT NOT NULL,
    operator_run_id TEXT NOT NULL,
    economic_slot_key TEXT NOT NULL UNIQUE CHECK(length(economic_slot_key)=64),
    policy_version TEXT NOT NULL CHECK(policy_version='stage4-managed-full-exit/v1'),
    rules_fingerprint TEXT NOT NULL CHECK(length(rules_fingerprint)=64),
    risk_fingerprint TEXT NOT NULL CHECK(length(risk_fingerprint)=64),
    config_fingerprint TEXT NOT NULL CHECK(length(config_fingerprint)=64),
    ownership_scope_digest TEXT NOT NULL CHECK(length(ownership_scope_digest)=64),
    risk_envelope_payload TEXT NOT NULL,
    risk_envelope_digest TEXT NOT NULL CHECK(length(risk_envelope_digest)=64),
    consumed_durable_command_key TEXT NOT NULL UNIQUE
        REFERENCES exchange_orders(durable_command_key) DEFERRABLE INITIALLY DEFERRED,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
    consumed_at_ms INTEGER NOT NULL CHECK(consumed_at_ms>=created_at_ms),
    status TEXT NOT NULL CHECK(status='CONSUMED'),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
    UNIQUE(operator_run_id,execution_root_id),
    CHECK(source_buy_command_key<>consumed_durable_command_key)
);

CREATE TABLE exchange_managed_sell_authority_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    authority_id TEXT NOT NULL REFERENCES exchange_managed_sell_authorities(authority_id),
    event_ordinal INTEGER NOT NULL CHECK(event_ordinal=1),
    event_type TEXT NOT NULL CHECK(event_type='CONSUMED'),
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    operator_run_id TEXT NOT NULL,
    occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms>=0),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
    UNIQUE(authority_id,event_ordinal)
);

CREATE TRIGGER exchange_managed_sell_authorities_no_update
BEFORE UPDATE ON exchange_managed_sell_authorities BEGIN
    SELECT RAISE(ABORT,'exchange managed SELL authorities are append-only');
END;
CREATE TRIGGER exchange_managed_sell_authorities_no_delete
BEFORE DELETE ON exchange_managed_sell_authorities BEGIN
    SELECT RAISE(ABORT,'exchange managed SELL authorities are append-only');
END;
CREATE TRIGGER exchange_managed_sell_authority_events_no_update
BEFORE UPDATE ON exchange_managed_sell_authority_events BEGIN
    SELECT RAISE(ABORT,'exchange managed SELL authority events are append-only');
END;
CREATE TRIGGER exchange_managed_sell_authority_events_no_delete
BEFORE DELETE ON exchange_managed_sell_authority_events BEGIN
    SELECT RAISE(ABORT,'exchange managed SELL authority events are append-only');
END;
