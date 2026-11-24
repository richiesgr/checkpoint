# Event forwarding

Forwards tenant events from a landing zone to per-tenant destinations, with tenant isolation as the first design goal.

- **Processing job** (PySpark Structured Streaming): reads `*.jsonl` files, validates and routes events by exact `(tenant_id, product, schema)` rules, shapes the payload and publishes Delivery_Messages to Kafka.
- **File_Sink_Worker** (PySpark Structured Streaming): reads `delivery.file` from Kafka and writes one JSONL file per micro-batch and destination.

Both jobs run with `trigger(availableNow=True)`: they drain what is available and exit.

Design document: `docs/design.md`. AI interaction log: [docs/ai-log/](docs/ai-log/).

## Versions

| Component | Version |
| --- | --- |
| Python | 3.13.9 (venv built from a 3.13.9 interpreter) |
| JDK | 21.0.9 (Spark 4.3 supports 17, 21 or 25) |
| PySpark / Spark | 4.3.0 (Scala 2.13 build) |
| Spark Kafka connector | `org.apache.spark:spark-sql-kafka-0-10_2.13:4.3.0` (always the same version as PySpark) |
| Kafka client library (test side only) | kafka-python 3.0.13 |
| Kafka broker image | `apache/kafka:3.9.1` (single node, KRaft) |
| Docker | Docker Desktop with Compose v2 (`docker compose`) |

Pinned in `requirements.txt`: `pyspark==4.3.0`, `PyYAML==6.0.4`, `kafka-python==3.0.13`.
Pinned in `requirements-dev.txt` (adds `-r requirements.txt`): `pytest==9.2.0`, `hypothesis==6.170.17`.
Other installed packages (`py4j`, `pluggy`, `iniconfig`, `packaging`, `Pygments`, `colorama`, `sortedcontainers`) are transitive.

**Kafka connector.** `event_forwarding/spark_session.py` (the session builder shared by the tests and both CLIs) sets `spark.jars.packages` to the connector above. Spark downloads it from Maven on first use, so the first Kafka run needs network access; later runs use the local Ivy cache. To run offline, download the connector jar and its dependencies and ship them instead (for example with `spark.jars`). Tests that are not marked `kafka` build the session without the connector and need no network.

**Windows.** Spark local mode on Windows needs `HADOOP_HOME` pointing to a folder with `bin\winutils.exe` and `hadoop.dll`. This machine uses `C:\hadoop`. Without it, writes can fail with `UnsatisfiedLinkError` or `winutils.exe` errors.

**Interpreter for Spark.** The CLIs and the test fixture set `PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` to the venv's `python.exe` (`pin_python_env()` in `spark_session.py`), so Spark workers use the venv, not another Python.

## Setup from a fresh clone (PowerShell)

```powershell
# 1. Check the JDK (17, 21 or 25)
java -version

# 2. Create the venv. It must be built from a Python 3.13.9 interpreter.
#    The path below is the one on the development machine and may differ on yours.
#    conda is not used directly (no conda activate/install/create); only its python.exe is called.
& 'C:\Users\Richard\miniconda3\python.exe' -m venv .venv
.\.venv\Scripts\python.exe --version      # must print: Python 3.13.9

# 3. Install the pinned dependencies (only through the venv interpreter)
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Every command below calls `.\.venv\Scripts\python.exe` directly, so no activation is needed.

## Tests

```powershell
# Everything that does not need Docker (Spark local[*], no network). pytest.ini skips the kafka marker by default.
.\.venv\Scripts\python.exe -m pytest

# Slow run: show timings
.\.venv\Scripts\python.exe -m pytest --durations=10

# Tests that need the local Kafka broker (start it first, see below)
.\.venv\Scripts\python.exe -m pytest -m kafka
```

Run one pytest process at a time: each starts its own Spark JVM. Hypothesis tests use 2 examples each, because every example runs a Spark job. Kafka-marked tests skip with a message naming the broker when `localhost:9092` is unreachable (`KAFKA_TEST_HOST` and `KAFKA_TEST_PORT` override the address).

## Run the pipeline locally with Kafka

```powershell
# Start the broker (advertised to the host as localhost:9092)
docker compose up -d
docker compose ps

# Create the built topics: delivery.file (3 partitions) and delivery.file.dlq (1 partition).
# The designed-only topics (delivery.file.retry, delivery.http*, delivery.syslog*) are NOT created.
.\scripts\create-topics.ps1

# Processing job: landing zone -> Kafka (sample/ holds events.jsonl, the only *.jsonl file there)
.\.venv\Scripts\python.exe -m event_forwarding.cli_spark `
    --config sample\config.yaml `
    --landing-zone sample `
    --checkpoint checkpoints\processing

# File_Sink_Worker: Kafka -> JSONL files (trigger availableNow, drains delivery.file and exits)
.\.venv\Scripts\python.exe -m event_forwarding.cli_file_sink_worker `
    --config sample\config.yaml `
    --checkpoint checkpoints\sink `
    --processing-checkpoint checkpoints\processing

