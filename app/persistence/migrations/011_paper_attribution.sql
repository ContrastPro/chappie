CREATE TABLE paper_attributions (
    stable_decision_key TEXT PRIMARY KEY REFERENCES paper_decisions(stable_decision_key),
    decision_slot_key TEXT NOT NULL CHECK(length(decision_slot_key) = 64),
    durable_command_key TEXT NULL REFERENCES paper_commands(durable_command_key),
    cycle_outcome TEXT NOT NULL CHECK(cycle_outcome IN ('HOLD','PLANNER_DECLINED','RISK_REJECTED','NOT_EXECUTED','EXECUTED')),
    portfolio_id TEXT NOT NULL,
    portfolio_version_before INTEGER NOT NULL CHECK(portfolio_version_before >= 0),
    portfolio_version_after INTEGER NOT NULL CHECK(portfolio_version_after >= portfolio_version_before),
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    decision_time_ms INTEGER NOT NULL CHECK(decision_time_ms >= 0),
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    strategy_config_fingerprint TEXT NOT NULL CHECK(length(strategy_config_fingerprint) = 64),
    signal_id TEXT NOT NULL,
    signal_direction TEXT NOT NULL CHECK(signal_direction IN ('buy','sell','hold')),
    signal_reason_code TEXT NOT NULL,
    pipeline_run_id TEXT NOT NULL,
    intent_id TEXT NULL,
    risk_decision_id TEXT NULL,
    risk_outcome TEXT NULL,
    risk_rejection_reasons_json TEXT NOT NULL,
    risk_modification_reasons_json TEXT NOT NULL,
    requested_target_exposure TEXT NULL,
    approved_target_exposure TEXT NULL,
    execution_id TEXT NULL,
    execution_status TEXT NULL,
    fill_id TEXT NULL REFERENCES fills(fill_id),
    observed_paper_fee TEXT NULL,
    gross_notional TEXT NULL,
    before_state_digest TEXT NOT NULL CHECK(length(before_state_digest) = 64),
    after_state_digest TEXT NOT NULL CHECK(length(after_state_digest) = 64),
    attribution_digest TEXT NOT NULL CHECK(length(attribution_digest) = 64)
);

CREATE TRIGGER paper_attributions_no_update
BEFORE UPDATE ON paper_attributions BEGIN
    SELECT RAISE(ABORT, 'paper attributions are append-only');
END;

CREATE TRIGGER paper_attributions_no_delete
BEFORE DELETE ON paper_attributions BEGIN
    SELECT RAISE(ABORT, 'paper attributions are append-only');
END;
