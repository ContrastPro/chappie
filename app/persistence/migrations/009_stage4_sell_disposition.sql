CREATE TABLE exchange_managed_sell_dispositions (
    disposition_id TEXT PRIMARY KEY CHECK(length(disposition_id)=64),
    authority_id TEXT NOT NULL UNIQUE REFERENCES exchange_managed_sell_authorities(authority_id),
    durable_command_key TEXT NOT NULL UNIQUE REFERENCES exchange_orders(durable_command_key),
    client_order_id TEXT NOT NULL,
    economic_slot_key TEXT NOT NULL CHECK(length(economic_slot_key)=64),
    exact_quantity TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    portfolio_version INTEGER NOT NULL CHECK(portfolio_version>=1),
    portfolio_digest TEXT NOT NULL CHECK(length(portfolio_digest)=64),
    session_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms>=0),
    policy_version TEXT NOT NULL CHECK(policy_version='stage4-sell-disposition/v1'),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64)
);

CREATE TRIGGER exchange_managed_sell_dispositions_no_update
BEFORE UPDATE ON exchange_managed_sell_dispositions BEGIN
    SELECT RAISE(ABORT,'managed SELL dispositions are append-only');
END;
CREATE TRIGGER exchange_managed_sell_dispositions_no_delete
BEFORE DELETE ON exchange_managed_sell_dispositions BEGIN
    SELECT RAISE(ABORT,'managed SELL dispositions are append-only');
END;
