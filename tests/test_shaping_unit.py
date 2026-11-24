"""Unit tests for shape_events (task 4.3). Validates: Requirements 4.1-4.5."""
from __future__ import annotations

import json

from event_forwarding.config_model import parse_config
from event_forwarding.pipeline import shape_events

TENANT = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"


def _config(rules):
    return parse_config(
        {
            "kafka": {"bootstrap_servers": "localhost:9092"},
            "output_root": "output",
            "destinations": [{"id": "d1", "tenant_id": TENANT, "kind": "file", "format": "jsonl"}],
            "rules": [
                {"tenant_id": TENANT, "product": "p", "schema": "s", "destinations": ["d1"], **r}
                for r in rules
            ],
        }
    )


def _shape(spark, config, datas):
    rows = [
        (f"e{i}", "p", "s", TENANT, json.dumps(d), "f.jsonl", i % len(config.rules), "d1", "file", "jsonl")
        for i, d in enumerate(datas)
    ]
    cols = ["event_id", "product", "schema", "tenant_id", "data", "source_file", "rule_index",
            "destination_id", "destination_kind", "destination_format"]
    df = shape_events(spark.createDataFrame(rows, cols), config)
    return {r.event_id: (r, json.loads(r.payload_json)) for r in
            df.selectExpr("*", "to_json(payload) as payload_json").collect()}


def test_include_list_omits_absent_and_keeps_envelope(spark):
    cfg = _config([{"include_fields": ["a", "missing", "n"]}])
    out = _shape(spark, cfg, [{"a": 1, "b": 2, "n": None}])
    row, payload = out["e0"]
    assert payload == {"a": 1, "n": None}
    assert (row.event_id, row.product, row.schema, row.tenant_id) == ("e0", "p", "s", TENANT)
    assert "data" not in row.asDict()


def test_all_fields_when_no_include_list(spark):
    cfg = _config([{}])
    assert _shape(spark, cfg, [{"a": {"x": [1]}, "b": "t"}, {}])["e0"][1] == {"a": {"x": [1]}, "b": "t"}


def test_constants_added_with_types(spark):
    cfg = _config([{"constant_fields": {"region": "eu", "n": 3, "ok": True}},
                   {"include_fields": ["a"], "constant_fields": {"region": "eu", "ok": False}}])
    out = _shape(spark, cfg, [{"a": 1, "risk": 7}, {"a": 1, "risk": 7}, {}, {"b": 1}])
    assert out["e0"][1] == {"a": 1, "risk": 7, "region": "eu", "n": 3, "ok": True}
    assert out["e1"][1] == {"a": 1, "region": "eu", "ok": False}
    assert out["e2"][1] == {"region": "eu", "n": 3, "ok": True}
    assert out["e3"][1] == {"region": "eu", "ok": False}
