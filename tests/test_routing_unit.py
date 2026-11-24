"""Unit tests for routing (task 4.2). Validates: Requirements 3.1-3.7, 2.12."""
from __future__ import annotations

import json

from event_forwarding.pipeline import parse_events, route_events

A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "9a1c2d3e-4b5f-4a6b-8c7d-0e1f2a3b4c5d"


def _rules(spark, rows):
    cols = ["rule_index", "tenant_id", "product", "schema", "enabled",
            "destination_id", "destination_kind", "destination_format"]
    return spark.createDataFrame(rows, cols)


def _events(spark, events):
    lines = [json.dumps({"event_id": e[0], "tenant_id": e[1], "product": e[2], "schema": e[3], "data": {"x": 1}})
             for e in events]
    return parse_events(spark.createDataFrame([(ln,) for ln in lines], ["value"]))


def test_exact_match_fan_out_and_default_deny(spark):
    valid, rejected = _events(spark, [
        ("e1", A, "sales", "purchase"),
        ("e2", A, "Sales", "purchase"),      # case differs: no match
        ("e3", A, "sales ", "purchase"),     # trailing space: no match
        ("e4", B, "sales", "purchase"),      # other tenant, no rule
        ("e5", A.upper(), "sales", "purchase"),  # non-lowercase UUID is normalized
        ("e6", "bad", "sales", "purchase"),
    ])
    rules = _rules(spark, [
        (0, A, "sales", "purchase", True, "d1", "file", "jsonl"),
        (1, A, "sales", "purchase", True, "d2", "file", "jsonl"),
        (2, A, "sales", "purchase", True, "d3", "http", "json"),
        (3, B, "sales", "purchase", False, "d4", "file", "jsonl"),
    ])
    routed, audit = route_events(valid, rules, rejected)
    got = sorted((r.event_id, r.destination_id) for r in routed.collect())
    assert got == [("e1", "d1"), ("e1", "d2"), ("e5", "d1"), ("e5", "d2")]
    entries = sorted((r.event_id, r.reason, r.destination_id) for r in audit.collect())
    assert entries == [
        ("e1", "destination_kind_not_built", "d3"),
        ("e2", "no_matching_rule", None),
        ("e3", "no_matching_rule", None),
        ("e4", "no_matching_rule", None),   # only a disabled rule exists
        ("e5", "destination_kind_not_built", "d3"),
        ("e6", "invalid_tenant_id", None),
    ]
    assert all("data" not in r.asDict() and "payload" not in r.asDict() for r in audit.collect())
