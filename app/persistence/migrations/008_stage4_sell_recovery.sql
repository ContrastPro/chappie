CREATE TABLE exchange_managed_sell_recovery_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recovery_event_id TEXT NOT NULL UNIQUE CHECK(length(recovery_event_id)=64),
    authority_id TEXT NOT NULL REFERENCES exchange_managed_sell_authorities(authority_id),
    event_ordinal INTEGER NOT NULL CHECK(event_ordinal IN (1,2)),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'SELL_RECOVERY_REQUIRED','RECOVERY_CONTINUATION_ALLOWED','DISPOSITION_REQUIRED'
    )),
    durable_command_key TEXT NOT NULL REFERENCES exchange_orders(durable_command_key),
    client_order_id TEXT NOT NULL,
    economic_slot_key TEXT NOT NULL CHECK(length(economic_slot_key)=64),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state='PREPARED_BLOCKED'),
    block_reason_code TEXT NOT NULL,
    reason_set_digest TEXT NOT NULL CHECK(length(reason_set_digest)=64),
    snapshot_stage TEXT NOT NULL CHECK(snapshot_stage IN ('SNAPSHOT_B','FINAL_GATE','RECOVERY')),
    snapshot_digest TEXT NULL CHECK(snapshot_digest IS NULL OR length(snapshot_digest)=64),
    rules_fingerprint TEXT NOT NULL CHECK(length(rules_fingerprint)=64),
    portfolio_id TEXT NOT NULL,
    portfolio_version INTEGER NOT NULL CHECK(portfolio_version>=1),
    portfolio_digest TEXT NOT NULL CHECK(length(portfolio_digest)=64),
    session_id TEXT NOT NULL,
    session_submission_count INTEGER NOT NULL CHECK(session_submission_count>=0),
    reconciliation_generation INTEGER NOT NULL CHECK(reconciliation_generation>0),
    reconciliation_status TEXT NOT NULL,
    control_generation INTEGER NOT NULL CHECK(control_generation>=0),
    risk_fingerprint TEXT NOT NULL CHECK(length(risk_fingerprint)=64),
    config_fingerprint TEXT NOT NULL CHECK(length(config_fingerprint)=64),
    occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms>=0),
    policy_version TEXT NOT NULL CHECK(policy_version='stage4-sell-recovery/v1'),
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
    UNIQUE(authority_id,event_ordinal),
    CHECK((event_ordinal=1 AND event_type='SELL_RECOVERY_REQUIRED') OR
          (event_ordinal=2 AND event_type IN ('RECOVERY_CONTINUATION_ALLOWED','DISPOSITION_REQUIRED')))
);

CREATE TRIGGER exchange_managed_sell_recovery_events_no_update
BEFORE UPDATE ON exchange_managed_sell_recovery_events BEGIN
    SELECT RAISE(ABORT,'managed SELL recovery events are append-only');
END;
CREATE TRIGGER exchange_managed_sell_recovery_events_no_delete
BEFORE DELETE ON exchange_managed_sell_recovery_events BEGIN
    SELECT RAISE(ABORT,'managed SELL recovery events are append-only');
END;
