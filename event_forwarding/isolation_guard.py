"""Isolation_Guard (isolation layer 1): last Spark check before the Kafka publish columns are built.

Pure DataFrame logic, no row iteration on the driver. A record is kept only if

* its ``destination_id`` is defined in ``destination_owner_df`` and the record's ``tenant_id`` equals
  that destination's owning ``tenant_id``, and
* its ``(tenant_id, product, schema)`` equals that of at least one enabled rule that references the
  destination.

Everything else goes to the violations DataFrame (processing audit shape, reason
``isolation_violation``, no payload) and the other records proceed.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, functions as F

ISOLATION_VIOLATION = "isolation_violation"

AUDIT_COLUMNS = ["event_id", "tenant_id", "product", "schema", "decision", "reason", "destination_id"]


def apply_isolation_guard(
    shaped_df: DataFrame, rules_df: DataFrame, destination_owner_df: DataFrame
) -> tuple[DataFrame, DataFrame]:
    """Split ``shaped_df`` into ``(guarded_df, violations_df)``.

    ``shaped_df`` needs ``event_id, tenant_id, product, schema, destination_id`` (other columns, such
    as ``payload``, are carried through to ``guarded_df`` unchanged and dropped from the violations).
    ``rules_df`` and ``destination_owner_df`` come from ``config_model.build_rules_df`` and
    ``build_destination_owner_df``.
    """
    owners = destination_owner_df.select(
        F.col("destination_id").alias("_owner_destination_id"),
        F.col("tenant_id").alias("_owner_tenant_id"),
    ).dropDuplicates()
    allowed = (
        rules_df.where(F.col("enabled"))
        .select(
            F.col("tenant_id").alias("_rule_tenant_id"),
            F.col("product").alias("_rule_product"),
            F.col("schema").alias("_rule_schema"),
            F.col("destination_id").alias("_rule_destination_id"),
        )
        .dropDuplicates()
        .withColumn("_rule_match", F.lit(True))
    )

    checked = shaped_df.join(
        F.broadcast(owners),
        F.col("destination_id") == F.col("_owner_destination_id"),
        "left",
    ).join(
        F.broadcast(allowed),
        (F.col("tenant_id") == F.col("_rule_tenant_id"))
        & (F.col("product") == F.col("_rule_product"))
        & (F.col("schema") == F.col("_rule_schema"))
        & (F.col("destination_id") == F.col("_rule_destination_id")),
        "left",
    )

    ok = (
        F.coalesce(F.col("_owner_tenant_id") == F.col("tenant_id"), F.lit(False))
        & F.coalesce(F.col("_rule_match"), F.lit(False))
    )
    helper_cols = [
        "_owner_destination_id",
        "_owner_tenant_id",
        "_rule_tenant_id",
        "_rule_product",
        "_rule_schema",
        "_rule_destination_id",
        "_rule_match",
    ]
    guarded = checked.where(ok).drop(*helper_cols)
    violations = checked.where(~ok).select(
        "event_id",
        "tenant_id",
        "product",
        "schema",
        F.lit("dropped").alias("decision"),
        F.lit(ISOLATION_VIOLATION).alias("reason"),
        "destination_id",
    )
    return guarded, violations
