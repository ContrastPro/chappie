CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL CHECK(length(checksum) = 64),
    applied_at_ms INTEGER NOT NULL CHECK(applied_at_ms >= 0)
);

CREATE TABLE paper_decisions (
    stable_decision_key TEXT PRIMARY KEY CHECK(length(stable_decision_key) = 64),
    decision_slot_key TEXT NOT NULL UNIQUE CHECK(length(decision_slot_key) = 64),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    terminal_outcome TEXT NULL CHECK(terminal_outcome IN
        ('HOLD','PLANNER_DECLINED','RISK_REJECTED','NOT_EXECUTED','EXECUTED'))
);

CREATE TABLE portfolio_roots (
    portfolio_id TEXT PRIMARY KEY,
    state_payload TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64),
    config_fingerprint TEXT NOT NULL CHECK(length(config_fingerprint) = 64),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0)
);

CREATE TABLE paper_commands (
    durable_command_key TEXT PRIMARY KEY CHECK(length(durable_command_key) = 64),
    stable_decision_key TEXT NOT NULL UNIQUE REFERENCES paper_decisions(stable_decision_key),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64)
);

CREATE TABLE fills (
    fill_id TEXT PRIMARY KEY,
    durable_command_key TEXT NOT NULL UNIQUE REFERENCES paper_commands(durable_command_key),
    order_id TEXT NOT NULL,
    risk_decision_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    gross_notional TEXT NOT NULL,
    fee TEXT NOT NULL,
    fee_asset TEXT NOT NULL,
    net_quote_change TEXT NOT NULL,
    net_base_change TEXT NOT NULL,
    filled_at_ms INTEGER NOT NULL CHECK(filled_at_ms >= 0),
    dust_liquidation INTEGER NOT NULL CHECK(dust_liquidation IN (0,1)),
    correlation_id TEXT NULL,
    model_version TEXT NOT NULL
);

CREATE TABLE journal_entries (
    journal_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_type TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    stable_decision_key TEXT NOT NULL REFERENCES paper_decisions(stable_decision_key),
    durable_command_key TEXT NULL REFERENCES paper_commands(durable_command_key),
    fill_id TEXT NULL REFERENCES fills(fill_id),
    portfolio_version_before INTEGER NOT NULL CHECK(portfolio_version_before >= 0),
    portfolio_version_after INTEGER NOT NULL CHECK(portfolio_version_after = portfolio_version_before + 1),
    before_digest TEXT NOT NULL CHECK(length(before_digest) = 64),
    after_state_payload TEXT NOT NULL,
    after_state_digest TEXT NOT NULL CHECK(length(after_state_digest) = 64),
    domain_timestamp_ms INTEGER NOT NULL CHECK(domain_timestamp_ms >= 0),
    model_version TEXT NOT NULL,
    UNIQUE(portfolio_id, portfolio_version_after)
);

CREATE TABLE portfolio_current (
    portfolio_id TEXT PRIMARY KEY,
    portfolio_version INTEGER NOT NULL CHECK(portfolio_version >= 0),
    state_payload TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64),
    last_journal_seq INTEGER NULL REFERENCES journal_entries(journal_seq),
    CHECK((portfolio_version = 0 AND last_journal_seq IS NULL) OR
          (portfolio_version > 0 AND last_journal_seq IS NOT NULL))
);

CREATE TABLE portfolio_checkpoints (
    portfolio_id TEXT NOT NULL,
    portfolio_version INTEGER NOT NULL CHECK(portfolio_version >= 0),
    state_payload TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64),
    last_journal_seq INTEGER NOT NULL REFERENCES journal_entries(journal_seq),
    model_version TEXT NOT NULL,
    PRIMARY KEY(portfolio_id, portfolio_version)
);

CREATE TABLE observation_deliveries (
    stable_decision_key TEXT NOT NULL REFERENCES paper_decisions(stable_decision_key),
    observer_name TEXT NOT NULL,
    observer_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('NOT_ATTEMPTED','DELIVERED','FAILED')),
    attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
    error_category TEXT NULL,
    error_message TEXT NULL,
    PRIMARY KEY(stable_decision_key, observer_name, observer_version)
);

CREATE TRIGGER journal_entries_no_update
BEFORE UPDATE ON journal_entries BEGIN
    SELECT RAISE(ABORT, 'journal entries are append-only');
END;

CREATE TRIGGER journal_entries_no_delete
BEFORE DELETE ON journal_entries BEGIN
    SELECT RAISE(ABORT, 'journal entries are append-only');
END;
