# Requirements Document

## Introduction

Event Forwarding is a local, Python-based mini product that reads multi-tenant events from a landing-zone directory (JSON Lines) and forwards them to customer-owned destinations according to per-tenant, per-product, per-schema configuration. The most serious failure is forwarding data to a tenant that must not receive it, so strict data isolation is the top-priority quality attribute.

The product has two stages decoupled by Apache Kafka:

1. **Processing stage (Spark).** A PySpark job (DataFrame API and Structured Streaming, local mode) reads the Landing_Zone with a Structured Streaming file source that tracks processed files in a checkpoint location (the local substitute for Databricks Auto Loader). It parses, validates, filters (default deny), shapes the payload, applies the Isolation_Guard, and PUBLISHES the shaped records to Kafka with the Spark Kafka sink. It no longer writes customer files. The default run mode is trigger `availableNow` (process everything available, then stop), so that the exercise and the tests terminate. The design document describes continuous micro-batch triggers (for example every 30 to 60 seconds) for the full product. No event-handling code, in either stage, collects event data to the driver, uses pandas, or uses Python UDFs (Requirement 11).
2. **Delivery stage (workers).** A small File_Sink_Worker consumes the `delivery.file` topic, re-checks isolation, batches records per destination and writes JSONL files under `<output_root>/<tenant_id>/<destination_id>/`. Decision: the File_Sink_Worker is implemented with Apache Spark Structured Streaming reading from Kafka (`readStream` with format `kafka` on `delivery.file`), trigger `availableNow` by default, its own Sink_Checkpoint_Location, and `foreachBatch` to write per-destination JSONL files. The File_Sink_Worker commits no Kafka offsets manually; restart safety comes from the Spark checkpoint. The File_Sink_Worker is event-handling code and is subject to the same bans as the processing stage (Requirement 11). The only driver-side loop in the File_Sink_Worker is a loop over configuration (the Destinations), never over Event rows.

Kafka topics are few and shared by destination kind (`delivery.file`, designed-only `delivery.http` and `delivery.syslog`, plus retry and dead-letter variants), not per tenant, product or destination. A per-tenant-product-destination topic design would need about 100,000 topics at 15,000 tenants, which does not scale. The message key `<tenant_id>|<destination_id>` keeps the records of one destination ordered within one partition.

Delivery is at-least-once end to end: the Spark Kafka sink is at-least-once, and customers deduplicate with `event_id`. File output is idempotent for a replayed offset range because file names are deterministic and derived from the topic, partition and offset range of the Micro_Batch.

Scope is sized for about 5 hours. The build delivers: configuration loading and validation, the Spark processing job with Kafka publish, the File_Sink_Worker (JSONL) with a dead-letter topic, isolation enforcement at three layers and tests, a docker-compose single-node Kafka (KRaft, no Zookeeper), a README, and a design document of at most 3 pages. HTTP and syslog destinations are "designed, not built": the configuration model still accepts the kinds `http` and `syslog`, but no topic consumer, sender, mock endpoint or mock receiver is built. Parquet output is designed only; the build writes JSONL. Configuration stays a JSON or YAML file read once at start, and no database is built.

Out of scope: the customer portal and configuration updates. The Forwarding_System expects a valid configuration as input. A "forward all data" option is not a pipeline feature: the pipeline never forwards an Event without an explicit exact-match Forwarding_Rule (default deny). The portal is assumed to expand a customer's "all my products" choice into explicit per-product and per-schema Forwarding_Rules before the configuration reaches the pipeline.

Built vs. designed-only:

| Capability | Status |
|---|---|
| Config model (JSON or YAML file, read once at start), validation, default-deny exact-match filtering, field include and constant fields (a new column with a fixed string, number or boolean value; no calculated fields) | Built |
| Spark Structured Streaming file source with checkpoint, trigger `availableNow`, isolation guard, shaping | Built |
| Kafka publish stage (Spark Kafka sink, topic chosen by destination kind through the `topic` column, key `<tenant_id>\|<destination_id>`) | Built |
| File_Sink_Worker (Spark Structured Streaming reading from Kafka, trigger `availableNow`, own Sink_Checkpoint_Location, `foreachBatch`, per-destination JSONL files, Spark-side sink isolation re-check, temporary write then atomic rename with deterministic names, no manual offset commit) | Built |
| Dead-letter topic `delivery.file.dlq` (isolation violations, malformed messages and write failures, published with the Spark Kafka sink) | Built |
| docker-compose with a single-node Kafka broker (KRaft mode), topic creation and inspection commands | Built |
| Run_Summary and Audit_Log written as JSON Lines (Spark for both stages), minimal | Built (minimal) |
| Parquet output in the File_Sink_Worker | Designed, not built |
| HTTP and syslog workers (`delivery.http`, `delivery.syslog`) with sender, mock endpoint, mock receiver | Designed, not built |
| Retry topics with delays (`delivery.file.retry`, `delivery.http.retry`, `delivery.syslog.retry`) | Designed, not built |
| Per-destination circuit breaker, rate limits, delivery time budget, bounded worker pool | Designed, not built |
| Dedicated topics and partitions for the top tenants | Designed, not built |
| Kafka Connect file, S3 and HTTP sinks as an alternative to the custom worker | Designed, not built |
| Kafka ACL considerations (producer and consumer principals per topic) | Designed, not built |
| Continuous micro-batch triggers (for example every 30 to 60 seconds) | Designed, not built |
| Wildcard rule type ("all products") as a possible extension | Designed, not built (risk described in the design document) |
| Customer portal and configuration updates | Out of scope |
| Database-backed configuration service, self-service portal, near-real-time config reload, streaming ingestion, multi-node deployment, audit store, replay tooling, lag monitoring | Designed, not built |

## Glossary

