<#
.SYNOPSIS
  Runs the end-to-end test (task 13) against the local Kafka broker.
.DESCRIPTION
  1. Starts the docker compose broker if it is not running and waits until it accepts connections.
  2. Creates the topics delivery.file and delivery.file.dlq (scripts/create-topics.ps1, idempotent).
  3. Runs tests/end_to_end through pytest with the venv interpreter (-m kafka).
  The test generates a new run directory each time: %TEMP%\e2e-<run_id> (or $env:E2E_RUN_ROOT).
  It is kept after the run for inspection. The broker is left running; stop it with `docker compose down`.
.PARAMETER GenerateOnly
  Only generate a run directory (config, landing files, expected.json). No broker, no Spark.
.EXAMPLE
  .\tests\end_to_end\run-end-to-end.ps1
.EXAMPLE
  .\tests\end_to_end\run-end-to-end.ps1 -GenerateOnly
#>
param(
    [switch]$GenerateOnly
)
$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "venv not found at $python. Create it first (see README.md)." }

if ($GenerateOnly) {
    & $python -m tests.end_to_end.generator
    exit $LASTEXITCODE
}

function Test-Broker {
    docker compose exec -T kafka /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 *> $null
    return ($LASTEXITCODE -eq 0)
}

if (-not (Test-Broker)) {
    Write-Host 'Kafka broker not reachable, starting it with docker compose up -d'
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed. Is Docker Desktop running?' }
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-Broker) { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Kafka broker did not become ready within 60 seconds.' }
}
Write-Host 'Kafka broker is ready'

& (Join-Path $repo 'scripts\create-topics.ps1') | Out-Null

& $python -m pytest tests/end_to_end -m kafka --durations=5
exit $LASTEXITCODE