# Inspect the topics (key, headers, partition, offset, value)
.\scripts\inspect-topic.ps1 -Topic delivery.file
.\scripts\inspect-topic.ps1 -Topic delivery.file.dlq -Max 5 -TimeoutMs 5000

# List the outputs
Get-ChildItem output -Recurse -File        # delivered files: output\<tenant_id>\<destination_id>\
Get-ChildItem audit -Recurse -File         # audit\processing, audit\delivery, run and worker summaries
Get-ChildItem rejected -Recurse -File      # lines rejected at parse time

# Stop the broker (data is kept only inside the container)
docker compose down
```

CLI arguments:

| CLI | Argument | Meaning |
| --- | --- | --- |
| `cli_spark` | `--config` | JSON or YAML config |
| | `--landing-zone` | directory with `*.jsonl` files (required; no subdirectories) |
| | `--checkpoint` | checkpoint location of the processing query (required) |
| `cli_file_sink_worker` | `--config` | JSON or YAML config |
| | `--checkpoint` | sink checkpoint location (required) |
| | `--processing-checkpoint` | optional; the sink checkpoint must differ from it |

Exit codes: 0 success; 1 isolation violation or failed DLQ publish or audit write during the run; 2 startup failure (config, landing zone, checkpoint clash, broker unreachable), before any event is read. Checkpoint, `output/`, `audit/` and `rejected/` directories are not committed.

Running a job again with the same checkpoint processes only new input. Delete the checkpoint directory (and use a fresh topic) to start over.

## Configuration and shaping limit

See `sample/config.yaml`. Destinations belong to one tenant; rules match exactly on `tenant_id`, `product` and `schema` (case-sensitive, no wildcards) and reference destinations of the same tenant. A tenant with no matching rule gets nothing (default deny).

**Shaping limit.** A rule can include a list of top-level `data` fields and can add columns only as **constant fields**: a fixed string, number or boolean value. **Calculated fields and expressions are not supported**, and a rule that defines one is rejected at load time.

## Delivery semantics

Delivery is **at-least-once**, not exactly-once. The Spark Kafka sink can republish a batch after a failure, so Kafka can hold duplicates. File output is idempotent: file names are derived from topic, partition and offset range, so replaying a batch rewrites the same file. A reader that must not see duplicates deduplicates on `event_id`.

## Why these components are tested

Tenant isolation is the highest priority, so it gets the most tests and is checked at three independent layers:

| Component | Why it is tested |
| --- | --- |
| Config validation (`config_model`) | Every isolation rule starts here: tenant ownership of destinations, path containment, no calculated fields, secrets by env-var reference. A bad config must be rejected as a whole. |
| Routing (`pipeline.route_events`) | Exact matching and default deny decide what leaves the system. A loose match is a data leak. |
| Isolation layer 1: Isolation_Guard | Last check before publish: record tenant must equal the destination owner and be covered by an enabled rule. |
| Isolation layer 2: Sink_Isolation_Check (`sink_transform`) | The worker does not trust Kafka messages. Tampered key or value, wrong tenant or unknown destination go to the DLQ, never to a file. |
| Isolation layer 3: path derivation (`file_sink_write`) | Output paths come only from configured owner and destination id, never from message content. |
| Write step, replay and failure | Deterministic names and atomic rename give idempotent files. One failing destination must not affect the others. |
| Checkpoint behavior | A rerun must not reprocess input; new files must be picked up. |
| Static scan | Enforces that the event path uses Spark only: no pandas, `collect`, `toPandas`, `take`, `head`, UDFs or driver-side row loops. |
| No-Docker acceptance scenario | Runs the five sample events end to end (routing, shaping, sink transform, files) without Kafka. |

## Built

- Config module: YAML/JSON load, full validation, serializer.
- Processing job: parse and classify lines, exact routing, include and constant-field shaping, Isolation_Guard, Kafka publish keyed `<tenant_id>|<destination_id>`, rejected output, processing audit, Run_Summary, CLI.
- File_Sink_Worker: Sink_Transform, Sink_Isolation_Check, deterministic JSONL files with atomic rename, DLQ on `delivery.file.dlq`, delivery audit, Worker_Summary, CLI.
- Local Kafka (`docker-compose.yml`, `scripts/create-topics.ps1`, `scripts/inspect-topic.ps1`).
- Tests: config properties, routing, isolation layers 1 to 3, write step, static scan, no-Docker acceptance, checkpoint restart. The `kafka` marker and broker fixtures exist; Kafka-marked tests are planned in task 12 and none are in the suite yet, so `pytest -m kafka` currently selects nothing.

## Cut

- Calculated fields and expressions in rules (only constants are supported).
- Optional property tests and unit tests listed as optional in `.kiro/specs/event-forwarding/tasks.md` (for example fan-out, shaping, replay, summary counters and audit-leak properties, CLI edge-case tests).

## Designed only

Not implemented; described in `docs/design.md`.

- HTTP and syslog destinations (rules for them are dropped with `destination_kind_not_built`).
- Parquet output (dropped with `format_not_built`).
- Retry topic and the other per-kind topics (`delivery.file.retry`, `delivery.http*`, `delivery.syslog*`); these topics are not created.
- Production deployment, scaling and cost model.
