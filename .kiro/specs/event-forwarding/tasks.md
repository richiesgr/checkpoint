# Implementation Plan: Event Forwarding

## Overview

Python 3.13.9 / PySpark 4.3.x project in `f:\dev_python\checkpoint` (Windows, PowerShell), using a project-local `.venv/`. Package `event_forwarding/`, tests in `tests/`, sample data in `sample/`. The plan follows the design: a Spark Structured Streaming processing job publishes to Kafka, and a Spark Structured Streaming File_Sink_Worker writes per-destination JSONL files.

Time budget is about 5 hours, so the top-level tasks are ordered by risk and priority. Scope can be cut from the end:

1. Tasks 1 to 8 are the core: config validation (constant fields only), routing, Isolation_Guard, Sink_Transform and write step, with their property tests, the static scan and the no-Docker acceptance test (about 3 hours).
2. Tasks 9 to 11 add the streaming jobs and CLIs, the checkpoint tests, and the deliverables (docker-compose, README, `docs/design.md`, `docs/ai-log/`) (about 1.5 hours).
3. Tasks 12 and 13 add the Kafka-marked tests and the final check (about 0.5 hour). Cut these first, then the optional (`*`) tasks.

Implementation constraints that apply to every task:

- Every module except `config_model.py` is Event_Path code. It must not import pandas or use pyarrow as a writer. It must not call `collect`, `toPandas`, `take` or `head`. It must not use Python UDFs or iterate over Event rows on the driver (Req 11.11).
- The only driver-side loop is over configured Destinations. Tests may call `collect()` on small result DataFrames.
- Tests that are not marked `kafka` must run with Spark `local[*]`, without Docker and without network.
- Environment: Python 3.13.9 and PySpark/Spark 4.3.x (Spark 4.3 supports Python 3.11+; exact patch pinned in `requirements.txt`), JDK 17, 21 or 25 (JDK 21 is installed). All commands (tests, CLIs, linters) run through the venv interpreter, for example `.\.venv\Scripts\python.exe -m pytest`. Packages are installed only with `python -m pip` from the pinned requirements files, never into the global or conda base environment.
- Spark Kafka connector: `org.apache.spark:spark-sql-kafka-0-10_2.13:<same version as pyspark>`, set through `spark.jars.packages` in the session builder (shared helper used by the fixture and both CLIs).
- Windows: Spark local mode may need `HADOOP_HOME` (winutils) and a supported JDK. Use `os.replace` for atomic renames. Use `Path` objects and avoid hard-coded `/`.

## Tasks

- [x] 1. Project scaffold and sample data
  - [x] 1.1 Create project skeleton and test harness
    - Git setup (the workspace is not yet a git repository): run `git init`, `git remote add origin gitea@192.168.50.214:richiesgr/checkpoint-assignement.git`, `git fetch`, and align with the remote's default branch without overwriting remote history. Create a feature branch such as `feature/event-forwarding`. Do not push; ask the user first. Do not modify git config.
    - Create `.gitignore` excluding `.venv/`, `__pycache__/`, `.pytest_cache/`, `.hypothesis/`, `spark-warehouse/`, `metastore_db/`, `derby.log`, checkpoint directories, the run output directories `output/`, `audit/` and `rejected/`, Kafka data volumes, and `.env` files. Commit it first, staging files by name.
    - Create the venv from the conda base interpreter called by its full path (`C:\Users\Richard\miniconda3\python.exe -m venv .venv`); conda itself is never used directly (no `conda activate`, `conda install` or `conda create`). Check that `.\.venv\Scripts\python.exe --version` prints 3.13.9. Check `java -version` reports JDK 17, 21 or 25.
    - Create `event_forwarding/__init__.py`, `requirements.txt` (exact pins: `pyspark==4.3.<patch>`, PyYAML, kafka-python or confluent-kafka for test-side inspection only) and `requirements-dev.txt` (pinned pytest, hypothesis), installed with `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`. Create `pytest.ini` (register marker `kafka`, `testpaths = tests`, default `-m "not kafka"`).
    - Create a small shared Spark session helper (for example `event_forwarding/spark_session.py`) that sets `PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` to the venv `sys.executable` and sets `spark.jars.packages` to `org.apache.spark:spark-sql-kafka-0-10_2.13:<pyspark version>`.
    - Create `tests/conftest.py` with a session-scoped `local[*]` SparkSession fixture built through that helper (venv python env vars set, small `spark.sql.shuffle.partitions`, UI disabled, `tmp_path`-friendly warehouse dir).
    - In `tests/conftest.py`, add a `kafka_broker` fixture that probes `localhost:<port>` and skips with a message naming the missing broker.
    - In `tests/conftest.py`, add a helper that builds a `Kafka_Shaped_Rows` DataFrame from literal rows using the schema in the design.
    - Record the exact installed versions (`python --version`, `java -version`, `python -m pip freeze`) for the README task.
    - _Requirements: 12.1, 12.2, 12.3, 15.8_

  - [x] 1.2 Create the sample input and sample configuration
    - Create `sample/events.jsonl` with exactly e1 to e5 as in `docs/event-forwarding-assignment.md`.
    - Create `sample/config.yaml`:
      - Tenant A: `sales/purchase` (include list omitting `internal_note`) and `security/alert` (constant `region: "eu"`; no calculated fields) to `dest-a-file`. No `identity` rule.
      - Tenant B: `sales/purchase` to `dest-b-file`.
      - Separate output directories per tenant.
      - A commented-out HTTP destination for tenant B as the intended production target.
      - Kafka bootstrap servers and `output_root`.
    - _Requirements: 10.1, 10.2, 10.9, 2.1_

