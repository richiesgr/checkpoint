"""No-Docker acceptance scenario: sample events and config through the whole pipeline.

build_publish_ready -> Kafka_Shaped_Rows -> sink_transform and process_micro_batch (temp output_root).
Validates: Requirements 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.11, 8.14.
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
from collections import Counter
from pathlib import Path

import pytest
from pyspark.sql import functions as F

from event_forwarding.config_model import build_destination_owner_df, load_config
from event_forwarding.file_sink_write import process_micro_batch
from event_forwarding.pipeline import build_publish_ready
from event_forwarding.sink_transform import sink_transform

ROOT = Path(__file__).resolve().parent.parent
A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "9d4e1a72-3c5b-4f8e-a1d2-6b7c8e9f0a13"


@pytest.fixture
def work_dir():
    """Short temp root (Spark staging paths exceed Windows MAX_PATH under pytest's tmp_path)."""
    with tempfile.TemporaryDirectory(prefix="acc") as tmp:
        yield Path(tmp)


class _Capture:
    def __init__(self):
        self.rows = []

    def __call__(self, df):
        self.rows.extend(r.asDict() for r in df.collect())


def _config(work_dir: Path):
    config = load_config(ROOT / "sample" / "config.yaml")
    return dataclasses.replace(
        config, output_root=str(work_dir / "out"), audit_root=str(work_dir / "audit")
    )


def _text_df(spark, lines):
    return spark.createDataFrame([(line,) for line in lines], ["value"])


def _kafka_shaped(publish_ready):
    """Add partition, offset, timestamp; cast key and value to binary."""
    return publish_ready.select(
        F.col("key").cast("binary").alias("key"),
        F.col("value").cast("binary").alias("value"),
        "topic",
        F.lit(0).alias("partition"),
        (F.monotonically_increasing_id()).alias("offset"),
        F.current_timestamp().alias("timestamp"),
    )


def _read_output(out_root: Path):
    """Return {relative dir parts: [parsed lines]} for every .jsonl file under out_root."""
    found = {}
    if not out_root.exists():
        return found
    for p in out_root.rglob("*.jsonl"):
        rel = p.relative_to(out_root).parts
        found.setdefault(rel[:-1], []).extend(
            json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line
        )
    return found


def test_sample_end_to_end(spark, work_dir):
    config = _config(work_dir)
    lines = [
        l for l in (ROOT / "sample" / "events.jsonl").read_text(encoding="utf-8").splitlines() if l
    ]
    publish_ready, _rejected, _audit = build_publish_ready(_text_df(spark, lines), config)

    # --- processing job output (10.3, 10.4)
    rows = publish_ready.collect()
    assert [r["topic"] for r in rows] == ["delivery.file"] * 3
    assert Counter(r["key"] for r in rows) == Counter(
        {f"{A}|dest-a-file": 2, f"{B}|dest-b-file": 1}
    )
    event_ids = [json.loads(r["value"])["event_id"] for r in rows]
    assert sorted(event_ids) == ["e1", "e2", "e3"]  # nothing for e4, e5

    # --- sink_transform on Kafka_Shaped_Rows (8.14)
    shaped = _kafka_shaped(publish_ready).cache()
    owners = build_destination_owner_df(spark, config)
    deliverable, dlq, _audit2 = sink_transform(shaped, owners)
    assert sorted(r["event_id"] for r in deliverable.collect()) == ["e1", "e2", "e3"]
    assert dlq.count() == 0

    # --- write step
    capture = _Capture()
    process_micro_batch(shaped, 1, config, publish_dlq=capture)
    assert capture.rows == []  # DLQ output holds no e4 or e5 (or anything else)

    out = _read_output(work_dir / "out")
    assert set(out) == {(A, "dest-a-file"), (B, "dest-b-file")}  # no cross-tenant files
    a_lines, b_lines = out[(A, "dest-a-file")], out[(B, "dest-b-file")]
    assert Counter(l["event_id"] for l in a_lines) == Counter({"e1": 1, "e2": 1})
    assert [l["event_id"] for l in b_lines] == ["e3"]
    assert all(l["tenant_id"] == A for l in a_lines)
    assert all(l["tenant_id"] == B for l in b_lines)

    e1 = next(l for l in a_lines if l["event_id"] == "e1")
    assert "internal_note" not in e1["payload"]
    assert e1["payload"] == {"order_id": "o1", "amount": 10}

    e2 = next(l for l in a_lines if l["event_id"] == "e2")
    assert e2["payload"]["region"] == "eu"
    assert e2["payload"]["risk"] == 7
    assert e2["payload"]["alert_id"] == "a1"

    everything = json.dumps([a_lines, b_lines])
    assert '"e4"' not in everything and '"e5"' not in everything


def test_event_without_enabled_rule_publishes_and_writes_nothing(spark, work_dir):
    config = _config(work_dir)
    lines = [
        json.dumps(
            {
                "event_id": "e5",
                "product": "identity",
                "schema": "login",
                "tenant_id": A,
                "data": {"user": "u1", "result": "success"},
            }
        ),
        json.dumps(
            {
                "event_id": "e4",
                "product": "sales",
                "schema": "purchase",
                "tenant_id": "",
                "data": {"order_id": "o3", "amount": 3},
            }
        ),
    ]
    publish_ready, _rejected, _audit = build_publish_ready(_text_df(spark, lines), config)
    assert publish_ready.count() == 0

    capture = _Capture()
    process_micro_batch(_kafka_shaped(publish_ready), 1, config, publish_dlq=capture)
    assert capture.rows == []
    assert _read_output(work_dir / "out") == {}
