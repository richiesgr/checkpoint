"""Unit tests for the parse stage (task 4.1). Validates: Requirements 1.3-1.7, 8.1, 8.8."""
from __future__ import annotations

import json
from pathlib import Path

from event_forwarding.pipeline import parse_events

TENANT = "3F2B8C1E-7A4D-4E9B-9C1A-2D5E6F7A8B90"


def _ev(**over):
    base = {"event_id": "e", "product": "p", "schema": "s", "tenant_id": TENANT, "data": {"a": 1}}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


def _run(spark, lines):
    df = spark.createDataFrame([(ln,) for ln in lines], ["value"])
    valid, rejected = parse_events(df)
    return valid.collect(), rejected.collect()


def test_valid_event_is_normalized_and_data_kept_raw(spark):
    valid, rejected = _run(spark, [json.dumps(_ev())])
    assert rejected == []
    (row,) = valid
    assert row.tenant_id == TENANT.lower()
    assert json.loads(row.data) == {"a": 1}


def test_blank_lines_skipped_silently(spark):
    valid, rejected = _run(spark, ["", "   ", "\t"])
    assert valid == [] and rejected == []


def test_rejection_reasons(spark):
    cases = {
        "{not json": "malformed_json",
        "[1, 2]": "malformed_json",
        '"text"': "malformed_json",
        json.dumps(_ev(event_id=...)): "missing_envelope_field",
        json.dumps(_ev(product=None)): "missing_envelope_field",
        json.dumps(_ev(tenant_id=None)): "missing_envelope_field",
        json.dumps(_ev(data=None)): "missing_envelope_field",
        json.dumps(_ev(data="{}")): "invalid_data",
        json.dumps(_ev(data=[1])): "invalid_data",
        json.dumps(_ev(tenant_id="")): "invalid_tenant_id",
        json.dumps(_ev(tenant_id="not-a-uuid")): "invalid_tenant_id",
        json.dumps(_ev(tenant_id=" " + TENANT)): "invalid_tenant_id",
        json.dumps(_ev(tenant_id=TENANT.replace("-", ""))): "invalid_tenant_id",
    }
    valid, rejected = _run(spark, list(cases))
    assert valid == []
    got = {r.line: r.reason for r in rejected}
    assert got == cases


def test_rejected_rows_carry_source_file_and_no_data(spark, tmp_path: Path):
    f = tmp_path / "in.jsonl"
    f.write_text("{bad\n" + json.dumps(_ev()) + "\n", encoding="utf-8")
    valid, rejected = parse_events(spark.read.text(str(f)))
    (bad,) = rejected.collect()
    assert bad.source_file.endswith("in.jsonl")
    assert "data" not in rejected.columns
    assert valid.count() == 1


def test_sample_events(spark):
    lines = Path("sample/events.jsonl").read_text(encoding="utf-8").splitlines()
    valid, rejected = _run(spark, lines)
    assert sorted(r.event_id for r in valid) == ["e1", "e2", "e3", "e5"]
    assert [(r.event_id, r.reason) for r in rejected] == [("e4", "invalid_tenant_id")]