- [x] 2. Config_Module (highest risk, built first)
  - [x] 2.1 Implement `event_forwarding/config_model.py` (the Config_Module)
    - Define typed `Rule`, `Destination`, `KafkaSettings` and `Config` records. A `Rule` carries an optional include list, an optional map of constant fields (string, number or boolean values only) and its destination references. A `Rule` has no calculated fields.
    - Load JSON or YAML once from a CLI path. Distinguish missing path, unreadable file and unparseable content.
    - Collect all validation errors and raise one `ConfigError` that names each offending rule or Destination.
    - Validate:
      - tenant UUID, normalized to lowercase canonical form;
      - rule/destination tenant-ownership match;
      - undefined destination references;
      - destination id pattern `[A-Za-z0-9_-]{1,64}` and duplicate ids;
      - kind and format support (`file`, `http`, `syslog`; `jsonl`, `parquet` for file);
      - required kind-specific settings;
      - list and map size limits;
      - duplicate field names across include and constant fields;
      - constant values are JSON strings, numbers or booleans, and a rule that defines a calculated field is rejected;
      - output paths contained in `output_root`, including `..`, absolute paths and symlinks;
      - secret values given as env-var references only (unset or empty rejects, never echoing the value).
    - Provide a serializer for round-trip.
    - Provide builders for the small `rules_df` (broadcastable) and `destination_owner_df` (destination_id, owning tenant_id, output path, kind, format).
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 4.6, 4.7, 5.10, 8.3, 8.4, 14.8, 14.9, 15.9_

  - [x] 2.2 Write property test for configuration rejection
    - **Property 2: Config_Store rejects every defect it is supposed to reject**
    - Hypothesis, 2 examples. Plant one defect at a time (bad tenant UUID, ownership mismatch, undefined destination, bad kind or format, duplicate or malformed id, escaping path, unset secret env var, calculated field, non-scalar constant value) and assert the whole config is rejected with the offender named.
    - File: `tests/test_config_properties.py`.
    - **Validates: Requirements 2.5, 2.6, 2.7, 2.8, 4.6, 4.7, 5.10, 8.3, 8.4, 14.8, 14.9**

  - [ ]* 2.3 Write unit tests and round-trip test for configuration
    - **Property 18 (config half): Config round-trip is exact**
    - Cover missing, unreadable and unparseable config path errors, the valid `sample/config.yaml`, and parse â†’ serialize â†’ parse equality.
    - File: `tests/test_config_unit.py`.
    - **Validates: Requirements 2.4, 2.9, 12.7, 10.12**

  - [x] 2.4 Remove `expr_parser.py` and calculated-field support from `config_model.py`, config samples and tests
    - Cleanup of code built before the expression feature was cut (the calculated-field map, expression validation and the `expr_parser` import in `config_model.py`). Do this before any task that builds on shaping.
    - Delete `event_forwarding/expr_parser.py`, `tests/test_expr_parser_unit.py` and `tests/test_expr_properties.py`.
    - In `config_model.py`: remove the calculated-fields field, its size limit, its serializer and builder output, and the expression validation. Keep duplicate-name validation across include and constant fields. Add validation that every constant value is a string, number or boolean (a Python `bool` is a boolean, not a number), and that a rule with a calculated-fields key is rejected with an error naming the rule.
    - In `sample/config.yaml`: remove the calculated `risk_score` entry from `security/alert` and keep the constant `region: "eu"`.
    - Update `tests/test_config_properties.py` and any other test or fixture that references calculated fields or `expr_parser`. Add a test that string, number and boolean constants load unchanged and that a calculated field or a non-scalar constant is rejected at load time.
    - Check with a text search that no reference to `expr_parser`, calculated fields or expressions remains in code, tests or samples.
    - _Requirements: 2.2, 4.4, 4.6, 4.7, 10.9, 12.6_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Processing core: parse, routing, shaping, Isolation_Guard, publish-ready DataFrame
  - [x] 4.1 Implement envelope schema and parse stage
    - Create `event_forwarding/envelope_schema.py` with the `StructType` (`data` kept as raw string).
    - In `event_forwarding/pipeline.py`, implement `parse_events(text_df)` as pure Column logic:
      - skip blank lines;
      - classify `malformed_json`, `missing_envelope_field` and `invalid_data` (including non-object top-level JSON and null fields), keeping the source file name via `input_file_name`;
      - normalize `tenant_id`, sending an empty, null or non-UUID value to `invalid_tenant_id`.
    - Return a (valid, rejected) pair of DataFrames.
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.7, 8.1, 8.8_

  - [x] 4.2 Implement routing in `pipeline.py`
    - `route_events(valid_df, rules_df)` filters to the configured (tenant_id, product, schema) keys before shaping. It then runs a broadcast join on exact, case-sensitive, untrimmed keys with `enabled = true`.
    - Fan out one row per matching rule/destination. Never use wildcards.
    - Emit audit rows for `no_matching_rule`, `invalid_tenant_id` and `destination_kind_not_built` (http/syslog dropped before publish).
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 2.12, 8.2, 8.7, 11.2_

  - [x] 4.3 Implement shaping in `pipeline.py`
    - `shape_events(routed_df)` builds the payload with Column ops: included top-level fields (absent ones omitted) or all fields, plus constant fields (`lit` values of type string, number or boolean). Constants are the only added columns.
    - Keep the envelope fields `event_id`, `product`, `schema` and `tenant_id`.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x] 4.4 Implement `event_forwarding/isolation_guard.py`
    - A Spark anti-join or filter that keeps a record only if its tenant equals the destination's owning tenant and its (product, schema) is covered by an enabled rule that references that destination.
    - Return (guarded_df, violations_df) and route violations to the processing audit with `isolation_violation`. Other records proceed.
    - _Requirements: 8.5, 8.6, 11.3_

  - [x] 4.5 Implement `event_forwarding/kafka_publish.py` and `event_forwarding/audit_spark.py`
    - `kafka_publish`: derive `topic` from destination kind, `key` as `<tenant_id>|<destination_id>` (lowercase UUID), and `value` as `to_json(struct(tenant_id, destination_id, product, schema, event_id, payload))`.
    - `audit_spark`: build processing Audit_Log rows with fields `event_id, tenant_id, product, schema, decision, reason, destination_id`. Never include `data` or `payload`. Null the `tenant_id` when it is invalid. Write as Spark JSON Lines under `<audit_root>/processing/`.
    - _Requirements: 15.1, 15.2, 15.3, 11.3, 14.1, 14.3, 14.4, 14.5_

  - [x] 4.6 Wire `build_publish_ready` in `pipeline.py`
    - `build_publish_ready(text_df, config)` chains parse â†’ route â†’ shape â†’ Isolation_Guard â†’ publish columns and returns (publish_ready_df with `topic, key, value`, rejected_df, audit_df).
    - Keep it a pure DataFrame function so tests and `foreachBatch` use the same code.
    - _Requirements: 8.5, 11.1, 15.3, 3.4_

  - [x] 4.7 Write property test for routing
    - **Property 3: Routing matches exactly the enabled, exact-equal rules**
    - Hypothesis, 2 examples. Include case, whitespace, non-canonical UUID and `enabled=false` variants.
    - File: `tests/test_routing_properties.py`.
    - **Validates: Requirements 3.1, 3.3, 3.5, 11.2**

  - [x] 4.8 Write property test for Isolation Layer 1 (Isolation_Guard)
    - **Property 8: The Isolation_Guard admits only ownership-correct records**
    - Hypothesis, 2 examples, 2 to 5 tenants and 1 to 3 destinations each. Include a planted mismatch that must be excluded and audited as `isolation_violation`.
    - File: `tests/test_isolation_layer1_properties.py`.
    - **Validates: Requirements 8.2, 8.5, 8.6, 8.9, 8.12, 8.14, 11.3**

  - [ ]* 4.9 Write property test for unmatched, invalid-tenant and undelivered-kind events
    - **Property 4: Unmatched, invalid-tenant, or undelivered-kind events publish nothing**
    - File: `tests/test_unmatched_properties.py`.
    - **Validates: Requirements 2.12, 3.2, 3.6, 3.7, 8.1, 8.7**

  - [ ]* 4.10 Write property test for fan-out independence
    - **Property 5: Fan-out to multiple rules/destinations is independent**
    - File: `tests/test_fanout_properties.py`.
    - **Validates: Requirements 3.4, 9.1**

  - [ ]* 4.11 Write property test for payload shaping and Delivery_Message construction
    - **Property 6: Payload shaping and Delivery_Message construction are exact**
    - File: `tests/test_shaping_properties.py`.
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 15.2**

  - [ ]* 4.12 Write property test for landing-zone line classification
    - **Property 1: Landing zone file selection and line-level rejection**
    - Test `parse_events` on generated lines (blank, malformed, non-object, missing or null fields, non-object `data`). File selection (`.jsonl` only, no subdirectories) is exercised through a small temp-directory source.
    - File: `tests/test_landing_properties.py`.
    - **Validates: Requirements 1.1, 1.3, 1.4, 1.5, 1.6, 1.7**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. File_Sink_Worker core: Sink_Transform, write step, isolation layers 2 and 3
  - [x] 6.1 Implement `event_forwarding/sink_transform.py`
    - `sink_transform(kafka_shaped_df, destination_owner_df)` returns (deliverable_df, dlq_df, audit_df) as a pure DataFrame function.
    - Parse `value` with `from_json`. Rows with a null parse or any null required field go to the DLQ with `malformed_message`.
    - Apply Sink_Isolation_Check as a filter or anti-join. It checks that the destination is defined, that the value tenant equals the owner, that the key tenant equals the value tenant, and that the key equals `<tenant_id>|<destination_id>`. Failures go to the DLQ unchanged with `isolation_violation`.
    - Route Parquet destinations to `format_not_built` audit rows.
    - Compute min and max offset per (destination_id, topic, partition) with a window aggregation, and derive the deterministic `file_name` column.
    - _Requirements: 8.9, 8.10, 5.6, 5.7, 2.13, 15.8, 15.10_

  - [x] 6.2 Implement `event_forwarding/audit_worker.py`
    - Build delivery-stage Audit_Log rows for `delivered`, `dropped` and `dead-lettered`, with no payload.
    - Write JSONL under `<audit_root>/delivery/` with deterministic names from topic, partition and offset range, using a temp write and an atomic rename.
    - Surface audit write failures as errors, not as recorded decisions.
    - _Requirements: 14.2, 14.3, 14.5, 14.7_

  - [x] 6.3 Implement `event_forwarding/file_sink_write.py`
    - Implement the `foreachBatch` body as `process_micro_batch(batch_df, batch_id, config, publish_dlq=<Spark Kafka sink default>)`. The `publish_dlq` callable is a plain function parameter used for fault injection, not a consumer or producer abstraction.
    - Call `sink_transform`, then run one driver loop over configured Destinations only. Per Destination: filter the deliverable rows with a Spark filter, `DataFrame.write` JSONL to `<output_root>/<tenant_id>/<destination_id>/.tmp/`, and list and `os.replace` each file to its deterministic final name.
    - The path comes only from configured `(owner, destination_id)`, never from a message column.
    - On a write or rename failure for one Destination: delete its temp files, leave any existing final file unchanged, publish only that Destination's rows unchanged to `delivery.file.dlq` with reason `write_failed`, and continue with the other Destinations.
    - Raise on a failed DLQ publish or rename so the checkpoint does not advance.
    - Publish DLQ rows with headers, and write the delivery audit.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.8, 5.9, 5.11, 8.11, 9.3, 9.4, 14.10, 15.11_

  - [x] 6.4 Write property test for Isolation Layer 2 (Sink_Isolation_Check)
    - **Property 9: The Sink_Isolation_Check rejects tampered or unowned messages**
    - Hypothesis, 2 examples. Feed `Kafka_Shaped_Rows` with valid rows and planted wrong-tenant, wrong-key and unknown-destination rows. Assert that zero tampered rows are deliverable, each goes unchanged to the DLQ with `isolation_violation`, and each has one audit row.
    - File: `tests/test_sink_isolation_properties.py`.
    - **Validates: Requirements 8.9, 8.10, 8.13, 12.5**

  - [x] 6.5 Write property test for Isolation Layer 3 (path derivation)
    - **Property 10: Output path depends only on configured ownership, never on message content**
    - Run the write step against a temp `output_root` and assert every written file lies under its Destination's configured directory, that no other Destination directory is touched, and that tampered `product`, `schema` and `payload` values do not change the path.
    - File: `tests/test_path_properties.py`.
    - **Validates: Requirements 5.1, 5.5, 8.8, 8.11**

  - [x] 6.6 Write unit tests for the write step
    - Pass the same `Kafka_Shaped_Rows` twice and assert one file and no duplicates (12.15). Fail one Destination's write and assert that only its rows reach the DLQ output while the other Destinations are still written (12.18). Cover one JSONL file per batch, with the line format and `\n` separators.
    - File: `tests/test_sink_write_unit.py`.
    - _Requirements: 12.15, 12.18, 5.2, 5.3, 5.6, 5.11, 9.3_

  - [ ]* 6.7 Write unit tests for crash replay and double fault
    - Simulate a crash after rename and re-run, asserting no duplicates (12.16). Force both the write and the DLQ publish to fail, and assert that `process_micro_batch` raises and that the batch is excluded from the delivered and dead-lettered counts (12.17).
    - File: `tests/test_sink_faults_unit.py`.
    - _Requirements: 12.16, 12.17, 5.9, 9.4_

  - [ ]* 6.8 Write property test for idempotent replay
    - **Property 11: Deterministic file naming makes offset-range replay idempotent**
    - File: `tests/test_sink_replay_properties.py`.
    - **Validates: Requirements 5.2, 5.3, 5.6**

  - [ ]* 6.9 Write property test for clean write failure
    - **Property 12: Write/rename failure is clean and dead-letters the whole batch without advancing the checkpoint**
    - File: `tests/test_sink_failure_properties.py`.
    - **Validates: Requirements 5.9, 9.3, 9.4**

  - [ ]* 6.10 Write property test for checkpoint-advance semantics
    - **Property 13: Checkpoint advances only after the whole Micro_Batch (including DLQ publish) succeeds**
    - File: `tests/test_sink_checkpoint_semantics_properties.py`.
    - **Validates: Requirements 5.7, 9.3, 9.4**

  - [ ]* 6.11 Write property test for malformed messages
    - **Property 14: Malformed Delivery_Messages are dead-lettered, not written, and do not block the Micro_Batch**
    - File: `tests/test_sink_malformed_properties.py`.
    - **Validates: Requirements 15.10**

  - [ ]* 6.12 Write property test for Parquet destinations
    - **Property 15: Non-file destination kinds and non-JSONL formats are dropped, not written**
    - File: `tests/test_sink_format_properties.py`.
    - **Validates: Requirements 2.13**

