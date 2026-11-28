"""Delivery-stage Audit_Log writer for the File_Sink_Worker (Event_Path, Spark only).

Rows carry identifiers and decisions, never ``payload``. One JSON Lines file is written per
Micro_Batch under ``<audit_root>/delivery/``. The name comes from the batch's topic, partition and
offset ranges, computed by a Spark aggregation, so a replayed batch overwrites the same file.

Spark writes to a temp directory partitioned by that name column. The driver only lists directories
and calls ``os.replace`` (file metadata, no event data). Any failure raises ``AuditWriteError``;
it is never recorded as a decision.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from event_forwarding.audit_spark import AUDIT_FIELDS, build_audit_rows
from event_forwarding.log_util import remove_staging_dir

DELIVERY_AUDIT_DIR = "delivery"
DECISION_DELIVERED = "delivered"

_NAME_COL = "_audit_file"
_TMP_DIR = ".tmp"
_MAX_NAME_LEN = 120


class AuditWriteError(RuntimeError):
    """The Audit_Log could not be written. Callers must fail the Micro_Batch."""


def delivery_audit_path(audit_root: str | Path) -> str:
    return str(Path(audit_root) / DELIVERY_AUDIT_DIR)


def delivered_audit_rows(deliverable_df: DataFrame) -> DataFrame:
    """Audit rows with decision ``delivered`` and no reason, from written deliverable rows."""
    return build_audit_rows(deliverable_df, DECISION_DELIVERED, None)


def _name_df(offsets_df: DataFrame) -> DataFrame:
    """One-row DataFrame with the deterministic audit file name for the batch."""
    per_partition = offsets_df.groupBy("topic", "partition").agg(
        F.min("offset").alias("_min"), F.max("offset").alias("_max")
    )
    ranges = per_partition.select(
        F.concat(
            F.col("topic"),
            F.lit("-"),
            F.col("partition").cast("string"),
            F.lit("-"),
            F.col("_min").cast("string"),
            F.lit("-"),
            F.col("_max").cast("string"),
        ).alias("_range")
    )
    joined = ranges.agg(F.concat_ws("_", F.sort_array(F.collect_list("_range"))).alias("_joined"))
    name = F.when(
        F.length("_joined") <= _MAX_NAME_LEN, F.col("_joined")
    ).otherwise(
        F.concat(F.substring("_joined", 1, 60), F.lit("_"), F.sha1(F.col("_joined")))
    )
    return joined.where(F.col("_joined") != "").select(F.concat(F.lit("audit-"), name, F.lit(".jsonl")).alias(_NAME_COL))


def write_delivery_audit(audit_rows_df: DataFrame, offsets_df: DataFrame, audit_root: str | Path) -> None:
    """Write ``audit_rows_df`` as one JSONL file under ``<audit_root>/delivery/``.

    ``offsets_df`` is the batch's Kafka_Shaped_Rows (needs ``topic, partition, offset``); it only
    supplies the file name. Raises ``AuditWriteError`` on any failure.
    """
    final_dir = Path(delivery_audit_path(audit_root))
    # Short staging name: Spark's part and .crc files under it come close to the Windows path limit.
    tmp_dir = final_dir / _TMP_DIR / uuid.uuid4().hex[:8]
    try:
        final_dir.mkdir(parents=True, exist_ok=True)
        (
            audit_rows_df.select(*AUDIT_FIELDS)
            .crossJoin(F.broadcast(_name_df(offsets_df)))
            .repartition(1)
            .write.mode("overwrite")
            .option("ignoreNullFields", "false")
            .partitionBy(_NAME_COL)
            .json(str(tmp_dir))
        )
        for sub in tmp_dir.iterdir():
            if not (sub.is_dir() and sub.name.startswith(_NAME_COL + "=")):
                continue
            final_name = sub.name[len(_NAME_COL) + 1 :]
            parts = sorted(p for p in sub.iterdir() if p.name.startswith("part-") and not p.name.endswith(".crc"))
            if len(parts) != 1:
                raise AuditWriteError(f"expected one audit part file in {sub}, found {len(parts)}")
            os.replace(parts[0], final_dir / final_name)
    except AuditWriteError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
        raise AuditWriteError(f"audit write failed under {final_dir}: {exc}") from exc
    finally:
        remove_staging_dir(tmp_dir)
