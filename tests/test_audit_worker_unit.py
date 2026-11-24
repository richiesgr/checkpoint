"""Smoke tests for the delivery-stage audit writer."""
import json

import pytest

from event_forwarding.audit_worker import (
    AuditWriteError,
    delivered_audit_rows,
    write_delivery_audit,
)

TENANT = "11111111-1111-1111-1111-111111111111"


def _rows(spark):
    return spark.createDataFrame(
        [
            ("d1", TENANT, "p", "s", "e1", '{"a":1}', "delivery.file", 0, 5),
            ("d1", TENANT, "p", "s", "e2", '{"a":2}', "delivery.file", 0, 9),
        ],
        "destination_id string, tenant_id string, product string, schema string, event_id string,"
        " payload string, topic string, partition int, offset long",
    )


def test_delivered_rows_written_with_deterministic_name_and_no_payload(spark, tmp_path):
    df = _rows(spark)
    write_delivery_audit(delivered_audit_rows(df), df, tmp_path)
    write_delivery_audit(delivered_audit_rows(df), df, tmp_path)  # replay overwrites

    files = [p for p in (tmp_path / "delivery").iterdir() if p.is_file() and not p.name.endswith(".crc")]
    assert [p.name for p in files] == ["audit-delivery.file-0-5-9.jsonl"]
    lines = [json.loads(x) for x in files[0].read_text().splitlines()]
    assert {r["event_id"] for r in lines} == {"e1", "e2"}
    assert all(r["decision"] == "delivered" and "payload" not in r for r in lines)
    assert not (tmp_path / "delivery" / ".tmp").exists() or not any((tmp_path / "delivery" / ".tmp").iterdir())


def test_write_failure_raises(spark, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("x")  # a file where the audit root directory should be
    df = _rows(spark)
    with pytest.raises(AuditWriteError):
        write_delivery_audit(delivered_audit_rows(df), df, blocker)