- **Forwarding_System**: The complete Event Forwarding application described in this document: the Spark processing job, Kafka, and the File_Sink_Worker.
- **Event**: A JSON object with envelope fields `event_id`, `product`, `schema`, `tenant_id` and `data`.
- **Spark**: Apache Spark, used through PySpark (DataFrame API and Structured Streaming) in local mode, for both the processing stage and the File_Sink_Worker.
- **Landing_Zone**: The input directory containing JSON Lines files with Events for all products and tenants, read through a Structured Streaming file source (`readStream`, lines read as text).
- **Checkpoint_Location**: The directory in which the processing-stage Structured Streaming query stores which files have been processed, so that they are not reprocessed after a restart. It is the local substitute for Databricks Auto Loader.
- **Sink_Checkpoint_Location**: The directory, separate from the Checkpoint_Location, in which the File_Sink_Worker's Structured Streaming query stores the Kafka offsets of completed Micro_Batches. It replaces any manual offset commit.
- **Micro_Batch**: One unit of work of the streaming query, identified by a batch id. With trigger `availableNow`, one run processes all available input in one or more Micro_Batches and then stops.
- **Event_Path**: All Spark processing steps that operate on Event data (read, parse, validate, filter, shape, guard, publish, audit). The File_Sink_Worker modules (read from Kafka, parse, Sink_Isolation_Check, write, dead-letter, audit) are part of the Event_Path for the purposes of the bans in Requirement 11.
- **Tenant**: A customer identified by a UUID in `tenant_id`.
- **Valid_Tenant_Id**: A `tenant_id` string that parses as a UUID in canonical form.
- **Forwarding_Rule**: A configuration entry keyed by (tenant_id, product, schema) that defines whether and how matching Events are forwarded.
- **Config_Store**: The component that loads, validates and serves Forwarding_Rules, Destination definitions and Kafka settings from a configuration file.
- **Destination**: A customer-owned target of one kind: `file`, `http` or `syslog`, with kind-specific settings and an output format.
- **Router**: The component that selects, for each Event, the Forwarding_Rules that apply to it.
- **Transformer**: The component that applies field inclusion and constant fields to an Event.
- **Constant_Field**: A column added by a Forwarding_Rule with a fixed literal value of type string, number or boolean. It is the only supported way to add a column. The product has no calculated fields and no expression language.
- **Kafka_Topic**: A named Kafka log shared by all Destinations of one kind. Topic names are `delivery.file`, `delivery.http`, `delivery.syslog`, and the variants `<topic>.retry` and `<topic>.dlq`.
- **Delivery_Message**: A Kafka message with key `<tenant_id>|<destination_id>` and a JSON value that carries the envelope fields `tenant_id`, `destination_id`, `product`, `schema`, `event_id` and the shaped payload in the field `payload`.
- **Kafka_Publish_Stage**: The last Spark step of the Event_Path. It builds the columns `topic`, `key` and `value` for each guarded record and writes them with the Spark Kafka sink.
- **File_Sink_Worker**: A Spark Structured Streaming query that reads `delivery.file` from Kafka (`readStream`, format `kafka`), with trigger `availableNow` by default and its own Sink_Checkpoint_Location, and that in `foreachBatch` re-checks isolation, groups records per Destination and writes JSONL files to File_Destinations.
- **Kafka_Shaped_Rows**: A Spark DataFrame with the columns of the Spark Kafka source: `key` (binary), `value` (binary), `topic`, `partition`, `offset` and `timestamp`. It is the input of the Sink_Transform, supplied by the Kafka source in production and built from literal rows in tests, so that tests need no Kafka.
- **Sink_Transform**: The single function of the File_Sink_Worker that takes a Kafka_Shaped_Rows DataFrame and the configuration and returns the DataFrames of deliverable records, dead-letter records and audit records, using Spark column operations, filters and joins only. `foreachBatch` and the tests call the same Sink_Transform.
- **Dead_Letter_Topic**: The Kafka topic `delivery.file.dlq`, which receives Delivery_Messages that the File_Sink_Worker could not or must not write, together with a reason header.
- **Delivery_Batch**: The Delivery_Messages of one Destination and one topic partition within one Micro_Batch of the File_Sink_Worker, written to one file whose name is derived from the offset range of that Delivery_Batch.
- **Default_Deny**: The rule that an Event is forwarded only when an enabled Forwarding_Rule matches it by exact (tenant_id, product, schema) key.
- **Run_Summary**: A Spark output that contains the read, rejected, dropped and published counts of the processing stage, produced by Spark aggregations or query progress.
- **Worker_Summary**: A JSON output of the File_Sink_Worker with delivered and dead-lettered counts in total and per tenant and Destination, obtained from Spark aggregations.
- **File_Destination**: A Destination that receives JSONL files under a tenant-specific directory, written by the File_Sink_Worker.
- **HTTP_Destination**: A Destination of kind `http` that POSTs Events to a customer-provided URL. Designed, not built.
- **Syslog_Destination**: A Destination of kind `syslog` that sends Events as syslog messages to a customer-provided host and port. Designed, not built.
- **Isolation_Guard**: The final Spark filter or anti-join step that runs immediately before the Kafka_Publish_Stage, verifies that each outgoing record's tenant, product and schema match the Forwarding_Rule and Destination that it is being published for, and routes violating records to the Audit_Log. The File_Sink_Worker applies a second, independent check (Sink_Isolation_Check).
- **Sink_Isolation_Check**: The File_Sink_Worker check, implemented as a Spark filter or anti-join against a small DataFrame of (destination_id, owning tenant_id) built from the configuration, that the tenant_id of a Delivery_Message equals the owning tenant_id of its destination_id.
- **Config_Module**: The small module that loads and validates the configuration on the driver. It is the only module exempt from the bans of Requirement 11.11.
- **Rejected_Output**: A Spark output directory where the Forwarding_System records rejected lines and Events with a reason code.
- **Audit_Log**: An append-only set of JSON Lines outputs of delivery decisions with counts and identifiers but without payload data. Spark writes the processing-stage entries under `<audit_root>/processing/`, and the File_Sink_Worker writes the delivery-stage entries under `<audit_root>/delivery/`.
- **Acceptance_Scenario**: The five-event scenario (e1 to e5) and sample configuration defined in Requirement 10.
- **Design_Document**: The deliverable document of at most 3 pages and about 1,500 words described in Requirement 13.

## Requirements

### Requirement 1: Read Events from the Landing Zone

**User Story:** As a platform operator, I want the Forwarding_System to read Events from a landing-zone directory, so that events from all products and tenants can be processed in one run.

#### Acceptance Criteria

1. WHEN the Spark job is started with a Landing_Zone path that exists and is a directory, THE Forwarding_System SHALL read every file in that directory whose name ends in `.jsonl` (case-sensitive) with a Spark Structured Streaming file source (`readStream`, JSON lines read as text) configured with a Checkpoint_Location, without descending into subdirectories, and SHALL ignore all other files.
2. WHEN the Landing_Zone directory contains zero `*.jsonl` files, THE Forwarding_System SHALL complete the run with exit status 0, process zero Events, publish zero Delivery_Messages, and write nothing to the Rejected_Output.
3. WHEN a line in a JSONL file is empty or contains only whitespace, THE Forwarding_System SHALL skip the line with a Spark filter, without writing it to the Rejected_Output.
4. IF a non-blank line in a JSONL file is not valid JSON, THEN THE Forwarding_System SHALL write the line text and its source file name to the Rejected_Output with reason `malformed_json` using Spark, and continue with the remaining lines.
5. IF a line is valid JSON but its top-level value is not a JSON object, THEN THE Forwarding_System SHALL write the line and its source file name to the Rejected_Output with reason `malformed_json` using Spark, and continue with the remaining lines.
6. IF an Event lacks any of the fields `event_id`, `product`, `schema`, `tenant_id` or `data`, or any of these fields is present with a JSON `null` value, THEN THE Forwarding_System SHALL write the Event and its source file name to the Rejected_Output with reason `missing_envelope_field` using Spark and continue with the remaining lines.
7. IF all of the fields `event_id`, `product`, `schema`, `tenant_id` and `data` are present and non-null and the `data` field of an Event is not a JSON object, THEN THE Forwarding_System SHALL write the Event and its source file name to the Rejected_Output with reason `invalid_data` using Spark and continue with the remaining lines.
8. IF the Landing_Zone path does not exist or is not a directory, THEN THE Forwarding_System SHALL exit with a non-zero status, write an error message that includes the path to standard error, and process zero Events.
9. IF a `*.jsonl` file in the Landing_Zone cannot be opened or read, THEN THE Forwarding_System SHALL write an error message naming the file to standard error, continue with the remaining files, and exit with a non-zero status after all files are attempted.
10. WHEN the Spark job is restarted with the same Checkpoint_Location, THE Forwarding_System SHALL NOT reprocess files that the streaming query has already processed.
11. THE Forwarding_System SHALL use the trigger `availableNow` as the default run mode of the Spark job, so that a run processes all available input and then stops with exit status 0 when no error occurred.

