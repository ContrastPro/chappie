CREATE TABLE exchange_trade_settlement_resolutions (
    resolution_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_id TEXT NOT NULL UNIQUE CHECK(length(resolution_id)=64),
    fill_id TEXT NOT NULL UNIQUE CHECK(length(fill_id)=64),
    blocked_settlement_id TEXT NOT NULL UNIQUE REFERENCES exchange_trade_settlements(settlement_id),
    portfolio_id TEXT NOT NULL REFERENCES exchange_portfolio_roots(portfolio_id),
    session_id TEXT NOT NULL REFERENCES exchange_sessions(session_id),
    venue TEXT NOT NULL CHECK(venue='binance-spot'),
    environment TEXT NOT NULL CHECK(environment='testnet'),
    account_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    reconciliation_id TEXT NOT NULL REFERENCES exchange_reconciliation_runs(reconciliation_id),
    reconciliation_generation INTEGER NOT NULL CHECK(reconciliation_generation>0),
    policy_id TEXT NOT NULL CHECK(policy_id='zero-commission-asset-neutral/v1'),
    reason_code TEXT NOT NULL CHECK(reason_code='exact_zero_commission_asset_neutral'),
    portfolio_version_before INTEGER NOT NULL CHECK(portfolio_version_before>=0),
    portfolio_version_after INTEGER NOT NULL CHECK(portfolio_version_after=portfolio_version_before+1),
    before_digest TEXT NOT NULL CHECK(length(before_digest)=64),
    after_state_payload TEXT NOT NULL,
    after_state_digest TEXT NOT NULL CHECK(length(after_state_digest)=64),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
    model_version TEXT NOT NULL CHECK(model_version='exchange-settlement-resolution/v1'),
    record_payload TEXT NOT NULL,
    record_digest TEXT NOT NULL CHECK(length(record_digest)=64),
    UNIQUE(venue,environment,account_fingerprint,symbol,exchange_trade_id),
    UNIQUE(portfolio_id,portfolio_version_after)
);

CREATE TRIGGER exchange_trade_settlement_resolutions_no_update
BEFORE UPDATE ON exchange_trade_settlement_resolutions BEGIN
    SELECT RAISE(ABORT, 'exchange trade settlement resolutions are append-only');
END;

CREATE TRIGGER exchange_trade_settlement_resolutions_no_delete
BEFORE DELETE ON exchange_trade_settlement_resolutions BEGIN
    SELECT RAISE(ABORT, 'exchange trade settlement resolutions are append-only');
END;
