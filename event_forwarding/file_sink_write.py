"""File_Sink_Worker write step: the ``foreachBatch`` body (Event_Path, Spark only).

Event data is only touched by Spark. The single driver loop runs over configured Destinations and
the driver only lists and renames files (metadata). The output path comes from configuration
``(owner tenant, destination_id)``, never from a message column.

Delivery is at-least-once. Files get deterministic names, so a replayed batch overwrites the same
final file. If a rename fails part-way, files already renamed stay in place and the whole
Destination is dead-lettered, so those events may also be delivered on replay or by the customer's
copy of the DLQ; customers deduplicate on ``event_id``.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from functools import reduce
from pathlib import Path
from typing import Callable

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from event_forwarding.audit_worker import delivered_audit_rows, write_delivery_audit
from event_forwarding.audit_spark import build_audit_rows
from event_forwarding.config_model import Config, build_destination_owner_df, destination_output_dir
from event_forwarding.log_util import get_logger, remove_staging_dir
from event_forwarding.sink_transform import DECISION_DEAD_LETTERED, sink_transform

log = get_logger(__name__)

DLQ_TOPIC = "delivery.file.dlq"
REASON_WRITE_FAILED = "write_failed"

_TMP_DIR = ".tmp"
_NAME_COL = "file_name"


class DlqPublishError(RuntimeError):
    """The DLQ publish failed. The Micro_Batch must fail so the checkpoint does not advance."""


def kafka_dlq_publisher(bootstrap_servers: str) -> Callable[[DataFrame], None]:
    """Default ``publish_dlq``: one Spark Kafka sink write of (key, value, reason) with headers."""

    def publish(dlq_df: DataFrame) -> None:
        out = dlq_df.select(
            F.col("key").cast("binary").alias("key"),
            F.col("value").cast("binary").alias("value"),
            F.array(
                F.struct(
                    F.lit("reason").alias("key"),
                    F.col("reason").cast("string").cast("binary").alias("value"),
                )
            ).alias("headers"),
        ).withColumn("topic", F.lit(DLQ_TOPIC))
        (
            out.write.format("kafka")
            .option("kafka.bootstrap.servers", bootstrap_servers)
            .option("includeHeaders", "true")
            .save()
        )

    return publish


def _jsonl_line(df: DataFrame) -> DataFrame:
    """One text column ``value``: envelope fields followed by ``payload`` (raw JSON object text)."""
    envelope = F.to_json(F.struct("event_id", "product", "schema", "tenant_id"))
    line = F.concat(
        F.substring(envelope, 1, F.length(envelope) - 1),
        F.lit(',"payload":'),
        F.col("payload"),
        F.lit("}"),
    )
    return df.select(line.alias("value"), F.col(_NAME_COL))


def _write_destination(dest_df: DataFrame, final_dir: Path, tmp_root: Path) -> None:
    """Write the Destination's rows as JSONL files into ``tmp_root`` then rename to final names.

    Spark writes one part file per ``file_name``; the driver lists and ``os.replace``s them.
    """
    (
        _jsonl_line(dest_df)
        .repartition(F.col(_NAME_COL))
        .write.mode("overwrite")
        .partitionBy(_NAME_COL)
        .option("encoding", "UTF-8")
        .option("lineSep", "\n")
        .text(str(tmp_root))
    )
    final_dir.mkdir(parents=True, exist_ok=True)
    prefix = _NAME_COL + "="
    for sub in sorted(tmp_root.iterdir()):
        if not (sub.is_dir() and sub.name.startswith(prefix)):
            continue
        parts = [p for p in sub.iterdir() if p.name.startswith("part-") and not p.name.endswith(".crc")]
        if len(parts) != 1:
            raise OSError(f"expected one part file in {sub}, found {len(parts)}")
        os.replace(parts[0], final_dir / sub.name[len(prefix) :])


def process_micro_batch(
    batch_df: DataFrame,
    batch_id: int,
    config: Config,
    publish_dlq: Callable[[DataFrame], None] | None = None,
) -> None:
    """Process one Micro_Batch of Kafka_Shaped_Rows. Raises if the batch must be retried."""
    if publish_dlq is None:
        publish_dlq = kafka_dlq_publisher(config.kafka.bootstrap_servers)

    started = time.monotonic()
    log.info("sink batch %d: start", batch_id)
    written: list[str] = []
    failed: list[str] = []
    spark = batch_df.sparkSession
    owners = build_destination_owner_df(spark, config)
    deliverable_df, sink_dlq_df, sink_audit_df = sink_transform(batch_df, owners)
    deliverable_df = deliverable_df.persist()
    try:
        dlq_parts: list[DataFrame] = [sink_dlq_df]
        audit_parts: list[DataFrame] = [sink_audit_df]
        delivered_parts: list[DataFrame] = []

        for dest in config.destinations:  # the only driver loop: configured Destinations
            if dest.kind != "file" or dest.format != "jsonl":
                continue
            dest_df = deliverable_df.filter(F.col("destination_id") == F.lit(dest.id))
            final_dir = destination_output_dir(config, dest)
            # Short staging name: Spark's part and .crc files under it come close to the Windows path limit.
            tmp_root = final_dir / _TMP_DIR / f"{batch_id}-{uuid.uuid4().hex[:8]}"
            try:
                _write_destination(dest_df, final_dir, tmp_root)
            except Exception as write_exc:  # noqa: BLE001 - dead-letter this Destination, keep going
                log.error(
                    "sink batch %d: write failed for destination %s, its rows go to %s: %s",
                    batch_id, dest.id, DLQ_TOPIC, write_exc,
                )
                failed.append(dest.id)
                try:
                    shutil.rmtree(tmp_root, ignore_errors=True)
                except OSError as exc:  # pragma: no cover - ignore_errors should prevent this
                    raise RuntimeError(f"cleanup after write failure failed for {dest.id}") from exc
                dlq_parts.append(
                    dest_df.select("key", "value", F.lit(REASON_WRITE_FAILED).alias("reason"))
                )
                audit_parts.append(
                    build_audit_rows(dest_df, DECISION_DEAD_LETTERED, REASON_WRITE_FAILED)
                )
                continue
            remove_staging_dir(tmp_root)
            delivered_parts.append(dest_df)
            written.append(dest.id)

        if delivered_parts:
            delivered_df = reduce(DataFrame.unionByName, delivered_parts)
            audit_parts.append(delivered_audit_rows(delivered_df))

        dlq_df = reduce(DataFrame.unionByName, dlq_parts)
        try:
            publish_dlq(dlq_df)
        except Exception as exc:
            log.error("sink batch %d: DLQ publish failed, the batch will be retried: %s", batch_id, exc)
            raise DlqPublishError(f"DLQ publish failed for batch {batch_id}: {exc}") from exc

        audit_df = reduce(DataFrame.unionByName, audit_parts)
        write_delivery_audit(audit_df, batch_df, config.audit_root)
        log.info(
            "sink batch %d: done in %.1fs, destinations written=%d failed=%s",
            batch_id, time.monotonic() - started, len(written), failed or "none",
        )
    finally:
        deliverable_df.unpersist()
