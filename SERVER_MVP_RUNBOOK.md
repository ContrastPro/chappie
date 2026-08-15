# Chappie Server MVP Runbook

## Scope

This runbook covers the disposable Paper and Binance Spot Testnet MVP only.
LIVE execution is unavailable and must not be enabled. The first server
deployment is Paper-only.

## Server prerequisites and installation

The supported MVP server is a small Linux host managed by `systemd`. Install
Python 3.13, Git, and a `chappie` service account. Clone the selected revision
to `/opt/chappie`, create its virtual environment, and install the project:

```text
sudo useradd --system --home /var/lib/chappie --shell /usr/sbin/nologin chappie
sudo install -d -o chappie -g chappie -m 0750 /var/lib/chappie/{data,logs,archive}
sudo git clone <approved-repository-url> /opt/chappie
sudo chown -R root:chappie /opt/chappie
cd /opt/chappie && python3.13 -m venv .venv
cd /opt/chappie && .venv/bin/pip install .[dev]
sudo install -d -o root -g chappie -m 0750 /etc/chappie
sudo install -o root -g root -m 0644 deploy/systemd/chappie.service /etc/systemd/system/
sudo install -o root -g chappie -m 0640 deploy/chappie.env.example /etc/chappie/chappie.env
sudo systemctl daemon-reload
```

`/var/lib/chappie/data`, `/var/lib/chappie/logs`, and
`/var/lib/chappie/archive` are the normal runtime directories. They are owned
by `chappie`; `/etc/chappie/chappie.env` is secret-bearing and must remain
`root:chappie` with mode `0640`. The tracked template has placeholders only.

The service unit pins the working directory and virtualenv interpreter,
restarts only failures after ten seconds, and limits restart bursts to three in
five minutes. It sends `SIGTERM`, which Chappie handles as graceful shutdown.

## Active Testnet schema contract

The continuous Testnet MVP uses exchange schema **v4** (`001_initial.sql` through
`004_exchange_settlement.sql`). It is the minimum schema that provides durable
orders, reconciliation, authoritative-trade settlement, and a managed Portfolio.

Migrations v5–v10 are historical M4.10/Stage 4 experiment support. They are not
required for normal MVP startup or trading cycles.

## Database locations and reset policy

- Paper: set `SQLITE_PATH`, for example `data/chappie-paper.sqlite3`.
- Testnet: set a separate `SQLITE_PATH`, for example
  `data/testnet_mvp.sqlite3`. This is the active Testnet default when no
  operator override is supplied.
- Never point Paper and Testnet at the same database file.
- Normal MVP startup never reads `data/m4_10_testnet_smoke.sqlite3`.

Paper and Testnet databases are disposable development artifacts. On an
unexpected execution state, safe-stop first. Archive the database and logs only
when they help diagnose the issue; otherwise stop the process, remove the
specific database and its SQLite sidecars, and start with a fresh database.
Never manually repair financial rows to continue a run.

Future LIVE durability requires a separately designed and audited policy.

## Paper startup

Configure a dedicated Paper database and set:

```text
TRADING_MODE=paper
PAPER_TRADING_ENABLED=true
PERSISTENCE_ENABLED=true
SQLITE_PATH=data/chappie-paper.sqlite3
```

Start with `python -m app.main`. The process polls continuously unless
`SMOKE_RUN_CYCLES` is set for a bounded diagnostic run.

For the first server soak, retain the Paper-first values in
`/etc/chappie/chappie.env`, then run:

```text
sudo systemctl enable --now chappie
systemctl status chappie
journalctl -u chappie -f
cat /var/lib/chappie/logs/chappie-status.json
```

Choose and record a bounded soak period before considering Testnet operation.
During the soak, `chappie-status.json` must remain `READY`, show a recent last
successful cycle, and have no `safe_stop_reason`. On `SAFE_STOP`, stop the
service, preserve artifacts only when useful, investigate, and reset the
disposable Paper database rather than repairing rows manually.

## Testnet startup

Use a dedicated Testnet database and explicitly configure:

```text
TRADING_MODE=testnet
TESTNET_EXECUTION_ENABLED=true
PERSISTENCE_ENABLED=true
SQLITE_PATH=data/testnet_mvp.sqlite3
```

