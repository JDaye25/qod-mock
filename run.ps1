# run.ps1 — canonical run command for this repo (Windows)

$ErrorActionPreference = "Stop"

Write-Host "== QoD: blessed run entrypoint =="

# 1) Ensure virtualenv exists
if (-not (Test-Path ".\.venv")) {
  Write-Host "Creating venv at .\.venv ..."
  py -m venv .venv
}

# 2) Activate venv
Write-Host "Activating venv ..."
. .\.venv\Scripts\Activate.ps1

# 3) Install dependencies
Write-Host "Installing dependencies from requirements.txt ..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 4) Load .env into environment (local dev convenience)
if (Test-Path ".\.env") {
  Write-Host "Loading .env ..."
  Get-Content .\.env | ForEach-Object {
    if ($_ -match "^\s*#") { return }
    if ($_ -match "^\s*$") { return }
    $pair = $_ -split "=", 2
    if ($pair.Count -eq 2) {
      $name = $pair[0].Trim()
      $value = $pair[1].Trim()
      if (-not [string]::IsNullOrWhiteSpace($name)) {
        Set-Item -Path "env:$name" -Value $value
      }
    }
  }
}

# 5) Require env vars (don't print secrets!)
$requiredVars = @("QOD_CLIENT_SECRET")
$missing = @()

foreach ($v in $requiredVars) {
  $val = (Get-Item -Path "env:$v" -ErrorAction SilentlyContinue).Value
  if ([string]::IsNullOrWhiteSpace($val)) { $missing += $v }
}

if ($missing.Count -gt 0) {
  Write-Host ""
  Write-Host "Missing required environment variables:" -ForegroundColor Yellow
  $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }

  Write-Host ""
  Write-Host "Set them in your .env file (recommended) or manually like:" -ForegroundColor Cyan
  Write-Host '  $env:QOD_CLIENT_SECRET="your_secret_here"' -ForegroundColor Cyan
  Write-Host ""
  Write-Host "Then run again:" -ForegroundColor Cyan
  Write-Host "  .\run.ps1" -ForegroundColor Cyan

  exit 1
}

# 6) Start server
$port = if ($args.Count -ge 1) { [int]$args[0] } else { 8010 }

Write-Host "Starting API server at http://127.0.0.1:$port ..."
python -m uvicorn backend.main:app --reload --port $port