### Requirement 2: Forwarding Configuration Model

**User Story:** As a customer, I want to configure forwarding per product, schema and tenant, so that I control which events are forwarded and in what shape.

#### Acceptance Criteria

1. WHEN the Spark job or the File_Sink_Worker starts, THE Config_Store SHALL load Forwarding_Rules, Destination definitions and Kafka settings (bootstrap servers) from a single JSON or YAML configuration file whose path is given on the command line.
2. THE Config_Store SHALL represent each Forwarding_Rule with a tenant_id, a product, a schema, a boolean `enabled` flag, an optional list of 1 to 200 included field names, an optional map of 1 to 100 Constant_Fields, and a list of 1 to 20 destination references.
3. THE Config_Store SHALL represent each Destination with an identifier, an owning tenant_id, a kind (`file`, `http` or `syslog`), an output format and the settings required by its kind.
4. IF the configuration file path is missing from the command line, does not exist, cannot be read, or its content cannot be parsed as JSON or YAML, THEN THE Forwarding_System SHALL exit with a non-zero status before reading any Event or Delivery_Message and report an error identifying which of these conditions occurred.
5. IF a Forwarding_Rule has a tenant_id that is not a Valid_Tenant_Id, THEN THE Config_Store SHALL reject the configuration and report an error identifying that rule.
6. IF a Forwarding_Rule references a Destination whose owning tenant_id differs from the rule's tenant_id, THEN THE Config_Store SHALL reject the configuration and report an error identifying the rule and the Destination.
7. IF a Forwarding_Rule references a Destination identifier that is not defined in the configuration file, THEN THE Config_Store SHALL reject the configuration and report an error identifying the rule and the undefined identifier.
8. IF a Destination has an output format that its kind does not support, a kind other than `file`, `http` or `syslog`, a missing required kind-specific setting, an identifier that duplicates another Destination's identifier, or an identifier that does not match the pattern `[A-Za-z0-9_-]{1,64}`, THEN THE Config_Store SHALL reject the configuration and report an error identifying that Destination.
9. IF the configuration file fails validation, THEN THE Forwarding_System SHALL exit with a non-zero status before reading any Event or Delivery_Message, publish zero Delivery_Messages, write nothing to any Destination, and report every validation error found together with the identifier of the offending rule or Destination.
10. WHEN the Spark job or the File_Sink_Worker starts, THE Forwarding_System SHALL read the configuration file exactly once, so that a configuration change made while the process is running does not affect that process and applies from the next start.
11. THE Forwarding_System SHALL obtain all configuration from the JSON or YAML configuration file and SHALL use no database for configuration.
12. WHEN a Forwarding_Rule references a Destination of kind `http` or `syslog` that passed validation, THE Forwarding_System SHALL publish zero Delivery_Messages for that Destination and SHALL record one Audit_Log entry per affected Event with decision `dropped` and reason `destination_kind_not_built`.
13. WHERE a Destination of kind `file` has the output format `parquet`, THE File_Sink_Worker SHALL write zero files for that Destination and SHALL record one Audit_Log entry per affected Delivery_Message with decision `dropped` and reason `format_not_built`.

### Requirement 3: Event Selection

**User Story:** As a customer, I want to forward only the events I selected, so that unselected products and schemas never leave the platform.

#### Acceptance Criteria

1. WHEN an Event is processed, THE Router SHALL select only Forwarding_Rules whose tenant_id, product and schema equal the Event's tenant_id, product and schema, where tenant_id values are compared after normalization to lowercase canonical UUID form (8-4-4-4-12 hexadecimal) and product and schema values are compared as case-sensitive exact strings with no trimming of leading or trailing whitespace.
2. IF no enabled Forwarding_Rule matches an Event, THEN THE Router SHALL drop the Event without publishing it to any Kafka_Topic and SHALL record one Audit_Log entry containing the Event identifier, the Event's tenant_id, product and schema, a timestamp, and the reason `no_matching_rule`.
3. WHILE a Forwarding_Rule has `enabled` set to false, THE Router SHALL treat the Forwarding_Rule as not matching any Event.
4. WHEN more than one enabled Forwarding_Rule matches an Event, THE Router SHALL process the Event once for each matching Forwarding_Rule, and a failure while processing the Event for one Forwarding_Rule SHALL NOT prevent or alter its processing for the other matching Forwarding_Rules.
5. THE Router SHALL perform no wildcard or prefix matching on tenant_id, product or schema.
6. IF an Event's tenant_id is missing or cannot be parsed as a UUID, THEN THE Router SHALL drop the Event without publishing it to any Kafka_Topic and SHALL record one Audit_Log entry containing the Event identifier (if present), a timestamp, and the reason `invalid_tenant_id`.
7. THE Router SHALL forward an Event only when an enabled Forwarding_Rule matches it by exact key (Default_Deny), and THE Forwarding_System SHALL provide no "forward all data" mode, so that an Event of a product or schema without an explicit Forwarding_Rule is never forwarded.
8. THE Forwarding_System SHALL treat the customer portal and configuration updates as out of scope and SHALL accept a valid configuration file as its only configuration input, in which any "all my products" choice of a customer is already expanded into explicit per-product and per-schema Forwarding_Rules.
9. THE Design_Document SHALL state the Default_Deny choice and the out-of-scope portal assumption (see Requirement 13), and SHALL describe a wildcard rule type as a possible extension together with its risk.

### Requirement 4: Payload Shaping

**User Story:** As a customer, I want to choose which payload fields are forwarded and to add extra fields, so that events arrive in the shape my systems need.

#### Acceptance Criteria

1. WHERE a Forwarding_Rule defines an included-fields list, THE Transformer SHALL output only those top-level `data` fields whose names appear in the list, with their original values unchanged.
2. WHERE a Forwarding_Rule defines an included-fields list, IF a listed field name is absent from the Event's `data`, THEN THE Transformer SHALL omit that field from the output and still output the other listed fields.
3. WHERE a Forwarding_Rule defines no included-fields list, THE Transformer SHALL output all top-level fields of `data` with their original values unchanged.
4. WHERE a Forwarding_Rule defines constant fields, THE Transformer SHALL add each constant field to the output with its configured value and under its configured field name.
5. WHEN the Transformer outputs an Event, THE Transformer SHALL include the Event envelope fields `event_id`, `product`, `schema` and `tenant_id` with their original values alongside the shaped payload, and the Kafka_Publish_Stage SHALL carry them in the Delivery_Message.
6. IF a constant field name equals the name of an included field or of another constant field in the same Forwarding_Rule, THEN THE Config_Store SHALL reject the Forwarding_Rule at load time with an error identifying the duplicate field name, and SHALL leave previously loaded configuration unchanged.
7. IF a constant field value is not a JSON string, number or boolean, or a Forwarding_Rule defines a calculated field or any other mechanism that adds a column from the fields of an Event, THEN THE Config_Store SHALL reject the Forwarding_Rule at load time with an error identifying the rule and the field name.
8. THE README and the Design_Document SHALL state that the only way a Forwarding_Rule adds a column is a constant field with a fixed string, number or boolean value, and that calculated fields and expressions are not supported.

