# Native bootstrap only; the Python setup engine owns Chappie setup semantics.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
try {
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($null -eq $python) { throw 'Chappie requires Python 3.13 or newer. Install it, then re-run setup.ps1.' }
  & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)"
  if ($LASTEXITCODE -ne 0) { throw 'Chappie requires Python 3.13 or newer.' }
  $venvPython = Join-Path $root '.venv\Scripts\python.exe'
  if (-not (Test-Path $venvPython)) {
    & $python.Source -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Chappie virtual environment creation failed.' }
  }
  & $venvPython -m pip install -e $root
  if ($LASTEXITCODE -ne 0) { throw 'Chappie package installation failed.' }
  $setupArgs = @('setup', '--yes', '--interactive') + $args
  $consent = Read-Host 'Allow external AI downloads/installations (Ollama and a selected model)? [y/N]'
  if ($consent -match '^(?i)y(?:es)?$') { $setupArgs += '--allow-external' }
  & (Join-Path $root '.venv\Scripts\chappie.exe') @setupArgs
  $setupExit = $LASTEXITCODE
  if ($setupExit -ne 0) { exit $setupExit }
  Write-Host "Run '$root\.venv\Scripts\chappie.exe status' to inspect Chappie status."
  exit 0
}
catch {
  Write-Error $_
  exit 1
}