- [x] 7. Static scan and no-Docker acceptance scenario
  - [x] 7.1 Write the AST static-scan test
    - Parse every Event_Path module of both jobs with `ast`: `pipeline.py`, `isolation_guard.py`, `kafka_publish.py`, `audit_spark.py`, `summary_spark.py`, `envelope_schema.py`, `sink_transform.py`, `file_sink_write.py`, `audit_worker.py`, `summary_worker.py`, `cli_spark.py`, `cli_file_sink_worker.py`.
    - Fail on imports of `pandas` or `pyarrow` (as writer), calls to `collect`, `toPandas`, `take` or `head`, and any `udf`, `pandas_udf`, `mapInPandas` or `eval`/`exec` use.
    - Skip modules that do not exist yet, but fail if the module list is empty. Allow-list only `config_model.py`.
    - Include a self-check that feeds the scanner a snippet containing each banned construct and asserts it is flagged.
    - File: `tests/test_static_scan.py`.
    - _Requirements: 11.11, 11.12, 11.13_

  - [x] 7.2 Write the no-Docker acceptance scenario test
    - Load `sample/events.jsonl` and `sample/config.yaml`. Run `build_publish_ready` and assert:
      - three rows on `delivery.file`;
      - keys `<A>|dest-a-file` Ã—2 and `<B>|dest-b-file` Ã—1;
      - zero rows for e4 and e5.
    - Convert the publish-ready DataFrame to `Kafka_Shaped_Rows` (add `partition`, `offset` and `timestamp`; cast `key` and `value` to binary). Run `sink_transform` and `process_micro_batch` into a temp `output_root`. Assert:
      - e1 and e2 appear once each under tenant A;
      - `internal_note` is absent from e1;
      - `region == "eu"` and the original `risk` value unchanged on e2;
      - e3 appears once under tenant B;
      - no cross-tenant files;
      - the DLQ output holds no e4 or e5.
    - Also assert that an Event with no enabled rule produces zero published and zero written records.
    - File: `tests/test_acceptance_no_docker.py`.
    - _Requirements: 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.11, 8.14_

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Streaming jobs, CLIs and checkpoint behavior
  - [x] 9.1 Implement summaries and the broker probe
    - Create `event_forwarding/summary_spark.py` (Run_Summary with read, rejected, dropped and published counts, total and per tenant and Destination) and `event_forwarding/summary_worker.py` (Worker_Summary with delivered and dead-lettered counts). Both use Spark aggregations only, with non-negative integers and JSON output.
    - Create `event_forwarding/kafka_util.py` with a bootstrap-servers reachability probe (a socket connect, not event handling).
    - _Requirements: 9.5, 9.6, 15.7_

  - [x] 9.2 Implement the processing streaming query and `cli_spark.py`
    - In `pipeline.py` add `run_processing(spark, config, landing_zone, checkpoint, batch_sink=<Kafka sink>)`. It uses `readStream.format("text")` with a `*.jsonl` glob, no recursion, `trigger(availableNow=True)` and a checkpoint.
    - `foreachBatch` calls `build_publish_ready` and `batch_sink`. The default `batch_sink` is one `df.write.format("kafka")` call over `topic`, `key` and `value`.
    - Write the Rejected_Output, the processing audit and the Run_Summary with Spark.
    - Exit non-zero after the run when there was an isolation violation or an unreadable file.
    - `cli_spark.py` builds its session through the shared Spark session helper (which sets `PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` to the venv python and `spark.jars.packages` to the Kafka connector), parses arguments, loads the config once, checks that the landing zone is a directory, probes the broker and runs the query. Config errors, a bad landing-zone path and an unreachable broker give a non-zero exit before any Event is read.
    - _Requirements: 1.1, 1.2, 1.8, 1.9, 1.10, 1.11, 5.13, 8.6, 11.1, 14.1, 15.3, 15.4, 15.7_

  - [x] 9.3 Implement `cli_file_sink_worker.py`
    - Build the session through the shared Spark session helper (venv `PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON`, Kafka connector via `spark.jars.packages`).
    - Load the config once and build `destination_owner_df`.
    - `readStream.format("kafka")` on `delivery.file` with `trigger(availableNow=True)` and its own Sink_Checkpoint_Location, distinct from the processing checkpoint.
    - Wire `foreachBatch(process_micro_batch)`, commit no manual offsets, write the Worker_Summary at drain end, and exit non-zero on an isolation violation or a DLQ failure.
    - Probe the broker and fail fast with the bootstrap servers named.
    - _Requirements: 5.8, 5.12, 8.10, 9.4, 9.6, 15.7, 15.11, 15.12_

  - [x] 9.4 Write checkpoint restart and incremental tests (no Kafka)
    - Call `run_processing` twice with the same checkpoint and a local capture `batch_sink` (a Spark write to a temp directory). Assert zero additional records the second time.
    - Add a new file between two runs and assert that only its events are processed.
    - File: `tests/test_checkpoint.py`.
    - _Requirements: 1.10, 5.13, 12.13, 12.14_

  - [ ]* 9.5 Write CLI and edge-case unit tests
    - Cover an empty landing zone (exit 0, nothing published, nothing rejected), a non-directory landing zone, an unreadable file, a non-`.jsonl` file ignored, and config failure exit codes.
    - File: `tests/test_cli_unit.py`.
    - _Requirements: 1.1, 1.2, 1.8, 1.9, 2.4, 2.9_

  - [ ]* 9.6 Write property test for summary counters
    - **Property 16: Run_Summary and Worker_Summary are consistent, non-negative counters**
    - File: `tests/test_summary_properties.py`.
    - **Validates: Requirements 9.5, 9.6**

  - [ ]* 9.7 Write property test for audit and secret leakage
    - **Property 17: Audit entries never leak payload or secret values**
    - File: `tests/test_audit_leak_properties.py`.
    - **Validates: Requirements 14.3, 14.5, 14.6, 14.10**

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Deliverables: docker-compose, README, design document, AI log
  - [x] 11.1 Create docker-compose and topic scripts
    - Create `docker-compose.yml` with a single-node Kafka broker in KRaft mode (pinned `apache/kafka` image tag, no Zookeeper) and a host-reachable listener on a documented port.
    - Create `scripts/create-topics.ps1` that creates `delivery.file` and `delivery.file.dlq` with documented partition counts through `docker compose exec`.
    - Create `scripts/inspect-topic.ps1` (console consumer, printing key and headers).
    - _Requirements: 15.5, 15.6, 12.9_

  - [x] 11.2 Write `README.md`
    - Record the exact versions of Python 3.13.9, the JDK (17, 21 or 25; 21 installed), PySpark 4.3.x, the Spark Kafka connector `org.apache.spark:spark-sql-kafka-0-10_2.13:<pyspark version>` (explain it is resolved at runtime through `spark.jars.packages` in the session builder, which needs network access on first use, or can be shipped as a jar), the Kafka client library, the Kafka image and all pinned dependencies, taken from task 1.1. Note the Windows `HADOOP_HOME`/`winutils` requirement if needed.
    - Provide copy-pasteable PowerShell commands that work from a fresh clone, all through the venv interpreter:
      - check `java -version`;
      - create the venv (`C:\Users\Richard\miniconda3\python.exe -m venv .venv`), state that the venv is built from a Python 3.13.9 interpreter and that this path may differ on other machines, state that conda is not used directly, and check `.\.venv\Scripts\python.exe --version` prints 3.13.9;
      - install (`.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`);
      - run tests (`.\.venv\Scripts\python.exe -m pytest`);
      - state that the CLIs and test fixture set `PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` to the venv python;
      - `docker compose up -d`;
      - topic creation (stating that the designed-only topics are not created);
      - the Spark job;
      - the File_Sink_Worker with `availableNow`;
      - tests without Docker (`pytest`);
      - Kafka tests (`pytest -m kafka`);
      - topic inspection;
      - listing the output directories.
    - State the shaping limit explicitly: a rule can add a column only as a constant field with a fixed string, number or boolean value. Calculated fields and expressions are not supported.
    - Explain why each tested component was chosen and name isolation as the highest priority. State the at-least-once semantics with `event_id` dedup.
    - Add separate "Built", "Cut" and "Designed only" sections, and a link to `docs/ai-log/`.
    x

  - [x] 11.3 Write `docs/design.md` (at most 3 pages, at most 1,500 words, one diagram)
    - Use exactly five top-level sections: Architecture, Data isolation, Operating it, Scale and cost, Scope.
    - Architecture:
      - the full product, with each component labeled built or designed only;
      - where Spark is used and where it is not;
      - the HTTP and syslog designs, including tenant B's intended HTTP target;
      - few shared topics with the 15,000 Ã— 3 Ã— 2 arithmetic, and the key choice;
      - head-of-line blocking and its mitigations;
      - the options 1/2/3 comparison including Kafka Connect;
      - Landing_Zone read as the Auto Loader substitute.
    - Data isolation: three layers, tests, audit, and the security reviewer steps.
    - Operating it, with seven labeled topics: slow destinations, noisy tenant, config mistakes, poison events, duplicate delivery, backlog recovery, per-tenant monitoring. Cover at-least-once with `event_id` dedup, replay with retention assumption, and config-change behavior for in-flight events.
    - Scale and cost: 40k and 120k events/sec, first failure, the top-10 tenant hot-partition effect, ranked cost drivers, the 5-minute latency budget, and Kafka ACLs.
    - Scope: three labeled lists (built, cut with reasons, next week), Default_Deny, the out-of-scope portal assumption, and wildcard rule risk. Add one sentence in the Scope section that only constant added columns (fixed string, number or boolean) are supported and that calculated fields are cut.
    - _Requirements: 3.9, 6.1, 6.2, 7.1, 7.2, 8.15, 9.7, 9.8, 11.4, 11.5, 11.7, 11.8, 11.9, 11.10, 11.14, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.9, 13.10, 13.11, 13.12, 13.14, 13.15, 13.16, 13.17, 4.8_

  - [x] 11.4 Create the AI log placeholder
    - Create `docs/ai-log/README.md` explaining that unedited AI transcripts (secrets removed) are placed here, and the expected file naming per tool.
    - _Requirements: 12.12_

  - [ ]* 11.5 Write a design document check test
    - Count words (excluding the mermaid block), assert at most 1,500, exactly five top-level sections, at most one diagram, the presence of the seven Operating it topics, and the constant-only statement in the Scope section (and in `README.md`).
    - File: `tests/test_design_doc.py`.
    - _Requirements: 4.8, 13.1, 13.2, 13.3, 13.13_