### Requirement 5: File Destination and File_Sink_Worker

**User Story:** As a customer, I want events written as files to my storage location, so that my data lake can ingest them.

#### Acceptance Criteria

1. WHEN a Micro_Batch of the File_Sink_Worker contains Delivery_Messages from `delivery.file` that pass the Sink_Isolation_Check, THE File_Sink_Worker SHALL group them into one Delivery_Batch per Destination and topic partition and SHALL write each Delivery_Batch as one file under `<output_root>/<tenant_id>/<destination_id>/`, creating any missing directories in that path, using a Spark `DataFrame.write` partitioned by Destination, without iterating over rows on the driver.
2. THE File_Sink_Worker SHALL write each file in the JSONL format, with one UTF-8 encoded JSON object per Delivery_Message (the envelope fields and the payload fields), lines separated by a single `\n` character and no embedded unescaped newlines within an object.
3. THE File_Sink_Worker SHALL write one file per Delivery_Batch and SHALL NOT write one file per Delivery_Message.
4. WHEN the File_Sink_Worker writes the output of a Micro_Batch, THE File_Sink_Worker SHALL write it to a temporary location on the same file system as `<output_root>` and SHALL rename each file atomically to its final name under the Destination directory before the Micro_Batch is considered complete, so that a reader listing the Destination directory never observes a partial file under a final name.
5. THE File_Sink_Worker SHALL derive the output path only from the owning tenant_id of the Destination according to the configuration and from the destination_id, and SHALL NOT use any Delivery_Message value other than the validated destination_id in any path component.
6. THE File_Sink_Worker SHALL name each file deterministically from the destination_id and the topic, partition, and minimum and maximum offset of the Delivery_Batch within the Micro_Batch (for example `<destination_id>-<topic>-<partition>-<min_offset>-<max_offset>.jsonl`), so that a replay of the same offset range overwrites the existing file and does not duplicate records.
7. THE File_Sink_Worker SHALL compute the minimum and maximum offset per Destination, topic and partition with a Spark aggregation or window expression, SHALL carry the resulting file-name column on the records, and SHALL NOT collect Event rows or aggregate metadata to the driver, so that the only driver-side loop is a loop over the Destinations in the configuration.
8. THE File_Sink_Worker SHALL commit no Kafka offsets manually and SHALL rely on the Sink_Checkpoint_Location for restart safety, so that after a restart the query resumes after the last completed Micro_Batch.
9. THE File_Sink_Worker SHALL perform the work of a Micro_Batch in `foreachBatch` and SHALL let `foreachBatch` raise an exception when the Micro_Batch cannot be completed (including a failed rename and a failed Dead_Letter_Topic publish), so that the Sink_Checkpoint_Location does not advance past that Micro_Batch.
10. IF a File_Destination path would resolve outside `<output_root>`, including through `..` segments, absolute paths, or symbolic links, THEN THE Config_Store SHALL reject the Destination at load time, SHALL return an error indicating the path is outside the output root, and SHALL leave previously loaded Destinations unchanged.
11. IF a write or rename for a Destination fails within a Micro_Batch, THEN THE File_Sink_Worker SHALL delete the temporary files of that Destination, SHALL leave any existing final file of that Destination unchanged, and SHALL publish every Delivery_Message of that Destination in that Micro_Batch unchanged (key and value) to the Dead_Letter_Topic with the Spark Kafka sink, with the reason header `write_failed` and an error category, selected from the Micro_Batch DataFrame with a Spark filter.
12. WHEN the File_Sink_Worker is run in its default mode (trigger `availableNow`), THE File_Sink_Worker SHALL process the Delivery_Messages up to the end offsets available at start in one or more Micro_Batches, write the Worker_Summary, and exit with status 0 when no error occurred.
13. WHEN the Spark job is repeated with the same Landing_Zone, configuration and Checkpoint_Location, THE Forwarding_System SHALL publish zero additional Delivery_Messages, because the Checkpoint_Location tracks processed files.

### Requirement 6: HTTP Destination (designed, not built)

**User Story:** As a customer, I want events POSTed to my URL, so that my webhook can receive them.

#### Acceptance Criteria

1. THE Design_Document SHALL describe the HTTP_Destination, including the topics `delivery.http`, `delivery.http.retry` and `delivery.http.dlq`, the consuming HTTP worker, the request format (Content-Type `application/json` for a single event, `application/x-ndjson` for a batch of 2 or more events), connect and total timeouts with their valid ranges and defaults, the classification of response statuses and connection failures as delivered, retryable or not deliverable, the handling of redirects to a different scheme, hostname or port, and the dead-lettering of batches that are not deliverable.
2. THE Design_Document SHALL state that tenant B's intended production target is an HTTP_Destination, and that the build delivers tenant B's events to a File_Destination instead.

### Requirement 7: Syslog Destination (designed, not built)

**User Story:** As a customer with a SIEM that only accepts syslog, I want events sent as syslog messages, so that my SIEM can ingest them.

#### Acceptance Criteria

1. THE Design_Document SHALL describe the Syslog_Destination, including the topics `delivery.syslog`, `delivery.syslog.retry` and `delivery.syslog.dlq`, the consuming syslog worker, message framing (RFC 5424 over TCP), the mapping of each Event field to an RFC 5424 field (PRI, TIMESTAMP, HOSTNAME, APP-NAME, MSGID, STRUCTURED-DATA, MSG), the behavior when a mapped Event field is absent, and its failure handling for connection failure, send failure, and send timeout.
2. THE Design_Document SHALL describe for the Syslog_Destination the octet-counting framing, the maximum message length of 8,192 bytes, the handling of a record that would exceed that length, and the retry policy that applies to network delivery.

### Requirement 8: Strict Data Isolation

**User Story:** As a security reviewer, I want cross-tenant and cross-product forwarding to be impossible by construction and verified by tests at several layers, so that no customer receives data they must not receive.

#### Acceptance Criteria

