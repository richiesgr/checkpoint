"""Kafka_Publish_Stage column logic (Event_Path, pure Spark Column expressions).

Turns guarded rows into the ``topic`` / ``key`` / ``value`` columns that the Spark Kafka sink
publishes. Topics are shared by destination kind; tenant, product and destination go into the
key and value, never into the topic name.
"""
from __future__ import annotations

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

TOPIC_BY_KIND = {
    "file": "delivery.file",
    "http": "delivery.http",
    "syslog": "delivery.syslog",
}

# Fields of the Delivery_Message value, in order.
VALUE_FIELDS = ("tenant_id", "destination_id", "product", "schema", "event_id", "payload")


def topic_for_kind(kind_col: Column | str) -> Column:
    """Topic name for a destination kind column; null for an unknown kind."""
    kind = F.col(kind_col) if isinstance(kind_col, str) else kind_col
    expr = None
    for known, topic in TOPIC_BY_KIND.items():
        branch = F.when(kind == F.lit(known), F.lit(topic))
        expr = branch if expr is None else expr.when(kind == F.lit(known), F.lit(topic))
    return expr


def add_publish_columns(df: DataFrame) -> DataFrame:
    """Return ``df`` reduced to ``topic``, ``key`` and ``value`` (all strings).

    Required input columns: ``destination_kind``, ``tenant_id``, ``destination_id``, ``product``,
    ``schema``, ``event_id`` and ``payload`` (a struct or map column, serialized as a nested JSON
    object). ``tenant_id`` is lower-cased so the key always carries the canonical form.
    ``key`` is ``<tenant_id>|<destination_id>``.
    """
    tenant = F.lower(F.col("tenant_id"))
    prepared = df.withColumn("tenant_id", tenant)
    return prepared.select(
        topic_for_kind("destination_kind").alias("topic"),
        F.concat(F.col("tenant_id"), F.lit("|"), F.col("destination_id")).alias("key"),
        F.to_json(F.struct(*[F.col(name) for name in VALUE_FIELDS])).alias("value"),
    )
