"""Shared pytest fixtures: local Spark session, Kafka broker probe, Kafka_Shaped_Rows builder."""
from __future__ import annotations

import logging
import os
import socket
import sys
from datetime import datetime, timezone

import pytest
from pyspark.sql.types import (
    BinaryType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from event_forwarding.spark_session import build_spark

# Same shape that spark.readStream.format("kafka").load() produces (design: Kafka_Shaped_Rows).
kafka_shaped_schema = StructType(
    [
        StructField("key", BinaryType()),
        StructField("value", BinaryType()),
        StructField("topic", StringType()),
        StructField("partition", IntegerType()),
        StructField("offset", LongType()),
        StructField("timestamp", TimestampType()),
    ]
)

KAFKA_HOST = os.environ.get("KAFKA_TEST_HOST", "localhost")
KAFKA_PORT = int(os.environ.get("KAFKA_TEST_PORT", "9092"))

# The py4j bridge and the test-side kafka-python client log every call or connection at INFO,
# which would flood the live log.
logging.getLogger("py4j").setLevel(logging.WARNING)
logging.getLogger("kafka").setLevel(logging.WARNING)

# Make sure spawned Python workers use the venv interpreter.
os.environ["PYSPARK_PYTHON"] = sys.executable
os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    warehouse = tmp_path_factory.mktemp("spark-warehouse")
    session = build_spark(
        app_name="event-forwarding-tests",
        # Two cores and one default partition: tiny test DataFrames would otherwise get 16
        # partitions, and on Windows each task over Python-created data starts a new Python worker.
        master="local[2]",
        default_parallelism=1,
        warehouse_dir=str(warehouse),
        shuffle_partitions=2,
        # The connector is only resolved (network) for kafka-marked tests.
        with_kafka=False,
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


@pytest.fixture(scope="session")
def kafka_broker():
    """Return 'host:port' of a reachable broker, or skip naming the missing broker."""
    address = f"{KAFKA_HOST}:{KAFKA_PORT}"
    try:
        with socket.create_connection((KAFKA_HOST, KAFKA_PORT), timeout=1.0):
            pass
    except OSError:
        pytest.skip(
            f"Kafka broker not reachable at {address}; start it with `docker compose up -d`"
        )
    return address


@pytest.fixture(scope="session")
def kafka_spark(tmp_path_factory, kafka_broker):
    """Separate-from-`spark` session is not possible in one JVM, so Kafka tests run in their own session."""
    from pyspark.sql import SparkSession

    # spark.jars.packages is applied only when the JVM starts. Do not start one here without the
    # connector (getOrCreate() would). Stop an existing session only. A JVM already started by the
    # `spark` fixture in the same process has no connector, so run kafka tests on their own.
    active = SparkSession.getActiveSession()
    if active is not None:
        active.stop()
    warehouse = tmp_path_factory.mktemp("spark-warehouse-kafka")
    session = build_spark(
        app_name="event-forwarding-kafka-tests",
        warehouse_dir=str(warehouse),
        shuffle_partitions=2,
        with_kafka=True,
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def _to_bytes(value):
    if value is None or isinstance(value, (bytes, bytearray)):
        return None if value is None else bytes(value)
    return str(value).encode("utf-8")


@pytest.fixture
def make_kafka_rows(spark):
    """Build a Kafka_Shaped_Rows DataFrame from literal rows.

    Each row is a dict or tuple with key, value, topic, partition, offset and optionally timestamp.
    key/value may be str (encoded as UTF-8) or bytes. Missing timestamp defaults to a fixed instant.
    """
    default_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def _build(rows):
        normalized = []
        for r in rows:
            if isinstance(r, dict):
                key, value, topic = r.get("key"), r.get("value"), r["topic"]
                partition, offset = r.get("partition", 0), r["offset"]
                ts = r.get("timestamp", default_ts)
            else:
                key, value, topic, partition, offset, *rest = r
                ts = rest[0] if rest else default_ts
            normalized.append(
                (_to_bytes(key), _to_bytes(value), topic, int(partition), int(offset), ts)
            )
        return spark.createDataFrame(normalized, schema=kafka_shaped_schema)

    return _build
