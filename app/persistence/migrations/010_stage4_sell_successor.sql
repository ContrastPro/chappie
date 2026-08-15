CREATE TABLE exchange_managed_sell_successors (
    successor_authority_id TEXT PRIMARY KEY REFERENCES exchange_managed_sell_authorities(authority_id),
    predecessor_authority_id TEXT NOT NULL UNIQUE REFERENCES exchange_managed_sell_authorities(authority_id),
    predecessor_economic_slot_key TEXT NOT NULL UNIQUE CHECK(length(predecessor_economic_slot_key)=64),
    predecessor_generation INTEGER NOT NULL CHECK(predecessor_generation=0),
    successor_generation INTEGER NOT NULL CHECK(successor_generation=1),
    successor_economic_slot_key TEXT NOT NULL UNIQUE CHECK(length(successor_economic_slot_key)=64),
    successor_durable_command_key TEXT NOT NULL UNIQUE REFERENCES exchange_orders(durable_command_key),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
    policy_version TEXT NOT NULL CHECK(policy_version='stage4-managed-sell-successor/v1'),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64)
);

CREATE TRIGGER exchange_managed_sell_successors_no_update
BEFORE UPDATE ON exchange_managed_sell_successors BEGIN
    SELECT RAISE(ABORT,'managed SELL successors are append-only');
END;
CREATE TRIGGER exchange_managed_sell_successors_no_delete
BEFORE DELETE ON exchange_managed_sell_successors BEGIN
    SELECT RAISE(ABORT,'managed SELL successors are append-only');
END;
