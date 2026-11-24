"""Smoke tests for sink_transform (no Kafka)."""
from __future__ import annotations

import json

from event_forwarding.sink_transform import sink_transform

A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _owners(spark):
    return spark.createDataFrame(
        [
            ("dest-a", A, "out/a", "file", "jsonl"),
            ("dest-b", B, "out/b", "file", "jsonl"),
            ("dest-p", A, "out/p", "file", "parquet"),
        ],
        "destination_id string, tenant_id string, output_path string, kind string, format string",
    )


def _msg(tenant, dest, event_id="e", payload=None):
    return json.dumps(
        {
            "tenant_id": tenant,
            "destination_id": dest,
            "product": "sales",
            "schema": "purchase",
            "event_id": event_id,
            "payload": payload if payload is not None else {"x": 1},
        }
    )


def test_sink_transform_splits_rows(spark, make_kafka_rows):
    rows = [
        (f"{A}|dest-a", _msg(A, "dest-a", "ok1"), "delivery.file", 0, 5),
        (f"{A}|dest-a", _msg(A, "dest-a", "ok2"), "delivery.file", 0, 7),
        ("not-json-key", "{broken", "delivery.file", 0, 8),
        (f"{A}|dest-a", json.dumps({"tenant_id": A}), "delivery.file", 0, 9),
        (f"{B}|dest-a", _msg(A, "dest-a", "badkey"), "delivery.file", 0, 10),
        (f"{A}|dest-b", _msg(A, "dest-b", "wrongowner"), "delivery.file", 0, 11),
        (f"{A}|nope", _msg(A, "nope", "unknown"), "delivery.file", 0, 12),
        (f"{A}|dest-p", _msg(A, "dest-p", "pq"), "delivery.file", 0, 13),
    ]
    deliverable, dlq, audit = sink_transform(make_kafka_rows(rows), _owners(spark))

    d = {r["event_id"]: r for r in deliverable.collect()}
    assert set(d) == {"ok1", "ok2"}
    assert d["ok1"]["file_name"] == "dest-a-delivery.file-0-5-7.jsonl"
    assert json.loads(d["ok1"]["payload"]) == {"x": 1}

    reasons = sorted(r["reason"] for r in dlq.collect())
    assert reasons == ["isolation_violation"] * 3 + ["malformed_message"] * 2
    assert dlq.columns == ["key", "value", "reason"]
    tampered = [r for r in dlq.collect() if r["key"] == f"{B}|dest-a".encode()]
    assert tampered[0]["value"] == _msg(A, "dest-a", "badkey").encode()

    a = {(r["event_id"], r["decision"], r["reason"]) for r in audit.collect()}
    assert ("pq", "dropped", "format_not_built") in a
    assert ("badkey", "dead-lettered", "isolation_violation") in a
    assert audit.count() == 6
