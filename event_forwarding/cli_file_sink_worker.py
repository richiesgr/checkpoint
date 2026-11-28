"""Command line entry point of the File_Sink_Worker (Spark Structured Streaming, availableNow).

Reads ``delivery.file`` with its own Sink_Checkpoint_Location, runs ``process_micro_batch`` in
``foreachBatch`` and writes the Worker_Summary after the drain. Offsets are tracked only by the
Spark checkpoint (no manual commits, no ``group.id``).

Exit codes: 0 success, 1 isolation violation or failed DLQ publish/audit write during the run,
2 startup failure (config, checkpoint clash or broker). Startup failures happen before any
message is read.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_forwarding.config_model import ConfigError, load_config
from event_forwarding.kafka_util import BrokerUnreachableError, require_broker
from event_forwarding.log_util import configure_logging, get_logger, read_total_counters

log = get_logger(__name__)

EXIT_OK = 0
EXIT_RUN_ERROR = 1
EXIT_STARTUP_ERROR = 2

SOURCE_TOPIC = "delivery.file"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli_file_sink_worker",
        description="Read delivery.file from Kafka and write per-destination JSONL files.",
    )
    parser.add_argument("--config", required=False, default=None, help="path of the JSON or YAML config")
    parser.add_argument("--checkpoint", required=True, help="Sink_Checkpoint_Location of the worker query")
    parser.add_argument(
        "--processing-checkpoint",
        required=False,
        default=None,
        help="Checkpoint_Location of the processing job; the sink checkpoint must differ from it",
    )
    return parser


def _fail(message: str, code: int = EXIT_STARTUP_ERROR) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _run_audit(spark, audit_root: str, since: datetime):
    """Delivery audit rows written by this run (files modified since ``since``), read with Spark."""
    from pyspark.sql import functions as F
    from pyspark.sql.types import StringType, StructField, StructType

    from event_forwarding.audit_spark import AUDIT_FIELDS
    from event_forwarding.audit_worker import delivery_audit_path

    schema = StructType([StructField(name, StringType()) for name in AUDIT_FIELDS])
    audit_dir = Path(delivery_audit_path(audit_root))
    audit_dir.mkdir(parents=True, exist_ok=True)
    return (
        spark.read.schema(schema)
        .option("pathGlobFilter", "audit-*.jsonl")
        .json(str(audit_dir))
        .where(F.col("_metadata.file_modification_time") >= F.lit(since))  # since is timezone-aware: exact instant
    )


def run_worker(spark, config, checkpoint: str | Path, starting_offsets: str = "earliest"):
    """Drain ``delivery.file`` once and write the Worker_Summary. Returns the isolation violation count.

    ``starting_offsets`` is the Spark Kafka source option: ``earliest`` (default) or a JSON map of
    per-partition offsets. Spark uses it only when the checkpoint is new; afterwards the
    checkpoint decides. Tests pass explicit offsets to read only their own messages.
    """
    from pyspark.sql import functions as F

    from event_forwarding.file_sink_write import process_micro_batch
    from event_forwarding.sink_transform import REASON_ISOLATION
    from event_forwarding.summary_worker import build_worker_summary, worker_summary_path, write_worker_summary

    bootstrap = config.kafka.bootstrap_servers
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    run_started = time.monotonic()
    log.info(
        "sink worker start: topic=%s starting_offsets=%s bootstrap=%s checkpoint=%s destinations=%d output_root=%s",
        SOURCE_TOPIC,
        starting_offsets,
        bootstrap,
        Path(checkpoint).resolve(),
        len(config.destinations),
        Path(config.output_root).resolve(),
    )

    def handle(batch_df, batch_id: int) -> None:
        process_micro_batch(batch_df, batch_id, config)

    stream = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", SOURCE_TOPIC)
        .option("startingOffsets", starting_offsets)
        .option("failOnDataLoss", "false")
        .load()
    )
    query = (
        stream.writeStream.foreachBatch(handle)
        .option("checkpointLocation", str(Path(checkpoint).resolve()))
        .trigger(availableNow=True)
        .start()
    )
    query.awaitTermination()

    audit = _run_audit(spark, config.audit_root, started).persist()
    try:
        write_worker_summary(build_worker_summary(audit), config.audit_root)
        violations = audit.where(F.col("reason") == F.lit(REASON_ISOLATION)).count()
        summary_dir = worker_summary_path(config.audit_root)
        log.info(
            "sink worker done in %.1fs: totals=%s isolation_violations=%d summary=%s",
            time.monotonic() - run_started,
            read_total_counters(summary_dir),
            violations,
            Path(summary_dir).resolve(),
        )
        if violations:
            log.error("sink worker: %d isolation violation(s), see the delivery audit and %s", violations, "delivery.file.dlq")
        return violations
    finally:
        audit.unpersist()


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse already wrote its usage error to stderr
        return int(exc.code) if isinstance(exc.code, int) and exc.code else EXIT_STARTUP_ERROR

    configure_logging()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        return _fail(str(exc))
    log.info("config loaded from %s", args.config)

    if args.processing_checkpoint and Path(args.processing_checkpoint).resolve() == Path(args.checkpoint).resolve():
        return _fail("the sink checkpoint must differ from the processing checkpoint")

    try:
        require_broker(config.kafka.bootstrap_servers)
    except (BrokerUnreachableError, ValueError) as exc:
        return _fail(str(exc))

    from event_forwarding.spark_session import build_spark

    spark = build_spark(app_name="event-forwarding-file-sink-worker")
    try:
        violations = run_worker(spark, config, args.checkpoint)
    except Exception as exc:  # noqa: BLE001 - failed DLQ publish, audit write or query error
        return _fail(f"file sink worker failed, checkpoint not advanced for the failing batch: {exc}", EXIT_RUN_ERROR)
    finally:
        spark.stop()

    if violations:
        print(f"error: {violations} isolation violation(s), see the delivery audit", file=sys.stderr)
        return EXIT_RUN_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
