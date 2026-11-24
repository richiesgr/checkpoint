"""Worker_Summary of the File_Sink_Worker (Event_Path, Spark aggregations only).

Input is the delivery Audit_Log DataFrame (``tenant_id``, ``destination_id``, ``decision``).
Rows with decision ``delivered`` and ``dead-lettered`` are counted. Other decisions (for example
``dropped``) are ignored. Output is non-negative integer counters, in total and per
(tenant_id, destination_id), written with Spark as JSON Lines. No event row is collected.
"""
from __future__ import annotations

from pathlib import Path

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

DECISION_DELIVERED = "delivered"
DECISION_DEAD_LETTERED = "dead-lettered"

SCOPE_TOTAL = "total"
SCOPE_TENANT_DESTINATION = "tenant_destination"
SUMMARY_COLUMNS = ("scope", "tenant_id", "destination_id", "delivered", "dead_lettered")

WORKER_SUMMARY_DIR = "worker_summary"


def _optional(df: DataFrame, name: str) -> Column:
    return F.col(name).cast("string") if name in df.columns else F.lit(None).cast("string")


def build_worker_summary(audit_df: DataFrame) -> DataFrame:
    """Return the Worker_Summary DataFrame (columns ``SUMMARY_COLUMNS``)."""
    rows = audit_df.select(
        F.lower(_optional(audit_df, "tenant_id")).alias("tenant_id"),
        _optional(audit_df, "destination_id").alias("destination_id"),
        _optional(audit_df, "decision").alias("decision"),
    )

    def count_of(decision: str) -> Column:
        hit = F.when(F.col("decision") == F.lit(decision), F.lit(1)).otherwise(F.lit(0))
        return F.coalesce(F.sum(hit), F.lit(0)).cast("long")

    aggs = [count_of(DECISION_DELIVERED).alias("delivered"), count_of(DECISION_DEAD_LETTERED).alias("dead_lettered")]
    per_key = (
        rows.where(F.col("decision").isin(DECISION_DELIVERED, DECISION_DEAD_LETTERED))
        .groupBy("tenant_id", "destination_id")
        .agg(*aggs)
        .select(F.lit(SCOPE_TENANT_DESTINATION).alias("scope"), "tenant_id", "destination_id", "delivered", "dead_lettered")
    )
    total = rows.agg(*aggs).select(
        F.lit(SCOPE_TOTAL).alias("scope"),
        F.lit(None).cast("string").alias("tenant_id"),
        F.lit(None).cast("string").alias("destination_id"),
        "delivered",
        "dead_lettered",
    )
    return total.unionByName(per_key).select(*SUMMARY_COLUMNS)


def worker_summary_path(output_root: str | Path) -> str:
    return str(Path(output_root) / WORKER_SUMMARY_DIR)


def write_worker_summary(summary_df: DataFrame, output_root: str | Path) -> None:
    """Write the aggregated summary as one JSON Lines part file under ``<output_root>/worker_summary``."""
    (
        summary_df.select(*SUMMARY_COLUMNS)
        .coalesce(1)
        .write.mode("overwrite")
        .option("ignoreNullFields", "false")
        .json(worker_summary_path(output_root))
    )
