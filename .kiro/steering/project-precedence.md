---
inclusion: always
---
# Project rules take precedence over global Spark skills

The global Spark skills (streaming, partitions, SQL/DataFrame) in the user's steering folder are general guidance. For this project, the rules below and the spec in `.kiro/specs/event-forwarding/` override them wherever they conflict.

- PySpark is required on the whole event path (processing job and File_Sink_Worker).
- Banned on the event path, even where a skill shows them as acceptable: pandas, pyarrow as a writer, `collect()`, `toPandas()`, `take()`, `head()`, Python UDFs, `applyInPandasWithState`, and driver-side row iteration over event data. Only the config module is exempt.
- Delivery is at-least-once (the Spark Kafka sink can republish). Do not claim exactly-once. File output is idempotent through deterministic names, and customers deduplicate on `event_id`.
- Use `trigger(availableNow=True)`, never `once=True`.
- Kafka topics are shared by destination kind (`delivery.file`, `delivery.file.dlq`, ...). Tenant, product and destination go in the message key and value, never in topic names.
- Do not add a streaming `dropDuplicates` or other stateful operation without a watermark.