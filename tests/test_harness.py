"""Smoke tests for the test harness (task 1.1)."""
import sys

import pyspark

from event_forwarding.spark_session import kafka_package
from tests.conftest import kafka_shaped_schema


def test_kafka_package_matches_pyspark_version():
    assert kafka_package() == f"org.apache.spark:spark-sql-kafka-0-10_2.13:{pyspark.__version__}"


def test_spark_session_runs_locally(spark):
    assert spark.range(3).count() == 3
    assert spark.sparkContext.master.startswith("local")
    assert spark.conf.get("spark.ui.enabled") == "false"


def test_make_kafka_rows_has_kafka_shape(make_kafka_rows):
    df = make_kafka_rows(
        [("tenantA|dest-a-file", '{"x":1}', "delivery.file", 0, 5)]
    )
    assert df.schema == kafka_shaped_schema
    row = df.select("key", "value", "topic", "partition", "offset").head()
    assert bytes(row["key"]) == b"tenantA|dest-a-file"
    assert (row["topic"], row["partition"], row["offset"]) == ("delivery.file", 0, 5)


def test_python_is_venv_interpreter():
    assert ".venv" in sys.executable.replace("\\", "/")


def test_kafka_broker_fixture_skips_without_broker(request):
    # Exercised via the fixture in kafka-marked tests; here just confirm the marker is registered.
    assert any(m.startswith("kafka:") for m in request.config.getini("markers"))