1. IF an Event has a `tenant_id` that is empty, missing, null or not a Valid_Tenant_Id, THEN THE Forwarding_System SHALL drop the Event before routing, write the Event to the Rejected_Output with reason `invalid_tenant_id`, publish the Event to zero Kafka_Topics, and continue processing the remaining Events in the run.
2. THE Router SHALL resolve Destinations only through the matched Forwarding_Rule.
3. WHEN a Forwarding_Rule is created, updated or loaded, THE Config_Store SHALL verify that every Destination referenced by the rule has the same owning tenant_id as the rule.
4. IF a Forwarding_Rule references a Destination whose owning tenant_id differs from the rule's tenant_id, THEN THE Config_Store SHALL reject the rule, keep the previously stored configuration unchanged, and return an error indicating a tenant ownership mismatch.
5. WHEN the Forwarding_System is about to publish a record to Kafka, THE Isolation_Guard SHALL verify, as a Spark filter or anti-join step applied before the Kafka_Publish_Stage, that the record's tenant_id equals the Destination's owning tenant_id and that the record's (product, schema) pair equals the (product, schema) pair of at least one enabled Forwarding_Rule that references the Destination.
6. IF the Isolation_Guard detects a mismatch, THEN THE Forwarding_System SHALL block the publication of that record, route the record's identifiers to the Audit_Log output through Spark with reason `isolation_violation`, without Python row loops, continue all other publications in the run, and exit the run with a non-zero status after all other publications have finished.
7. WHEN a product has no enabled Forwarding_Rule for a tenant, THE Forwarding_System SHALL publish zero Events of that product for that tenant.
8. THE Forwarding_System SHALL take tenant_id for routing, message keys and output paths only from the validated Event envelope and the configuration, and SHALL ignore any tenant_id value in the `data` object.
9. WHEN the File_Sink_Worker processes a Micro_Batch, THE File_Sink_Worker SHALL apply the Sink_Isolation_Check as a Spark filter or anti-join, which verifies that the destination_id is defined in the configuration, that the tenant_id in the message value equals the owning tenant_id of that destination_id according to the configuration, that the tenant_id in the message key equals the tenant_id in the message value, and that the message key equals `<tenant_id>|<destination_id>` of the message value.
10. IF the Sink_Isolation_Check fails for a Delivery_Message, THEN THE File_Sink_Worker SHALL write the Delivery_Message to no File_Destination, publish it unchanged (key and value) to the Dead_Letter_Topic with the Spark Kafka sink and the reason header `isolation_violation`, record one Audit_Log entry with decision `dead-lettered` and reason `isolation_violation`, continue with the remaining Delivery_Messages, and exit with a non-zero status after the run.
11. WHEN the File_Sink_Worker writes the files of a Micro_Batch, THE File_Sink_Worker SHALL derive each output path only from the configured owner and destination_id of one Destination, and SHALL run the per-Destination write step in a driver loop over the configuration (not over rows) that writes only the rows of that single Destination, selected with a Spark filter, to that Destination's own output path.
12. THE Forwarding_System SHALL include property-based isolation tests that run 2 generated cases of multi-tenant inputs and configurations, and that verify for the Spark logic that every record prepared for publication has a tenant_id equal to the Destination's owning tenant_id and a (product, schema) pair enabled by that tenant.
13. THE Forwarding_System SHALL include property-based isolation tests of the File_Sink_Worker that run 2 generated cases, feeding the Sink_Transform a Kafka_Shaped_Rows DataFrame that contains valid and tampered Delivery_Messages, and that verify every written record belongs to a Destination owned by the record's tenant_id and that every tampered Delivery_Message is published to the Dead_Letter_Topic and not written.
14. THE Forwarding_System SHALL include a test that every Event with no enabled Forwarding_Rule produces zero published Delivery_Messages and zero written records.
15. THE Design_Document SHALL describe how isolation is enforced at three layers (config validation and routing with the Isolation_Guard before Kafka publish, the Sink_Isolation_Check in the File_Sink_Worker, and path derivation from the configured owner with per-destination writers), tested (property-based and scenario tests) and audited (Audit_Log), and SHALL list the steps a security reviewer follows to verify each of these.

### Requirement 9: Handling Unreliable Customer Destinations

**User Story:** As a platform operator, I want a slow or failing customer destination to affect only that destination, so that other customers still receive their events on time.

#### Acceptance Criteria

1. THE Forwarding_System SHALL decouple processing from delivery with Kafka, so that a slow or failing Destination does not slow down or fail the Spark job, and so that the delivery outcome of a record for one Destination does not change the delivery outcome of any record for any other Destination.
2. WHILE a Destination is slow, THE Forwarding_System SHALL accumulate the undelivered Delivery_Messages in Kafka, so that the slowness only grows the lag of the File_Sink_Worker's query behind the end offsets and does not grow the Spark job's latency.
3. IF a write or rename to a File_Destination fails (Requirement 5.11), THEN THE File_Sink_Worker SHALL publish the affected Delivery_Messages unchanged to the Dead_Letter_Topic, record one Audit_Log entry per Delivery_Message with decision `dead-lettered`, the tenant, the Destination and an error category, and continue delivering the Micro_Batch to all other Destinations, so that a failure of one Destination does not block the others in the same Micro_Batch.
4. IF publishing the Delivery_Messages of a Micro_Batch to the Dead_Letter_Topic fails, THEN THE File_Sink_Worker SHALL raise an exception from `foreachBatch` so that the Sink_Checkpoint_Location does not advance past that Micro_Batch, SHALL record the failure in the Audit_Log, SHALL count the affected Delivery_Messages as neither delivered nor dead-lettered, and SHALL finish with a non-zero exit status.
5. WHEN a Spark run finishes, THE Forwarding_System SHALL write a Run_Summary of read, rejected, dropped and published event counts, as non-negative integers, in total and for each tenant and Destination that had at least one event read, obtained from Spark aggregations or streaming query progress, without collecting event rows to the driver.
6. WHEN a File_Sink_Worker run finishes, THE File_Sink_Worker SHALL write a Worker_Summary of delivered and dead-lettered counts, as non-negative integers, in total and for each tenant and Destination, obtained from Spark aggregations without collecting event rows to the driver.
7. THE Design_Document SHALL describe, as designed mitigation, the head-of-line blocking risk of shared topics (a slow Destination delays the other Destinations whose messages share its partition) and the mitigations: per-kind retry topics with delays, pausing a partition or moving a failing Destination's messages to a retry topic, a per-destination circuit breaker, per-destination rate limits, a bounded worker pool, and dedicated topics and partitions for the top tenants.
8. THE Design_Document SHALL describe, as designed but not built, the following mechanisms for unreliable network Destinations: retry with exponential backoff and jitter through retry topics, a per-destination delivery time budget, a bounded worker pool for concurrent delivery, a per-destination circuit breaker, per-destination rate limiting, a replay path from the Dead_Letter_Topic, and per-tenant delivery lag monitoring.

### Requirement 10: Acceptance Scenario

**User Story:** As a reviewer, I want a runnable sample that reproduces the assignment's scenario, so that I can verify the core behavior quickly.

#### Acceptance Criteria

