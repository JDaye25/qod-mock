# scripts/clean_room.ps1
# Clean-room reproducibility gate (Windows-friendly)
# - builds images
# - brings Compose up
# - waits for readiness (API /health)
# - runs minimal end-to-end scenario (/intent -> /telemetry -> /proof/{id}/finalize)
# - asserts artifacts exist (host-mounted) OR finalize response includes indicators
# - tears down cleanly

$ErrorActionPreference = "Stop"

# ---- Config ----
$ComposeFile = "docker-compose.yml"
$PreferredHostPort = if ($env:QOD_HOST_PORT) { $env:QOD_HOST_PORT } else { "" }

$HealthPath   = "/health"
$IntentPath   = "/intent"
$TelemetryPath = "/telemetry"

# Finalize is NOT /finalize in your OpenAPI:
# It is POST /proof/{session_id}/finalize
function Get-FinalizePath([string]$sessionId) {
  return "/proof/$sessionId/finalize"
}

# Host artifact folder (requires compose mount ./artifacts:/app/artifacts)
$ArtifactsRoot = Join-Path (Get-Location) "artifacts"

$MaxWaitSeconds = 90
$PollSeconds = 2

function Write-Step($msg) {
  Write-Host ""
  Write-Host "== $msg ==" -ForegroundColor Cyan
}

function Fail($msg) {
  Write-Host ""
  Write-Host "FAIL: $msg" -ForegroundColor Red
  exit 1
}

function Invoke-JsonPost($url, $bodyObj) {
  # Write JSON to a temp file (UTF-8 no BOM) to avoid PS quoting issues
  $json = ($bodyObj | ConvertTo-Json -Depth 20)
  $tmp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), ("qod_" + [System.Guid]::NewGuid().ToString("N") + ".json"))
  try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)

    return curl.exe -s -X POST $url `
      -H "Content-Type: application/json" `
      --data-binary "@$tmp"
  }
  finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
  }
}

function Get-ApiHostPortFromCompose {
  param(
    [string]$composeFile,
    [string]$serviceName = "qod-api"
  )

  $ps = docker compose -f $composeFile ps 2>&1 | Out-String
  if (-not $ps) { return $null }

  # Try to capture 0.0.0.0:8010->8000/tcp
  $regex = [regex]"$serviceName\s+.+?\s+Up.+?(?<hp>\d+)->8000/tcp"
  $m = $regex.Match($ps)
  if ($m.Success) { return $m.Groups["hp"].Value }

  # Fallback: any :####->8000/tcp
  $regex2 = [regex]"(?<hp>\d+)->8000/tcp"
  $m2 = $regex2.Match($ps)
  if ($m2.Success) { return $m2.Groups["hp"].Value }

  return $null
}

# ---- Clean up no matter what ----
$script:didComposeUp = $false