- [ ] 12. Kafka-marked tests (cut first if time runs short)
  - [x] 12.1 Write the Kafka acceptance test
    - Marked `kafka`. Run the processing job and then the File_Sink_Worker against the docker-compose broker using `sample/`.
    - Use the fixed shared topics `delivery.file` and `delivery.file.dlq` (one topic per destination kind, never per tenant or per run). Use unique checkpoint and output directories per run. Because the topics keep messages from earlier runs, assert only on the messages of this run (for example, offsets recorded at test start, or this run's `event_id`s).
    - Assert criteria 3 to 9 of Requirement 10: consumed topic and key (three messages, none for e4 and e5), files written by the worker, `internal_note` omitted, `region == "eu"` and the unchanged `risk` value on e2, no cross-tenant files, and an empty DLQ for e4 and e5.
    - File: `tests/test_acceptance_kafka.py`.
    - _Requirements: 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.10, 12.3, 12.4_

  - [ ]* 12.2 Write the Kafka integration test
    - Marked `kafka`. Exactly one test: Spark publish to the broker, worker consume, files and `delivery.file.dlq` inspected, including a tampered message injected into `delivery.file` that must land in the DLQ with the `isolation_violation` header.
    - File: `tests/test_kafka_integration.py`.
    - _Requirements: 12.4, 8.10, 14.10, 15.11_

  - [ ]* 12.3 Write the sink checkpoint restart test
    - Marked `kafka`. Run the File_Sink_Worker twice with the same Sink_Checkpoint_Location and assert that the second run processes no additional Delivery_Messages.
    - File: `tests/test_sink_checkpoint_kafka.py`.
    - _Requirements: 12.19, 5.12, 15.12_

- [ ] 13. Landing-zone generator and end-to-end test
  - [x] 13.1 Implement the landing-zone event generator
    - A generator that writes JSON Lines files in the event envelope (`event_id`, `product`, `schema`, `tenant_id`, `data`) into a landing-zone directory, for the processing job to read.
    - Every run gives its events unique `event_id`s, so a test can tell its events apart from messages left on the shared topics by earlier runs.
    - Decided with the user:
      - Base the data on the samples in `docs/event-forwarding-assignment.md` and keep it coherent with them.
      - At most 10 unique tenants, each a UUID.
      - `product` comes from a fixed set and `schema` from a fixed, predefined set per product, matching the assignment: `sales/purchase`, `security/alert`, `identity/login`.
      - `data` is random but keeps the sample's fields for its schema (`purchase`: `order_id`, `amount`, `internal_note`; `alert`: `alert_id`, `risk`; `login`: `user`, `result`).
      - The data follows a repeating, predictable pattern, so the expected result can be computed and checked at the end (for example, counts per tenant, product and schema, and field values derived from the event's sequence number).
      - Bad data is mixed in, so the rejected output can be checked too: for example malformed JSON, an empty or non-UUID `tenant_id`, a missing envelope field, a non-object `data`.
      - The generator also writes a matching config file (rules and destinations for the generated tenants), so the test can check that every rule is applied exactly. The mix covers tenants with rules, tenants without rules, products not forwarded, include lists and constant fields. `sample/config.yaml` is not changed.
      - A fixed seed is used for the random parts.
      - Everything for the end-to-end test lives in its own directory, `tests/end_to_end/` (settings, generator, Kafka helpers, test, start script, README). Each run writes its generated config, data, checkpoints and output to `%TEMP%\e2e-<run_id>` (or `E2E_RUN_ROOT`), outside the repository, because Spark staging paths under the repository exceed the Windows 260-character limit. Never committed.
      - Rules of the generated config: T0-T3 `sales/purchase` (include `order_id`, `amount`) and `security/alert` (constant `region: "eu"`); T4-T5 `sales/purchase` all fields; T6 `sales/purchase` fan-out to two destinations; T7 a disabled rule; T8 no rule with a destination; T9 no rule and no destination; no tenant forwards `identity/login`.
    - Counts per run (decided with the user): exactly 1,000 valid events that match at least one enabled rule (delivered), 150 valid events that match no enabled rule (not delivered: unforwarded product, tenant without rules, disabled rule), and 20 bad lines (rejected). Delivered file records can exceed 1,000 when a rule fans out to two destinations; the test computes the exact expected count from the generated config.
    - The 1,170 lines are split across 5 landing-zone `.jsonl` files.
    - Not Event_Path code: it produces input and never touches the processing or sink path.

  - [x] 13.2 Write the end-to-end test
    - Marked `kafka`. Flow: the generator writes files to a temp landing zone. The processing job (`run_processing`) reads them and publishes to `delivery.file`. The File_Sink_Worker reads `delivery.file`, applies `sink_transform` and writes each destination's files under the `output_root` from `config.yaml` (redirected to a temp directory for the test).
    - Only the file destination kind is exercised. No `delivery.http` or `delivery.syslog` topics.
    - Use the fixed shared topics, unique checkpoints and output directories per run, and assert only on this run's `event_id`s.
    - Assert: every generated event that a rule forwards is written exactly once under its tenant's configured destination directory, with the configured shaping (include list, constant fields). No event reaches another tenant's directory. Events without an enabled rule, unconfigured tenants and invalid tenants produce no files. Nothing from this run reaches `delivery.file.dlq` unless planted.
    - Check rejected lines too: each planted bad line appears in the Rejected_Output with its expected reason, and none reaches Kafka or a file.
    - File: `tests/end_to_end/test_end_to_end_kafka.py`.
    - Also check that no staging files (`.tmp`) are left in customer directories.
    - Add `tests/end_to_end/run-end-to-end.ps1`, which starts the broker if needed, creates the topics with `scripts/create-topics.ps1`, and runs this test through the venv (`.\.venv\Scripts\python.exe -m pytest tests/end_to_end -m kafka`). `-GenerateOnly` only writes a run directory.
    - The worker starts at the topic offsets recorded before the run (`run_worker(..., starting_offsets=...)`, default `earliest`), so it never reads earlier runs' messages.
    - _Requirements: 3.1, 3.2, 4.1, 4.4, 5.1, 8.5, 8.9, 8.11, 15.3, 15.11_

- [ ] 14. Final checkpoint - Ensure all tests pass
  - Run `pytest` (no Docker) and, if the broker is available, `pytest -m kafka`. Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. They are the lower-priority property tests, extra unit tests, the Kafka integration and sink checkpoint tests, and the design-document check test.
- The isolation property tests (4.8, 6.4, 6.5), the config rejection test (2.2), the calculated-field cleanup (2.4), routing (4.7), the static scan (7.1) and both acceptance tests (7.2, 12.1) are required because the assignment names isolation as the top priority.
- Cut order if time runs short: optional tasks first, then task 12 (keep 12.1 if the broker works), then 9.4 and the remaining task 9 work. Never cut tasks 2, 4, 6, 7 or the task 11 deliverables.
- Git: the workspace is not yet a git repository. Task 1.1 initialises it, adds `origin` (`gitea@192.168.50.214:richiesgr/checkpoint-assignement.git`), fetches and aligns with the remote's default branch, and creates a feature branch. Nothing is pushed without asking the user.
- Commit after each completed task on the feature branch, one logical change per commit, using Conventional Commits with the task id (for example `feat: config model and validation (task 2.1)`). Stage files by name (never `git add -A` or `git add .`) and run the test command through the venv before each commit. `docs/ai-log/` is committed unedited, with secrets and personal data removed.
- `.venv/`, caches, Spark artifacts, checkpoints, run output directories, Kafka volumes and `.env` files are never committed (see `.gitignore` in task 1.1).
- Each task references specific requirements for traceability. Property tests carry their design property number.
- `process_micro_batch` takes a `publish_dlq` callable only so tests can inject failures. The tests use no in-memory consumer or producer.
- Calculated fields and the expression grammar were cut. Task 2.4 removes the already-built expr_parser.py and calculated-field support. Property 7 (expression oracle) no longer exists, and the other property numbers are unchanged. The old tasks 2.1 and 2.3 were deleted, so the former 2.2, 2.4 and 2.5 are now 2.1, 2.2 and 2.3.
- Property tests in the plan sit next to their implementation tasks for early feedback, but they are separate leaf tasks so they can be skipped independently.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["2.4"] },
    { "id": 4, "tasks": ["2.3"] },
    { "id": 5, "tasks": ["3"] },
    { "id": 6, "tasks": ["4.1", "4.4", "4.5"] },
    { "id": 7, "tasks": ["4.2", "4.12"] },
    { "id": 8, "tasks": ["4.3"] },
    { "id": 9, "tasks": ["4.6"] },
    { "id": 10, "tasks": ["4.7", "4.8", "4.9", "4.10", "4.11"] },
    { "id": 11, "tasks": ["5"] },
    { "id": 12, "tasks": ["6.1", "6.2"] },
    { "id": 13, "tasks": ["6.3", "6.4"] },
    { "id": 14, "tasks": ["6.5", "6.6", "6.7", "6.8", "6.9", "6.10", "6.11", "6.12"] },
    { "id": 15, "tasks": ["7.1", "7.2"] },
    { "id": 16, "tasks": ["8"] },
    { "id": 17, "tasks": ["9.1"] },
    { "id": 18, "tasks": ["9.2", "9.3"] },
    { "id": 19, "tasks": ["9.4", "9.5", "9.6", "9.7"] },
    { "id": 20, "tasks": ["10"] },
    { "id": 21, "tasks": ["11.1"] },
    { "id": 22, "tasks": ["11.2", "11.3", "11.4"] },
    { "id": 23, "tasks": ["11.5", "12.1", "12.2", "12.3", "13.1"] },
    { "id": 24, "tasks": ["13.2"] },
    { "id": 25, "tasks": ["14"] }
  ]
}
```