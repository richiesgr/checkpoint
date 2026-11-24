"""Sink_Transform (Event_Path, pure DataFrame function, Spark only).

Input is a Kafka_Shaped_Rows DataFrame (key, value, topic, partition, offset, timestamp) and the
small ``destination_owner_df`` built from configuration. Output is three DataFrames:

* ``deliverable_df``: one row per valid, ownership-correct JSONL file Delivery_Message with columns
  ``destination_id, tenant_id, product, schema, event_id, payload, topic, partition, offset,
  file_name, key, value`` (``key``/``value`` are the original binary Kafka key and value).
  ``payload`` is the JSON text of the payload object. No output path is carried: the
  write step derives it from configuration only.
* ``dlq_df``: ``key, value, reason`` (original binary key and value, unchanged) with reason
  ``malformed_message`` or ``isolation_violation``.
* ``audit_df``: the Audit_Log fields (see ``audit_spark.AUDIT_FIELDS``) for dead-lettered and
  dropped rows. Delivered rows are audited by the write step once the write succeeded.
"""
from __future__ import annotations

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType

from event_forwarding.audit_spark import build_audit_rows

REASON_MALFORMED = "malformed_message"
REASON_ISOLATION = "isolation_violation"
REASON_FORMAT_NOT_BUILT = "format_not_built"

DECISION_DEAD_LETTERED = "dead-lettered"
DECISION_DROPPED = "dropped"

DLQ_FIELDS = ("key", "value", "reason")

# Delivery_Message fields other than ``payload`` (payload is an object, read as raw JSON text).
_MESSAGE_SCHEMA = StructType(
    [
        StructField("tenant_id", StringType()),
        StructField("destination_id", StringType()),
        StructField("product", StringType()),
        StructField("schema", StringType()),
        StructField("event_id", StringType()),
    ]
)
_REQUIRED = ("tenant_id", "destination_id", "product", "schema", "event_id")

_FILE_KIND = "file"
_JSONL_FORMAT = "jsonl"


def sink_transform(
    kafka_shaped_df: DataFrame, destination_owner_df: DataFrame
) -> tuple[DataFrame, DataFrame, DataFrame]:
    """Split Kafka_Shaped_Rows into (deliverable_df, dlq_df, audit_df)."""
    value_text = F.col("value").cast("string")
    key_text = F.col("key").cast("string")

    parsed = F.from_json(value_text, _MESSAGE_SCHEMA)
    base = kafka_shaped_df.select(
        F.col("key"),
        F.col("value"),
        key_text.alias("_key_text"),
        F.col("topic"),
        F.col("partition"),
        F.col("offset"),
        parsed.alias("_msg"),
        F.get_json_object(value_text, "$.payload").alias("_payload"),
    ).select(
        "key",
        "value",
        "_key_text",
        "topic",
        "partition",
        "offset",
        *[F.col("_msg")[name].alias(name) for name in _REQUIRED],
        F.col("_payload").alias("payload"),
        F.col("_msg").isNull().alias("_parse_null"),
    )

    owners = destination_owner_df.select(
        F.col("destination_id").alias("_o_destination_id"),
        F.col("tenant_id").alias("_o_tenant_id"),
        F.col("kind").alias("_o_kind"),
        F.col("format").alias("_o_format"),
    )
    joined = base.join(
        F.broadcast(owners), base["destination_id"] == owners["_o_destination_id"], "left"
    )

    malformed = F.col("_parse_null") | F.col("payload").isNull()
    for name in _REQUIRED:
        malformed = malformed | F.col(name).isNull()

    key_tenant = F.split(F.col("_key_text"), r"\|").getItem(0)
    isolation_ok = F.coalesce(
        F.col("_o_destination_id").isNotNull()
        & (F.col("tenant_id") == F.col("_o_tenant_id"))
        & (key_tenant == F.col("tenant_id"))
        & (F.col("_key_text") == F.concat(F.col("tenant_id"), F.lit("|"), F.col("destination_id"))),
        F.lit(False),
    )
    deliverable_format = (F.col("_o_kind") == F.lit(_FILE_KIND)) & (
        F.col("_o_format") == F.lit(_JSONL_FORMAT)
    )

    reason = (
        F.when(malformed, F.lit(REASON_MALFORMED))
        .when(~isolation_ok, F.lit(REASON_ISOLATION))
        .when(~F.coalesce(deliverable_format, F.lit(False)), F.lit(REASON_FORMAT_NOT_BUILT))
    )
    classified = joined.withColumn("_reason", reason)

    dlq_rows = classified.filter(F.col("_reason").isin(REASON_MALFORMED, REASON_ISOLATION))
    dlq_df = dlq_rows.select("key", "value", F.col("_reason").alias("reason"))
    dlq_audit = build_audit_rows(dlq_rows, F.lit(DECISION_DEAD_LETTERED), F.col("_reason"))

    dropped_rows = classified.filter(F.col("_reason") == F.lit(REASON_FORMAT_NOT_BUILT))
    dropped_audit = build_audit_rows(dropped_rows, F.lit(DECISION_DROPPED), F.col("_reason"))

    audit_df = dlq_audit.unionByName(dropped_audit)

    window = Window.partitionBy("destination_id", "topic", "partition")
    file_name = F.concat(
        F.col("destination_id"),
        F.lit("-"),
        F.col("topic"),
        F.lit("-"),
        F.col("partition").cast("string"),
        F.lit("-"),
        F.min("offset").over(window).cast("string"),
        F.lit("-"),
        F.max("offset").over(window).cast("string"),
        F.lit(".jsonl"),
    )
    deliverable_df = (
        classified.filter(F.col("_reason").isNull())
        .withColumn("file_name", file_name)
        .select(
            "destination_id",
            "tenant_id",
            "product",
            "schema",
            "event_id",
            "payload",
            "topic",
            "partition",
            "offset",
            "file_name",
            # Original Kafka key/value, carried so a failed write can dead-letter rows unchanged.
            "key",
            "value",
        )
    )
    return deliverable_df, dlq_df, audit_df
