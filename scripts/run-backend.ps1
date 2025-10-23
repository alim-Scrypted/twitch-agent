Param(
    [string]$Port = "8000",
    [string]$AdminKey = "changeme"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$env:ADMIN_KEY = $AdminKey

$pythonCandidates = @(
  ".\.venv-backend\Scripts\python.exe",
  ".\.venv\Scripts\python.exe",
  ".\.venv-orch\Scripts\python.exe",
  ".\.venv-runner\Scripts\python.exe"
)

$python = $null
$pyArgs = @()
foreach ($p in $pythonCandidates) {
  if (Test-Path $p) { $python = $p; break }
}
if (-not $python) {
  if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py"; $pyArgs = @("-3") }
  elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
  else { Write-Error "No Python found. Install Python 3.10+."; exit 1 }
}

$env:PYTHONPATH = "$PWD\backend"
& $python @pyArgs -m uvicorn backend.main:app --reload --port $Port

