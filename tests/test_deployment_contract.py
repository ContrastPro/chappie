"""Static deployment contract checks for the active MVP server surface."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

from app.foundation.config import Settings

ROOT = Path(__file__).parents[1]
SERVICE_FILE = ROOT / "deploy/systemd/chappie.service"
ENV_TEMPLATE = ROOT / "deploy/chappie.env.example"


def test_systemd_unit_has_a_bounded_graceful_service_contract() -> None:
    parser = ConfigParser(interpolation=None)
    parser.read(SERVICE_FILE)

    assert parser["Service"]["workingdirectory"] == "/opt/chappie"
    assert parser["Service"]["environmentfile"] == "/etc/chappie/chappie.env"
    assert parser["Service"]["execstart"] == "/opt/chappie/.venv/bin/python -m app.main"
    assert parser["Service"]["restart"] == "on-failure"
    assert parser["Service"]["restartsec"] == "10s"
    assert parser["Service"]["killsignal"] == "SIGTERM"
    assert parser["Unit"]["startlimitburst"] == "3"
    assert parser["Install"]["wantedby"] == "multi-user.target"


def test_server_environment_template_is_paper_first_and_secret_free() -> None:
    contents = ENV_TEMPLATE.read_text()

    assert "TRADING_MODE=paper" in contents
    assert "TESTNET_EXECUTION_ENABLED=false" in contents
    assert "SQLITE_PATH=/var/lib/chappie/data/chappie-paper.sqlite3" in contents
    assert "SQLITE_PATH=/var/lib/chappie/data/testnet_mvp.sqlite3" in contents
    assert "data/m4_10_testnet_smoke.sqlite3" not in contents
    assert "TRADING_MODE=live" not in [
        line.strip() for line in contents.splitlines() if not line.lstrip().startswith("#")
    ]
    assert "__SET_IN_SECRET_MANAGER__" in contents
    assert "__SET_OPERATOR_ACCOUNT_FINGERPRINT__" in contents


def test_server_paths_parse_without_sharing_paper_and_testnet_databases() -> None:
    paper = Settings(_env_file=ENV_TEMPLATE)

    assert paper.trading_mode == "paper"
    assert paper.paper_trading_enabled
    assert not paper.testnet_execution_enabled
    assert paper.sqlite_path == Path("/var/lib/chappie/data/chappie-paper.sqlite3")
    assert paper.server_status_file == Path("/var/lib/chappie/logs/chappie-status.json")
