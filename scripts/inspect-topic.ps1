<#
.SYNOPSIS
  Prints the records of a topic with key, headers, partition and offset, using the Kafka console consumer.

.EXAMPLE
  .\scripts\inspect-topic.ps1 -Topic delivery.file
  .\scripts\inspect-topic.ps1 -Topic delivery.file.dlq -Max 5 -TimeoutMs 5000

.DESCRIPTION
  Reads from the beginning by default and stops after -TimeoutMs without new records.
  The consumer then logs "ERROR ... TimeoutException" to stderr; that is its normal way of stopping.
  Output per record (tab separated): Partition, Offset, headers (NO_HEADERS when none), key, value.
  The console consumer uses its own random group id, so it does not affect the Spark jobs
  (they track offsets in their checkpoints, not in consumer groups).
#>
param(
    [Parameter(Mandatory = $true)][string]$Topic,
    [int]$Max = 20,
    [int]$TimeoutMs = 10000,
    [switch]$FromEnd
)

# Continue: the consumer writes progress lines to stderr, which Windows PowerShell 5.1 would treat as errors.
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path -Parent $PSScriptRoot)

[string[]]$startArgs = @()
if (-not $FromEnd) { $startArgs += '--from-beginning' }

docker compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh `
    --bootstrap-server localhost:9092 `
    --topic $Topic `
    @startArgs `
    --max-messages $Max `
    --timeout-ms $TimeoutMs `
    --property print.key=true `
    --property print.headers=true `
    --property print.partition=true `
    --property print.offset=true
# Exit code 1 from the consumer on timeout with no records is normal; only report real failures.
if ($LASTEXITCODE -gt 1) { throw "Console consumer failed (exit $LASTEXITCODE)." }
