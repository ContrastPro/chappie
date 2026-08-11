CREATE TABLE exchange_reconciliation_current (
    execution_root_id TEXT PRIMARY KEY REFERENCES exchange_execution_roots(execution_root_id),
    status TEXT NOT NULL CHECK(status IN ('REQUIRED','SAFE','FAILED','CONFLICT')),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    latest_reconciliation_id TEXT NULL,
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0),
    model_version TEXT NOT NULL,
    state_payload TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK(length(state_digest) = 64)
);

CREATE TABLE exchange_reconciliation_runs (
    reconciliation_id TEXT PRIMARY KEY,
    execution_root_id TEXT NOT NULL REFERENCES exchange_execution_roots(execution_root_id),
    session_id TEXT NOT NULL REFERENCES exchange_sessions(session_id),
    account_fingerprint TEXT NOT NULL,
    generation_before INTEGER NOT NULL CHECK(generation_before >= 0),
    generation_after INTEGER NOT NULL CHECK(
        generation_after = generation_before OR generation_after = generation_before + 1
    ),
    outcome TEXT NOT NULL CHECK(outcome IN ('REQUIRED','SAFE','FAILED','CONFLICT')),
    orders_examined INTEGER NOT NULL CHECK(orders_examined >= 0),
    orders_advanced INTEGER NOT NULL CHECK(orders_advanced >= 0),
    trades_persisted INTEGER NOT NULL CHECK(trades_persisted >= 0),
    open_orders_examined INTEGER NOT NULL CHECK(open_orders_examined >= 0),
    balances_examined INTEGER NOT NULL CHECK(balances_examined >= 0),
    reasons_payload TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL CHECK(started_at_ms >= 0),
    completed_at_ms INTEGER NOT NULL CHECK(completed_at_ms >= started_at_ms),
    model_version TEXT NOT NULL,
    result_payload TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK(length(result_digest) = 64),
    CHECK(
        (outcome = 'SAFE' AND generation_after = generation_before + 1)
        OR (outcome != 'SAFE' AND generation_after = generation_before)
    )
);

CREATE UNIQUE INDEX exchange_reconciliation_safe_generation
ON exchange_reconciliation_runs(execution_root_id, generation_after)
WHERE outcome = 'SAFE';

CREATE TRIGGER exchange_reconciliation_runs_no_update
BEFORE UPDATE ON exchange_reconciliation_runs BEGIN
    SELECT RAISE(ABORT, 'exchange reconciliation runs are append-only');
END;
CREATE TRIGGER exchange_reconciliation_runs_no_delete
BEFORE DELETE ON exchange_reconciliation_runs BEGIN
    SELECT RAISE(ABORT, 'exchange reconciliation runs are append-only');
END;

CREATE TRIGGER exchange_reconciliation_generation_insert
BEFORE INSERT ON exchange_reconciliation_runs
WHEN NEW.generation_before != (
    SELECT generation FROM exchange_reconciliation_current
    WHERE execution_root_id = NEW.execution_root_id
) BEGIN
    SELECT RAISE(ABORT, 'reconciliation generation is stale');
END;
