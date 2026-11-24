"""Run_Summary of the processing stage (Event_Path, Spark aggregations only).

The summary is a DataFrame of non-negative integer counters (``read``, ``rejected``, ``dropped``,
``published``). One row has ``scope = "total"``. One row per (tenant_id, destination_id) pair seen
has ``scope = "tenant_destination"``. Counters of stages that do not know the Destination (read,
rejected, dropped for lack of a rule) are attributed to a row with a null ``destination_id``.
Only aggregated rows are written, with Spark, as JSON Lines. No event row is collected.
"""
from __future__ import annotations

from pathlib import Path

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

COUNTERS = ("read", "rejected", "dropped", "published")
SCOPE_TOTAL = "total"
SCOPE_TENANT_DESTINATION = "tenant_destination"
SUMMARY_COLUMNS = ("scope", "tenant_id", "destination_id") + COUNTERS

RUN_SUMMARY_DIR = "run_summary"


def _optional(df: DataFrame, name: str) -> Column:
    return F.col(name).cast("string") if name in df.columns else F.lit(None).cast("string")


def _keyed(df: DataFrame, counter: str) -> DataFrame:
    """Reduce ``df`` to (tenant_id, destination_id, counter) with one row per input row.

    ``tenant_id`` and ``destination_id`` come from same-named columns when present. For publish-ready
    rows they are taken from the Kafka ``key`` (``<tenant_id>|<destination_id>``).
    """
    tenant = _optional(df, "tenant_id")
    destination = _optional(df, "destination_id")
    if "key" in df.columns and "tenant_id" not in df.columns:
        key = F.col("key").cast("string")
        tenant = F.lower(F.split(key, r"\|", 2).getItem(0))
        destination = F.split(key, r"\|", 2).getItem(1)
    elif "tenant_id" in df.columns:
        tenant = F.lower(tenant)
    return df.select(
        tenant.alias("tenant_id"), destination.alias("destination_id"), F.lit(counter).alias("_counter")
    )


def build_run_summary(
    read_df: DataFrame,
    rejected_df: DataFrame,
    dropped_df: DataFrame,
    published_df: DataFrame,
) -> DataFrame:
    """Return the Run_Summary DataFrame (columns ``SUMMARY_COLUMNS``).

    ``read_df``: every non-blank line read (valid and rejected). ``rejected_df``: rejected lines.
    ``dropped_df``: processing audit rows with decision ``dropped``. ``published_df``: publish-ready
    rows (``topic, key, value``).
    """
    events = (
        _keyed(read_df, "read")
        .unionByName(_keyed(rejected_df, "rejected"))
        .unionByName(_keyed(dropped_df, "dropped"))
        .unionByName(_keyed(published_df, "published"))
    )

    def counter_sum(name: str) -> Column:
        return F.coalesce(F.sum(F.when(F.col("_counter") == F.lit(name), F.lit(1)).otherwise(F.lit(0))), F.lit(0))

    aggs = [counter_sum(name).cast("long").alias(name) for name in COUNTERS]
    per_key = events.groupBy("tenant_id", "destination_id").agg(*aggs)
    per_key = per_key.select(
        F.lit(SCOPE_TENANT_DESTINATION).alias("scope"), "tenant_id", "destination_id", *COUNTERS
    )
    total = events.agg(*aggs).select(
        F.lit(SCOPE_TOTAL).alias("scope"),
        F.lit(None).cast("string").alias("tenant_id"),
        F.lit(None).cast("string").alias("destination_id"),
        *COUNTERS,
    )
    return total.unionByName(per_key).select(*SUMMARY_COLUMNS)


def run_summary_path(output_root: str | Path) -> str:
    return str(Path(output_root) / RUN_SUMMARY_DIR)


def write_run_summary(summary_df: DataFrame, output_root: str | Path) -> None:
    """Write the aggregated summary as one JSON Lines part file under ``<output_root>/run_summary``."""
    (
        summary_df.select(*SUMMARY_COLUMNS)
        .coalesce(1)
        .write.mode("overwrite")
        .option("ignoreNullFields", "false")
        .json(run_summary_path(output_root))
    )
