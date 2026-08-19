from pathlib import Path

from app import cli
from app.ai_setup_smoke import ChappieAISetupSmokeV1
from app.bootstrap import ChappiePaths
from app.project_qa_setup_smoke import ProjectQASetupSmokeV1
from app.windows_ollama import WindowsOllamaInstallerV1


def _paths(tmp_path: Path) -> ChappiePaths:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    return ChappiePaths.resolve(root)


def test_windows_cli_constructs_production_executor(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    engine = cli._setup_engine(_paths(tmp_path))
    assert isinstance(engine._windows_installer, WindowsOllamaInstallerV1)
    assert isinstance(engine._ollama_readiness_coordinator, cli.OllamaReadinessCoordinatorV1)
    assert isinstance(engine._smoke, ChappieAISetupSmokeV1)
    assert isinstance(engine._project_qa_smoke, ProjectQASetupSmokeV1)


def test_non_windows_cli_does_not_construct_windows_executor(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    engine = cli._setup_engine(_paths(tmp_path))
    assert engine._windows_installer is None
    assert engine._ollama_readiness_coordinator is None