1. THE Forwarding_System SHALL include sample input containing exactly five events, e1 to e5, with field values identical to those given in the assignment, where tenant A has tenant_id `3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90` and tenant B has tenant_id `9d4e1a72-3c5b-4f8e-a1d2-6b7c8e9f0a13`.
2. THE Forwarding_System SHALL include a sample configuration in which tenant A forwards `sales/purchase` and `security/alert` to one File_Destination (`dest-a-file`) and has no forwarding rule for `identity`, and tenant B forwards `sales/purchase` to one File_Destination (`dest-b-file`) whose output directory is separate from tenant A's. The sample configuration SHALL additionally show, as a commented or disabled entry, an HTTP_Destination definition for tenant B as the intended production target.
3. WHEN the Spark job of the sample is run against a running Kafka broker, THE Forwarding_System SHALL publish exactly three Delivery_Messages to `delivery.file`, with the keys `3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90|dest-a-file` for e1 and e2 and `9d4e1a72-3c5b-4f8e-a1d2-6b7c8e9f0a13|dest-b-file` for e3, and SHALL publish zero Delivery_Messages for e4 and e5 to any topic.
4. WHEN the File_Sink_Worker has consumed the three Delivery_Messages of the sample, THE File_Sink_Worker SHALL have written e1 and e2 to tenant A's File_Destination, with exactly one record of each event.
5. WHEN the sample is run, THE Forwarding_System SHALL omit the field `internal_note` from the copy of e1 in tenant A's File_Destination, and SHALL retain all other fields of e1 that the tenant A configuration does not exclude.
6. WHEN the sample is run, THE Forwarding_System SHALL write e3 exactly once to tenant B's File_Destination.
7. WHEN the sample is run, THE Forwarding_System SHALL write e4 (empty tenant_id) and e5 (identity product, not configured for its tenant) to zero Destinations, so that each Destination's output and `delivery.file.dlq` contain no record of either event.
8. WHEN the sample is run, THE Forwarding_System SHALL write zero tenant B events to tenant A's File_Destination and zero tenant A events to tenant B's File_Destination.
9. WHERE the sample configuration defines a Constant_Field `region` with value `"eu"` on `security/alert`, THE Forwarding_System SHALL include `region` equal to `"eu"` and the original `risk` value unchanged in the Delivery_Message and in the written file for e2.
10. THE Forwarding_System SHALL include an acceptance test, marked as requiring Kafka and run with docker-compose, that runs the Spark job and the File_Sink_Worker on the sample, asserts criteria 3 to 9 of this requirement on the consumed messages (topic and key) and on the files produced by the File_Sink_Worker after consuming from Kafka, and fails if any one of those assertions does not hold.
11. THE Forwarding_System SHALL include a variant of the acceptance test that runs without Docker, using the publish-ready DataFrame of the Kafka_Publish_Stage (columns `topic`, `key`, `value`), converting it to a Kafka_Shaped_Rows DataFrame (adding `partition`, `offset` and `timestamp` columns, with `key` and `value` cast to binary) and passing it to the same Sink_Transform and per-Destination write step that `foreachBatch` uses, and that asserts criteria 3 to 9 of this requirement.
12. IF the sample input or sample configuration is missing or cannot be parsed, THEN THE Forwarding_System SHALL exit with a non-zero status, report an error that identifies the unreadable file, and publish zero Delivery_Messages.

### Requirement 11: Processing Engine and Scale Reasoning

**User Story:** As a tech lead, I want the processing engine chosen with justified reasoning about scale and cost, so that the design holds at today's volume and at 3x.

#### Acceptance Criteria

1. THE Forwarding_System SHALL perform the read (Structured Streaming file source), parsing, tenant validation, filtering, joining with Forwarding_Rules, payload shaping, the Isolation_Guard, the Kafka publish with the Spark Kafka sink, the Rejected_Output and the processing-stage Audit_Log using Apache PySpark (DataFrame API and Structured Streaming), run in local mode.
2. THE Forwarding_System SHALL filter Events against the set of configured (tenant_id, product, schema) keys before the join with Forwarding_Rules and before payload shaping, so that 100% of Events whose (tenant_id, product, schema) key is not configured are excluded from the shaping step input.
3. THE Kafka_Publish_Stage SHALL choose the Kafka_Topic for each record from the Destination kind through the Spark `topic` column, and SHALL NOT create a Kafka_Topic per tenant, product or Destination.
4. THE Design_Document SHALL state that, in the full product, network delivery to HTTP_Destinations and Syslog_Destinations is performed by per-kind delivery workers that consume `delivery.http` and `delivery.syslog`, that the implementation technology of these HTTP and syslog workers is an open decision, and that this delivery is designed, not built. The File_Sink_Worker is not part of this open decision: it is built with Spark Structured Streaming reading from Kafka.
5. THE Design_Document SHALL state why file delivery is a separate Spark Structured Streaming query that reads from Kafka and not a file write inside the processing job (for example: delivery is decoupled from processing, per-destination failures and lag stay isolated, the Sink_Checkpoint_Location and deterministic file names give idempotent output, and per-destination logic is separated from the processing job).
6. THE README SHALL list the exact version number of Python, Java, PySpark, the Spark Kafka connector package, the Kafka client library, the Kafka image and every other dependency, the command to install them, and the commands to run the Spark job and the File_Sink_Worker.
7. THE Design_Document SHALL state where Spark is used (bulk filtering, shaping and Kafka publish at 2 to 6 TB/day) and where the design uses a different mechanism (small per-tenant configuration handled on the driver, and Kafka for decoupling delivery), with at least one reason for each, and SHALL state that the File_Sink_Worker uses Spark Structured Streaming reading from Kafka and that the implementation technology of the designed-only HTTP and syslog workers is an open decision.
8. THE Design_Document SHALL describe each of these full-product components: ingestion from the landing zone, configuration service and portal, routing stage, Kafka topics, per-kind delivery workers, dead-letter and replay, and audit. THE Design_Document SHALL label each component as "built" or "designed only".
9. THE Design_Document SHALL state, for both 40k events/sec and 120k events/sec peak: the expected behavior, the first component expected to fail and why (including Kafka partition count and broker throughput), the effect of the top 10 tenants producing about 60% of volume (hot partitions for a few keys), and the main cost drivers, with each cost driver ranked by relative contribution.
10. THE Design_Document SHALL describe how the Forwarding_System meets the 5-minute end-to-end delivery latency target, including continuous micro-batch triggers (for example every 30 to 60 seconds) in the full product versus trigger `availableNow` in the build, the latency budget allocated to each pipeline stage including Kafka and the worker, and the skew handling method for large tenants.
11. THE Forwarding_System SHALL use PySpark for the processing stage and for the File_Sink_Worker, and, in all event-handling code of both stages (every module except the Config_Module), SHALL NOT use pandas or pyarrow as a writer, SHALL NOT call `collect()`, `toPandas()`, `take` or `head` on Event data or on aggregates derived from Event data, SHALL NOT iterate over Event rows in Python on the driver, and SHALL NOT use Python UDFs (including `udf`, `pandas_udf` and `mapInPandas`) on the Event_Path.
12. WHERE driver-side handling is needed, THE Forwarding_System SHALL restrict it to the small configuration (Forwarding_Rules, Destinations and Kafka settings), including the per-Destination loop of the File_Sink_Worker, which iterates over configuration only.
13. THE Forwarding_System SHALL include an automated test that statically scans the source code of all event-handling modules of the processing stage and of the File_Sink_Worker (for example with the Python `ast` module), and fails if any of them contains any of the imports, calls or UDF usages listed in criterion 11, and SHALL include in that test an allow-list limited to the Config_Module.
14. THE Design_Document SHALL state the scalability constraint of criterion 11, SHALL give at least one reason (the driver would become a bottleneck and memory limit at 2 to 6 TB/day), and SHALL state that the same bans apply to the File_Sink_Worker modules and that only the Config_Module is exempt.

### Requirement 12: Tests, Setup and README

**User Story:** As a reviewer, I want tests for the riskiest parts and a README that gets me from clone to a working run, so that I can evaluate the submission quickly.

#### Acceptance Criteria

