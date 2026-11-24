"""Unit tests for kafka_publish and audit_spark (task 4.5)."""
import json
import os
import sys

import pytest
from pyspark.sql import Row

from event_forwarding.audit_spark import AUDIT_FIELDS, build_audit_rows, write_processing_audit
from event_forwarding.kafka_publish import add_publish_columns

TENANT = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"


def test_publish_columns_topic_key_value(spark):
    df = spark.createDataFrame(
        [
            Row(destination_kind="file", tenant_id=TENANT.upper(), destination_id="dest-a-file",
                product="sales", schema="purchase", event_id="e1",
                payload=Row(order_id="o1", amount=10, region="eu")),
            Row(destination_kind="http", tenant_id=TENANT, destination_id="dest-h",
                product="sales", schema="purchase", event_id="e2",
                payload=Row(order_id="o2", amount=5, region="eu")),
        ]
    )
    rows = {r["key"]: r for r in add_publish_columns(df).collect()}
    assert set(add_publish_columns(df).columns) == {"topic", "key", "value"}
    r1 = rows[f"{TENANT}|dest-a-file"]
    assert r1["topic"] == "delivery.file"
    assert json.loads(r1["value"]) == {
        "tenant_id": TENANT, "destination_id": "dest-a-file", "product": "sales",
        "schema": "purchase", "event_id": "e1",
        "payload": {"order_id": "o1", "amount": 10, "region": "eu"},
    }
    assert rows[f"{TENANT}|dest-h"]["topic"] == "delivery.http"


def test_audit_rows_have_no_payload_and_null_invalid_tenant(spark):
    df = spark.createDataFrame(
        [
            Row(event_id="e1", tenant_id=TENANT, product="p", schema="s", destination_id="d",
                payload=Row(secret="x"), data="{}"),
            Row(event_id="e2", tenant_id="not-a-uuid", product="p", schema="s", destination_id=None,
                payload=Row(secret="y"), data="{}"),
        ]
    )
    out = build_audit_rows(df, "dropped", "invalid_tenant_id")
    assert out.columns == list(AUDIT_FIELDS)
    by_id = {r["event_id"]: r for r in out.collect()}
    assert by_id["e1"]["tenant_id"] == TENANT
    assert by_id["e2"]["tenant_id"] is None
    assert by_id["e2"]["decision"] == "dropped" and by_id["e2"]["reason"] == "invalid_tenant_id"


def test_audit_handles_missing_columns_and_writes_jsonl(spark, tmp_path):
    if sys.platform == "win32" and not (os.environ.get("HADOOP_HOME") or os.environ.get("hadoop.home.dir")):
        pytest.skip("Spark file writes on Windows need HADOOP_HOME with winutils.exe (not set)")
    df = spark.createDataFrame([Row(event_id="e9", tenant_id=TENANT.upper(), data="{}")])
    audit = build_audit_rows(df, "dropped", "no_matching_rule")
    write_processing_audit(audit, tmp_path)
    files = list((tmp_path / "processing").glob("part-*.json"))
    assert files
    lines = [json.loads(line) for f in files for line in f.read_text().splitlines() if line]
    assert lines == [{
        "event_id": "e9", "tenant_id": TENANT, "product": None, "schema": None,
        "decision": "dropped", "reason": "no_matching_rule", "destination_id": None,
    }]
    assert "data" not in lines[0] and "payload" not in lines[0]
