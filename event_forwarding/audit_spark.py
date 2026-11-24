"""Processing-stage Audit_Log rows (Event_Path, Spark only).

Rows carry identifiers and decisions, never ``data`` or ``payload``. They are written as Spark
JSON Lines under ``<audit_root>/processing/``.
"""
from __future__ import annotations

from pathlib import Path

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

AUDIT_FIELDS = ("event_id", "tenant_id", "product", "schema", "decision", "reason", "destination_id")

PROCESSING_AUDIT_DIR = "processing"

_UUID_REGEX = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _as_column(value: Column | str | None) -> Column:
    if value is None:
        return F.lit(None).cast("string")
    return value if isinstance(value, Column) else F.lit(value)


def _optional(df: DataFrame, name: str) -> Column:
    return F.col(name).cast("string") if name in df.columns else F.lit(None).cast("string")


def build_audit_rows(
    df: DataFrame,
    decision: Column | str,
    reason: Column | str | None = None,
) -> DataFrame:
    """Project ``df`` onto the audit fields.

    ``decision`` and ``reason`` are literal strings or Columns. Missing optional input columns
    (``event_id``, ``product``, ``schema``, ``destination_id``) become null. ``tenant_id`` is
    lower-cased and set to null unless it is a canonical UUID. No other input column is kept.
    """
    tenant = _optional(df, "tenant_id")
    tenant_clean = F.when(F.lower(tenant).rlike(_UUID_REGEX), F.lower(tenant)).otherwise(
        F.lit(None).cast("string")
    )
    return df.select(
        _optional(df, "event_id").alias("event_id"),
        tenant_clean.alias("tenant_id"),
        _optional(df, "product").alias("product"),
        _optional(df, "schema").alias("schema"),
        _as_column(decision).cast("string").alias("decision"),
        _as_column(reason).cast("string").alias("reason"),
        _optional(df, "destination_id").alias("destination_id"),
    )


def processing_audit_path(audit_root: str | Path) -> str:
    return str(Path(audit_root) / PROCESSING_AUDIT_DIR)


def write_processing_audit(audit_df: DataFrame, audit_root: str | Path) -> None:
    """Append ``audit_df`` as JSON Lines under ``<audit_root>/processing/`` with Spark."""
    (
        audit_df.select(*AUDIT_FIELDS)
        .write.mode("append")
        .option("ignoreNullFields", "false")
        .json(processing_audit_path(audit_root))
    )
