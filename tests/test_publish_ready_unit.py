"""Tests for shape_events constant override and build_publish_ready (task 4.6). Validates: 4.4, 11.1, 15.3, 3.4."""
from __future__ import annotations

import json
from pathlib import Path

from event_forwarding.config_model import build_rules_df, load_config
from event_forwarding.pipeline import build_publish_ready, parse_events, route_events, shape_events

A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"


def _config(tmp_path: Path):
    raw = f"""
kafka: {{bootstrap_servers: "localhost:9092"}}
output_root: "{(tmp_path / 'out').as_posix()}"
destinations:
  - {{id: da, tenant_id: "{A}", kind: file, format: jsonl}}
rules:
  - tenant_id: "{A}"
    product: sales
    schema: purchase
    constant_fields: {{risk: "const", region: "eu"}}
    destinations: [da]
"""
    p = tmp_path / "c.yaml"
    p.write_text(raw, encoding="utf-8")
    return load_config(p)


def test_constant_overrides_data_field_without_duplicate_keys(spark, tmp_path):
    cfg = _config(tmp_path)
    ev = {"event_id": "e1", "product": "sales", "schema": "purchase", "tenant_id": A,
          "data": {"risk": 5, "amount": 3}}
    df = spark.createDataFrame([(json.dumps(ev),)], ["value"])
    valid, rejected = parse_events(df)
    routed, _ = route_events(valid, build_rules_df(spark, cfg), rejected)
    row = shape_events(routed, cfg).select(__import__("pyspark.sql.functions", fromlist=["x"]).to_json("payload").alias("p")).collect()[0]
    text = row["p"]
    assert text.count('"risk"') == 1
    assert json.loads(text) == {"amount": 3, "risk": "const", "region": "eu"}


def test_build_publish_ready_sample_shape(spark, tmp_path):
    cfg = _config(tmp_path)
    evs = [
        {"event_id": "e1", "product": "sales", "schema": "purchase", "tenant_id": A, "data": {"amount": 1}},
        {"event_id": "e2", "product": "sales", "schema": "other", "tenant_id": A, "data": {}},
        {"event_id": "e3", "product": "sales", "schema": "purchase", "tenant_id": "bad", "data": {}},
    ]
    df = spark.createDataFrame([(json.dumps(e),) for e in evs], ["value"])
    ready, rejected, audit = build_publish_ready(df, cfg)
    rows = ready.collect()
    assert ready.columns == ["topic", "key", "value"]
    assert len(rows) == 1 and rows[0]["topic"] == "delivery.file" and rows[0]["key"] == f"{A}|da"
    assert json.loads(rows[0]["value"])["payload"]["region"] == "eu"
    assert rejected.count() == 1
    reasons = sorted(r["reason"] for r in audit.collect())
    assert reasons == ["invalid_tenant_id", "no_matching_rule"]