1. THE Forwarding_System SHALL include automated tests that run with a single documented command from the repository root and cover configuration validation, event selection, payload shaping with constant fields, tenant_id validation, the publish-ready DataFrame (topic, key, value), the File_Sink_Worker file output (through the Sink_Transform on a Kafka_Shaped_Rows DataFrame), the sink-side isolation check, write failure with dead-lettering, and the Acceptance_Scenario.
2. WHEN the single test command is run on a clean checkout after the documented dependency installation, THE Forwarding_System SHALL run all tests that are not marked `kafka` with Spark in local mode, without Docker and without network access, and SHALL exit with code 0 if all of those tests pass and a non-zero code if any of them fails.
3. THE Forwarding_System SHALL mark every test that needs a running Kafka broker with a pytest marker `kafka` and a fixture that starts from the docker-compose broker, and SHALL skip those tests with a message naming the missing broker when the broker is not reachable.
4. THE Forwarding_System SHALL include exactly one Kafka integration test (Spark publish to the broker, worker consume from the broker, files and dead-letter topic inspected), the acceptance test of Requirement 10.10 and the sink checkpoint test of criterion 19, all marked `kafka`, and SHALL document a separate command that runs them.
5. THE Forwarding_System SHALL include property-based tests for isolation (Requirement 8.12 and 8.13) that run 2 generated cases per run each.
6. THE Forwarding_System SHALL include a test of Constant_Fields that asserts that string, number and boolean constant values are added to the payload unchanged, and that a rule defining a calculated field or a constant with another value type is rejected at load time.
7. THE Forwarding_System SHALL include a round-trip test that parses a configuration file, serializes the result, parses the serialized output again, and asserts that the first and second parsed results are equal.
8. THE Forwarding_System SHALL include a round-trip test that serializes a Delivery_Message as a Kafka_Shaped_Rows row, parses it again with the Sink_Transform, and asserts that the envelope fields, the key and the payload are equal.
9. THE README SHALL provide a copy-pasteable command for each of the following: installing dependencies, starting docker-compose Kafka, creating the topics (`delivery.file`, `delivery.file.dlq`, and the designed-only topics noted as not created), running the Spark job, running the File_Sink_Worker with trigger `availableNow`, running the tests without Docker, running the Kafka tests, inspecting the topics (for example with the Kafka console consumer in the container), and listing the contents of the output directories.
10. THE README SHALL state, for each tested component listed in criterion 1, the reason it was chosen for testing, and SHALL name isolation as the highest-priority component.
11. THE README SHALL contain three distinct sections that list, respectively, the features that were built, the features that were cut, and the features that are designed only.
12. THE Forwarding_System SHALL include a directory for the unedited AI interaction history at a path declared in the README (for example `docs/ai-log/`), and THE README SHALL contain a link to that path.
13. THE Forwarding_System SHALL include a checkpoint restart test that runs the Spark processing logic twice on the same Landing_Zone with the same Checkpoint_Location and asserts that the second run produces no additional publish-ready records.
14. THE Forwarding_System SHALL include a checkpoint incremental test that adds a new file to the Landing_Zone between two runs with the same Checkpoint_Location and asserts that the second run processes only the events of the new file.
15. THE Forwarding_System SHALL include a test that passes the same Kafka_Shaped_Rows DataFrame (same topic, partition and offset range) twice through the Sink_Transform and the per-Destination write step, and asserts that the Destination directory contains the same single file and no duplicate records.
16. THE Forwarding_System SHALL include a test that simulates a crash after the atomic rename and before the Micro_Batch completes, re-runs the write step on the same Kafka_Shaped_Rows DataFrame, and asserts that no duplicate records exist in the Destination directory.
17. THE Forwarding_System SHALL include a test that verifies the `foreachBatch` function raises an exception, so that the Sink_Checkpoint_Location does not advance, when the file write fails and the dead-letter publish also fails.
18. THE Forwarding_System SHALL include a test that a write failure for one Destination publishes only the rows of that Destination to the dead-letter output and still writes the files of the other Destinations of the same Micro_Batch.
19. THE Forwarding_System SHALL include a test that runs the File_Sink_Worker query twice with the same Sink_Checkpoint_Location against a running Kafka broker and asserts that the second run processes no additional Delivery_Messages. This test is marked `kafka`.

### Requirement 13: Design Document

**User Story:** As a hiring panel member, I want a concise design document, so that I can judge the candidate's decisions and prioritization.

#### Acceptance Criteria

1. THE Design_Document SHALL be delivered as `docs/design.md`, SHALL contain no more than 3 pages and no more than 1,500 words (excluding the diagram), and SHALL contain no more than one diagram.
2. THE Design_Document SHALL contain exactly these five top-level sections, each with at least 1 paragraph or list: Architecture, Data isolation, Operating it, Scale and cost, and Scope.
3. THE Design_Document SHALL, in the Operating it section, contain a separate subsection or labeled paragraph for each of these seven topics: unreachable or slow customer destinations, a noisy tenant, configuration mistakes, poison events, duplicate delivery, backlog recovery, and monitoring per tenant.
4. THE Design_Document SHALL state that delivery is at-least-once end to end (the Spark Kafka sink is at-least-once and file output is idempotent for a replayed offset range through deterministic names), SHALL describe how a customer deduplicates Events using `event_id`, and SHALL state that a re-consumed batch with a different offset range after a crash can duplicate records, which `event_id` covers.
5. THE Design_Document SHALL, in the Architecture section, explain why Kafka topics are few and shared by destination kind and not per tenant, product or Destination, including the arithmetic (about 15,000 tenants × about 3 forwarded products × about 2 destinations ≈ 90,000 to 100,000 topics, and several times more partitions), and the effect on broker metadata, controller recovery, file handles and operations.
6. THE Design_Document SHALL explain the choice of the message key `<tenant_id>|<destination_id>` (ordering and affinity per destination, partitioning of load across destinations, and the hot-partition effect of the top 10 tenants).
7. THE Design_Document SHALL describe head-of-line blocking on shared topics and its mitigations, as required by Requirement 9.7.
8. THE Design_Document SHALL describe Kafka Connect file, S3 and HTTP sinks as option 3 and as an alternative to the custom worker, and SHALL compare the options (option 1: Spark writes customer files directly; option 2: Kafka with custom workers, the chosen one; option 3: Kafka with Kafka Connect sinks) on isolation control, operations effort and failure isolation.
9. THE Design_Document SHALL describe isolation at three layers, as required by Requirement 8.15.
10. THE Design_Document SHALL describe replay from Kafka (running the File_Sink_Worker with a new Sink_Checkpoint_Location and explicit `startingOffsets` within the retention period, deterministic file names that overwrite for identical offset ranges, and republishing from the Dead_Letter_Topic after a fix), and SHALL state the retention period assumption.
11. THE Design_Document SHALL, in the Scope section, contain three labeled lists: what was built, what was cut (each item with a stated reason), and what would be done next with another week.
12. THE Design_Document SHALL state that a configuration change applies on the next start in the build and by near-real-time reload in the full product, that the build reads a JSON or YAML file while production would use a database-backed configuration service with a self-service portal, and SHALL state the outcome for Events in flight at the time of a change (delivered under the old configuration, delivered under the new configuration, or dropped), including Delivery_Messages already in Kafka.
13. IF the Design_Document exceeds 1,500 words (excluding the diagram) or contains more than one diagram, THEN THE Design_Document SHALL be treated as failing this requirement.
14. THE Design_Document SHALL state that the pipeline forwards only on an explicit exact-match Forwarding_Rule (Default_Deny), that "forward all data" is not a pipeline feature, and that the customer portal and configuration updates are out of scope because the portal is assumed to expand an "all my products" choice into explicit per-product and per-schema rules.
15. THE Design_Document SHALL describe a wildcard rule type as a possible extension and SHALL state its risk (new products would be forwarded without explicit opt-in) and the constraint that tenant_id is never a wildcard.
16. THE Design_Document SHALL state that the Landing_Zone is read with a Structured Streaming file source and Checkpoint_Location as the substitute for Databricks Auto Loader, and that the build runs trigger `availableNow`.
17. THE Design_Document SHALL list Kafka ACL considerations (a producer principal limited to writing `delivery.*` for the Spark job, a consumer principal per worker kind limited to reading its own topic and writing its own retry and dead-letter topics, and no customer access to Kafka), labeled as designed only.

