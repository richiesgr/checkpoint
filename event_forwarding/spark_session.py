"""Shared SparkSession builder used by tests and both CLIs."""
from __future__ import annotations

import os
import sys

import pyspark
from pyspark.sql import SparkSession

SCALA_BINARY_VERSION = "2.13"


def kafka_package() -> str:
    """Spark Kafka connector coordinate matching the installed PySpark/Scala build."""
    return f"org.apache.spark:spark-sql-kafka-0-10_{SCALA_BINARY_VERSION}:{pyspark.__version__}"


def pin_python_env() -> None:
    """Make Spark driver and workers use the interpreter running this process (the venv)."""
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable


def build_spark(
    app_name: str = "event-forwarding",
    master: str = "local[*]",
    warehouse_dir: str | None = None,
    shuffle_partitions: int = 4,
    with_kafka: bool = True,
    default_parallelism: int | None = None,
) -> SparkSession:
    pin_python_env()
    builder = (
        SparkSession.builder.master(master)
        .appName(app_name)
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", str(shuffle_partitions))
        .config("spark.sql.session.timeZone", "UTC")
    )
    if warehouse_dir:
        builder = builder.config("spark.sql.warehouse.dir", warehouse_dir)
    if default_parallelism is not None:
        builder = builder.config("spark.default.parallelism", str(default_parallelism))
    if with_kafka:
        builder = builder.config("spark.jars.packages", kafka_package())
    return builder.getOrCreate()
