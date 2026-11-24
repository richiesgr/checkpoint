"""Command line entry point of the processing job (Spark Structured Streaming, availableNow).

Exit codes: 0 success, 1 isolation violation or unreadable file during the run, 2 startup
failure (config, landing zone or broker). Startup failures happen before any Event is read.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from event_forwarding.config_model import ConfigError, load_config
from event_forwarding.kafka_util import BrokerUnreachableError, require_broker

EXIT_OK = 0
EXIT_RUN_ERROR = 1
EXIT_STARTUP_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli_spark", description="Read landing-zone events and publish Delivery_Messages to Kafka."
    )
    parser.add_argument("--config", required=False, default=None, help="path of the JSON or YAML config")
    parser.add_argument("--landing-zone", required=True, help="directory holding *.jsonl files")
    parser.add_argument("--checkpoint", required=True, help="Checkpoint_Location of the processing query")
    return parser


def _fail(message: str, code: int = EXIT_STARTUP_ERROR) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse already wrote its usage error to stderr
        return int(exc.code) if isinstance(exc.code, int) and exc.code else EXIT_STARTUP_ERROR

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        return _fail(str(exc))

    if not os.path.isdir(args.landing_zone):
        return _fail(f"landing zone is not an existing directory: {args.landing_zone}")

    try:
        require_broker(config.kafka.bootstrap_servers)
    except (BrokerUnreachableError, ValueError) as exc:
        return _fail(str(exc))

    from event_forwarding.pipeline import run_processing
    from event_forwarding.spark_session import build_spark

    spark = build_spark(app_name="event-forwarding-processing")
    try:
        result = run_processing(spark, config, Path(args.landing_zone), Path(args.checkpoint))
    finally:
        spark.stop()

    if result.isolation_violations:
        print(f"error: {result.isolation_violations} isolation violation(s), see the processing audit", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