### Requirement 14: Audit and Observability (minimal build)

**User Story:** As an operator and a security reviewer, I want a record of what was forwarded or dropped and why, so that I can investigate incidents without exposing payloads.

#### Acceptance Criteria

1. WHEN the Spark job makes a decision for an Event, THE Forwarding_System SHALL write the processing-stage Audit_Log as a Spark output in JSON Lines format under `<audit_root>/processing/`, with one valid JSON object per line and one line per Event decision, and SHALL NOT produce it with Python file appends.
2. WHEN the File_Sink_Worker makes a decision for a Delivery_Message, THE File_Sink_Worker SHALL write the delivery-stage Audit_Log as JSON Lines files under `<audit_root>/delivery/`, one valid JSON object per line, with deterministic file names derived from the topic, partition and offset range and with an atomic rename, so that a replay overwrites the file and does not duplicate entries.
3. THE Audit_Log SHALL record one entry per decision containing the event_id, tenant_id, product, schema, decision, reason code and Destination identifier, where decision is exactly one of `published`, `delivered`, `dropped`, `rejected` or `dead-lettered`, and where Spark writes `published`, `dropped` and `rejected` and the File_Sink_Worker writes `delivered`, `dropped` and `dead-lettered`.
4. IF the tenant_id of an Event is not valid, THEN THE Audit_Log SHALL record the entry with the tenant_id field omitted or null.
5. THE Audit_Log SHALL NOT contain any value from the `data` field or from the `payload` field of any Event or Delivery_Message, including for rejected and dead-lettered entries.
6. THE Forwarding_System SHALL NOT include in any log message, including Audit_Log entries, any `data` or `payload` field value, URL credentials, authorization header value or token value of a Destination.
7. IF the Audit_Log file cannot be created or an entry cannot be written, THEN THE Forwarding_System SHALL report an error indicating the audit write failure and SHALL NOT report the affected decision as successfully recorded.
8. WHERE a Destination defines an authorization header or token, THE Config_Store SHALL read the value from an environment variable reference and SHALL NOT accept a literal value for it in the configuration file.
9. IF a Destination's environment variable reference is unset or resolves to an empty string, THEN THE Config_Store SHALL reject that Destination's configuration with an error that names the variable but does not include any secret value.
10. THE Dead_Letter_Topic messages SHALL carry the reason and error category in Kafka headers and SHALL carry the original key and value unchanged, so that a replay after a fix republishes the original Delivery_Message.

### Requirement 15: Kafka Delivery Layer and Local Environment

**User Story:** As a developer, I want a local single-node Kafka and a fixed topic and message model, so that I can run and inspect the whole pipeline on one machine.

#### Acceptance Criteria

1. THE Forwarding_System SHALL define the Kafka_Topics by Destination kind: `delivery.file` (built), `delivery.file.dlq` (built), `delivery.file.retry` (designed only), `delivery.http`, `delivery.http.retry`, `delivery.http.dlq`, `delivery.syslog`, `delivery.syslog.retry` and `delivery.syslog.dlq` (designed only).
2. THE Kafka_Publish_Stage SHALL write each Delivery_Message with the message key `<tenant_id>|<destination_id>`, with the tenant_id in lowercase canonical UUID form, and with a JSON value that contains the fields `tenant_id`, `destination_id`, `product`, `schema`, `event_id` and `payload`.
3. THE Kafka_Publish_Stage SHALL publish with the Spark Kafka sink from the DataFrame columns `topic`, `key` and `value`, without collecting event rows to the driver.
4. WHEN the same Spark Micro_Batch is retried, THE Kafka_Publish_Stage MAY publish the same Delivery_Messages again, and THE Forwarding_System SHALL document this as at-least-once publication with `event_id` for deduplication.
5. THE Forwarding_System SHALL include a `docker-compose.yml` that starts a single-node Kafka broker in KRaft mode without Zookeeper, with a pinned image version and a listener reachable from the host on a documented port.
6. THE Forwarding_System SHALL include a script or documented command that creates the topics `delivery.file` and `delivery.file.dlq` with documented partition counts, and the README SHALL state that the designed-only topics are not created.
7. IF the Kafka broker is not reachable when the Spark job or the File_Sink_Worker starts, THEN THE Forwarding_System SHALL exit with a non-zero status, report an error that names the bootstrap servers, and advance no checkpoint.
8. THE File_Sink_Worker SHALL implement its batching, isolation, file naming and dead-letter logic in the Sink_Transform and the per-Destination write step, which accept a Kafka_Shaped_Rows DataFrame (`key`, `value`, `topic`, `partition`, `offset`, `timestamp`), so that the same functions run on the DataFrame of the Kafka source in `foreachBatch` and on a DataFrame built from literal rows in tests, without Kafka and without Docker, and THE Forwarding_System SHALL provide no in-memory consumer or producer abstraction.
9. THE File_Sink_Worker SHALL use the same Destination ownership data from the same configuration file as the Spark job, loaded once at start and passed to Spark as a small DataFrame of (destination_id, owning tenant_id, output path).
10. WHEN the File_Sink_Worker receives a Delivery_Message whose value cannot be parsed as JSON with the expected schema, or that lacks any of the fields `tenant_id`, `destination_id`, `product`, `schema`, `event_id` or `payload`, THEN THE File_Sink_Worker SHALL detect it with a Spark parse of the value (null result or null field), publish the Delivery_Message unchanged to the Dead_Letter_Topic with the reason header `malformed_message`, and continue with the remaining Delivery_Messages.
11. THE File_Sink_Worker SHALL read the `delivery.file` topic with a Spark Structured Streaming Kafka source, SHALL use trigger `availableNow` by default and a Sink_Checkpoint_Location distinct from the Checkpoint_Location of the processing stage, and SHALL write the Dead_Letter_Topic messages with the Spark Kafka sink including the headers column.
12. WHEN the File_Sink_Worker restarts with the same Sink_Checkpoint_Location, THE File_Sink_Worker SHALL NOT reprocess the offsets of Micro_Batches that completed before the restart.