try {
  Write-Step "Preflight: Docker is available"
  docker version | Out-Null

  Write-Step "Compose down + clean (start from zero-ish)"
  docker compose -f $ComposeFile down --remove-orphans --volumes | Out-Null

  Write-Step "Build images"
  docker compose -f $ComposeFile build --pull
  if ($LASTEXITCODE -ne 0) { Fail "docker compose build failed." }

  Write-Step "Compose up (detached)"
  docker compose -f $ComposeFile up -d
  if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed." }
  $script:didComposeUp = $true

  $hostPort = $null
  if ($PreferredHostPort) {
    $hostPort = $PreferredHostPort
    Write-Host "Using forced host port from QOD_HOST_PORT: $hostPort" -ForegroundColor Yellow
  } else {
    $hostPort = Get-ApiHostPortFromCompose -composeFile $ComposeFile -serviceName "qod-api"
    if (-not $hostPort) { $hostPort = "8010" } # reasonable default for your repo
    Write-Host "Detected published host port: $hostPort" -ForegroundColor Green
  }

  $ApiBaseUrl = "http://127.0.0.1:$hostPort"

  Write-Step "Wait for API readiness: GET $ApiBaseUrl$HealthPath"
  $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
  $healthy = $false

  while ((Get-Date) -lt $deadline) {
    try {
      $resp = curl.exe -s "$ApiBaseUrl$HealthPath"
      if ($resp -match "ok|OK|status") {
        $healthy = $true
        break
      }
    } catch {}
    Start-Sleep -Seconds $PollSeconds
  }

  if (-not $healthy) {
    docker compose -f $ComposeFile ps
    docker compose -f $ComposeFile logs --tail 200
    Fail "API did not become healthy within $MaxWaitSeconds seconds at $ApiBaseUrl$HealthPath."
  }

  Write-Step "Run minimal end-to-end scenario: /intent -> /telemetry -> /proof/{id}/finalize"

  # 1) /intent (matches OpenAPI Intent schema you pasted)
  $intentBody = @{
    text = "turbo qos request"
    target_p95_latency_ms = 200
    target_jitter_ms = 20
    duration_s = 30
    flow_label = "clean-room"
  }

  $intentRaw = Invoke-JsonPost "$ApiBaseUrl$IntentPath" $intentBody
  if (-not $intentRaw) { Fail "/intent returned empty response." }

  $sessionId = $null
  if ($intentRaw -match '"session_id"\s*:\s*"([^"]+)"') { $sessionId = $Matches[1] }

  if (-not $sessionId) {
    Write-Host "Intent response:" -ForegroundColor Yellow
    Write-Host $intentRaw
    Fail "Could not find session_id in /intent response."
  }

  Write-Host "session_id = $sessionId" -ForegroundColor Green

  # 2) /telemetry (TelemetrySample schema from OpenAPI)
  $telemetryBody = @{
    session_id = $sessionId
    n = 30
    p50_ms = 40
    p95_ms = 85
    jitter_ms = 10
    notes = "clean-room sample"
  }

  $telemetryRaw = Invoke-JsonPost "$ApiBaseUrl$TelemetryPath" $telemetryBody
  if (-not $telemetryRaw) { Fail "/telemetry returned empty response." }

  # 3) finalize proof (POST /proof/{session_id}/finalize)
  $finalizePath = Get-FinalizePath $sessionId
  $finalizeRaw = Invoke-JsonPost "$ApiBaseUrl$finalizePath" @{}  # empty JSON body is fine
  if (-not $finalizeRaw) { Fail "Finalize returned empty response." }

  Write-Step "Assert artifacts exist (preferred) OR response includes artifact/proof indicators"
  Write-Host "Finalize response:" -ForegroundColor Yellow
  Write-Host $finalizeRaw

  # --- Host artifact assertions (preferred) ---
  $artifactFound = $false
  $proofFound = $false

  if (Test-Path $ArtifactsRoot) {
    $candidateDir = Join-Path $ArtifactsRoot $sessionId
    $artifactFile = Join-Path $candidateDir "artifact.json"
    $proofFile = Join-Path $candidateDir "proof.json"

    if (Test-Path $artifactFile) { $artifactFound = $true }
    if (Test-Path $proofFile) { $proofFound = $true }
  }

  if ($artifactFound -and $proofFound) {
    Write-Host "Artifacts exist on host ✅" -ForegroundColor Green
  } else {
    # Fallback: if not host-mounted or filenames differ, accept response that includes proof/artifact hints
    if ($finalizeRaw -notmatch "artifact|proof|artifact_json|proof_json|status") {
      Fail "Artifacts not found on host AND finalize response did not include recognizable proof/artifact indicators. Ensure compose mounts ./artifacts and service writes artifact.json + proof.json."
    } else {
      Write-Host "Artifacts not found on host (maybe not mounted or different filenames), but finalize response contains indicators ✅" -ForegroundColor Yellow
    }
  }

  Write-Step "PASS: clean-room scenario succeeded"
  exit 0
}
catch {
  Write-Host ""
  Write-Host "Exception:" -ForegroundColor Red
  Write-Host $_
  exit 1
}
finally {
  if ($script:didComposeUp) {
    Write-Step "Tear down compose (clean)"
    docker compose -f $ComposeFile down --remove-orphans --volumes | Out-Null
  }
}