Inject the dedicated Testnet API key, secret, and account fingerprint through
the server's secret mechanism. Do not commit them or log them. Startup performs
the bounded reconciliation/readiness checks before it can submit anything.
On first run, the selected disposable database receives only the active v4
schema plus the normal execution root, session, and managed Portfolio state.
Any startup safety-check failure enters `SAFE_STOP` with submission disabled.

Testnet is not enabled by the installed Paper service. A bounded Testnet
startup requires a separately reviewed operator change to the secured
environment file, a fresh `testnet_mvp.sqlite3`, and credentials provided only
through that file or the server secret mechanism. Confirm schema v4,
reconciliation/readiness, and `READY` status before allowing any market cycle.
The current active Testnet runtime does not provide a distinct persistent
"readiness-only" mode; do not enable it as an unattended server service until
a separate explicit order-authorization step has been approved.

## SAFE_STOP

Any unexpected Testnet execution-consumer failure enters `SAFE_STOP` for that
process. Later market events are ignored by the Testnet coordinator and cannot
produce another submission. There is no automatic retry or in-process restart.

After SAFE_STOP:

1. Stop or let the supervisor stop the process.
2. Preserve logs and the database only if they are useful for diagnosis.
3. Inspect the sanitized failure category and durable order state.
4. Reset the disposable Testnet database when investigation is complete.
5. Start a fresh process; never manually resume the old order.

## Logs and basic operations

### Paper monitor

Use the read-only local terminal dashboard while Paper is running:

```text
python -m app.tools.paper_monitor
python -m app.tools.paper_monitor --once
python -m app.tools.paper_monitor --interval 2
```

The monitor opens the active SQLite database read-only and reads the atomically
published status JSON. It never creates orders, changes Portfolio state, writes
to SQLite, or requires credentials; it is safe to run beside `python -m app.main`.

`RUN_LOG_FILE` defaults to `logs/latest_run.log`; configure persistent log
rotation in the process manager. Monitor the last successful market cycle,
coordinator lifecycle, Testnet readiness, reconciliation status, settlement
status, and any `SAFE_STOP` record.

`SERVER_STATUS_FILE` defaults to `logs/chappie-status.json`. It is atomically
published with the mode, process state (`STARTING`, `READY`, `SAFE_STOP`, or
`STOPPED`), active schema, polling/reconciliation/settlement state, submission
availability, and a sanitized safe-stop reason. It contains no secrets or
private exchange payloads.

Send `SIGINT` or `SIGTERM` for graceful shutdown. Chappie disables decision
delivery, requests polling stop, closes persistence and network resources, then
publishes `STOPPED`.

Basic server checks are:

```text
systemctl is-active chappie
systemctl show chappie -p NRestarts
stat /var/lib/chappie/logs/chappie-status.json
cat /var/lib/chappie/logs/chappie-status.json
df -h /var/lib/chappie
du -sh /var/lib/chappie/logs /var/lib/chappie/data
```

Treat a stale status file, missing recent successful cycle, `SAFE_STOP`, low
disk space, unexpectedly growing logs, or missing database as an operator
alert. Use `sudo systemctl restart chappie` only after diagnosing the prior
state; restart storms are deliberately rate-limited by the unit.

## Archive, reset, update, and rollback

Stop first. To retain diagnostics, make a timestamped archive under
`/var/lib/chappie/archive` containing the selected database, its `-wal`/`-shm`
sidecars when present, current log, and status JSON. Otherwise remove only the
specific disposable database and its sidecars, then start fresh. Never perform
manual SQL repair.

```text
sudo systemctl stop chappie
sudo -u chappie mkdir -p /var/lib/chappie/archive/<timestamp>
sudo -u chappie mv /var/lib/chappie/data/chappie-paper.sqlite3* /var/lib/chappie/archive/<timestamp>/
sudo systemctl start chappie
```

For an approved code update, stop the service, check out the approved commit,
reinstall dependencies if project metadata changed, and start it. Roll back by
checking out the previously recorded Git commit and repeating the controlled
start. Keep the database reset/archive decision explicit; code rollback does
not imply manual financial-state repair.

The MVP does not provide a LIVE mode, automatic recovery ceremony, cancellation
workflow, or unattended repair of inconsistent Testnet state.
