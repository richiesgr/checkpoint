<#
.SYNOPSIS
  Creates the built Kafka topics on the local docker compose broker.

.DESCRIPTION
  Creates only the topics this project builds:
    delivery.file      main delivery topic for file destinations
    delivery.file.dlq  dead-letter topic of the File_Sink_Worker

  The designed-only topics (delivery.file.retry, delivery.http*, delivery.syslog*) are NOT created.
  Topics are shared by destination kind. Tenant, product and destination are in the message key and value.

  Partition counts are for local development (single broker, replication factor 1):
    delivery.file      -Partitions (default 3)  enough to see key-based spreading without a heavy local broker
    delivery.file.dlq  -DlqPartitions (default 1)  low volume
  Production sizing is discussed in the design document.

  Safe to run again: existing topics are left unchanged (--if-not-exists).
  Requires the broker from `docker compose up -d`.
#>
param(
    [int]$Partitions = 3,
    [int]$DlqPartitions = 1
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

# Same address the host uses; it is also valid inside the container.
$bootstrap = 'localhost:9092'
$kafkaTopics = '/opt/kafka/bin/kafka-topics.sh'

$topics = @(
    @{ Name = 'delivery.file';     Partitions = $Partitions },
    @{ Name = 'delivery.file.dlq'; Partitions = $DlqPartitions }
)

foreach ($t in $topics) {
    docker compose exec -T kafka $kafkaTopics --bootstrap-server $bootstrap `
        --create --if-not-exists --topic $t.Name --partitions $t.Partitions --replication-factor 1
    if ($LASTEXITCODE -ne 0) { throw "Creating topic $($t.Name) failed (exit $LASTEXITCODE). Is the broker running?" }
}

docker compose exec -T kafka $kafkaTopics --bootstrap-server $bootstrap --describe
if ($LASTEXITCODE -ne 0) { throw "Describing topics failed (exit $LASTEXITCODE)." }
