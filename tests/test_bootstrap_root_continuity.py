"""Process-level proof that native bootstrap hands setup the repository root."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_setup_sh_runs_workspace_cli_from_its_own_root(tmp_path: Path) -> None:
    repository = tmp_path / "checkout"
    repository.mkdir()
    script = repository / "setup.sh"
    shutil.copy2(Path("setup.sh"), script)
    venv_bin = repository / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (repository / ".venv/pyvenv.cfg").write_text("fixture\n", encoding="utf-8")
    _executable(venv_bin / "python", 'printf "%s" "$PWD" > "$ROOT/python-cwd"')
    _executable(venv_bin / "chappie", 'printf "%s" "$PWD" > "$ROOT/chappie-cwd"')
    fake_python = tmp_path / "python3"
    _executable(fake_python, 'test "$1" = "-c"')

    if os.name == "nt":
        source = script.read_text(encoding="utf-8")
        assert 'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)' in source
        assert 'cd "$ROOT"' in source
        assert '"$ROOT/.venv/bin/python" -m pip install -e "$ROOT"' in source
        assert '"$ROOT/.venv/bin/chappie" setup --yes --interactive "$@"' in source
        powershell = Path("setup.ps1").read_text(encoding="utf-8")
        assert "Push-Location $root" in powershell
        assert "Join-Path $root '.venv\\Scripts\\chappie.exe'" in powershell
        return

    subprocess.run(
        ["/bin/sh", str(script)],
        cwd=tmp_path,
        check=True,
        env={**os.environ, "PYTHON": str(fake_python), "ROOT": str(repository)},
        capture_output=True,
        text=True,
    )

    assert (repository / "python-cwd").read_text(encoding="utf-8") == str(repository)
    assert (repository / "chappie-cwd").read_text(encoding="utf-8") == str(repository)
