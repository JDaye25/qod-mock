param(
  [string]$BaseUrl = "http://localhost:8010"
)

$ErrorActionPreference = "Stop"

Write-Host "=== QoD Full Flow Smoke Test ==="

function Invoke-JsonPost {
  param(
    [string]$Url,
    [object]$Body
  )
  $json = $Body | ConvertTo-Json -Compress -Depth 10
  return Invoke-RestMethod -Method Post -Uri $Url -ContentType "application/json" -Body $json
}

# 1) Readiness check
$ready = Invoke-RestMethod -Method Get -Uri "$BaseUrl/ready"
if ($ready.status -ne "ok") { throw "Ready check failed: $($ready | ConvertTo-Json -Compress)" }
Write-Host "Ready OK"

# 2) Create intent
$intentBody = @{
  text = "smoke test run"
  target_p95_latency_ms = 120
  target_jitter_ms = 10
  duration_s = 60
  flow_label = "smoke-flow"
}

$intentResp = Invoke-JsonPost -Url "$BaseUrl/intent" -Body $intentBody

$SID = $intentResp.session_id
if ([string]::IsNullOrWhiteSpace($SID)) { throw "No session_id returned from /intent. Response: $($intentResp | ConvertTo-Json -Compress)" }

Write-Host "Session created: $SID"

# 3) Post telemetry
$telemetryBody = @{
  session_id = $SID
  n = 100
  p50_ms = 40
  p95_ms = 120
  jitter_ms = 8
  notes = "smoke telemetry"
}

$telemetryResp = Invoke-JsonPost -Url "$BaseUrl/telemetry" -Body $telemetryBody
if ($telemetryResp.status -ne "stored") { throw "Telemetry not stored. Response: $($telemetryResp | ConvertTo-Json -Compress)" }
Write-Host "Telemetry stored"

# 4) Finalize proof
$finalResp = Invoke-RestMethod -Method Post -Uri "$BaseUrl/proof/$SID/finalize"
if ([string]::IsNullOrWhiteSpace($finalResp.this_hash)) { throw "Proof missing this_hash. Response: $($finalResp | ConvertTo-Json -Compress)" }
Write-Host "Proof finalized"

# 5) Confirm artifact exists on host
$artifactMatch = Get-ChildItem ".\artifacts" -Recurse -File | Where-Object { $_.Name -like "*$SID*" }
if (-not $artifactMatch) { throw "Artifact file not found under .\artifacts for SID=$SID" }

Write-Host "Artifact written:"
$artifactMatch | ForEach-Object { Write-Host $_.FullName }

Write-Host "=== SUCCESS ==="
