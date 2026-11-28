"""Kafka acceptance test: sample events through the real broker (task 12.1).

Landing zone -> run_processing -> delivery.file -> run_worker (sink_transform, file write) -> files.

The topics are the fixed shared ones (delivery.file, delivery.file.dlq), so they keep messages
from earlier runs and the worker reads from `earliest`. Each run therefore gives the sample events
a run-unique event_id suffix and asserts only on its own events. Checkpoints and output
directories are unique per run.

Validates: Requirements 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.10, 12.3, 12.4.
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import uuid
from collections import Counter
from pathlib import Path

import pytest
from kafka import KafkaConsumer, TopicPartition

from event_forwarding.cli_file_sink_worker import run_worker
from event_forwarding.config_model import load_config
from event_forwarding.file_sink_write import DLQ_TOPIC
from event_forwarding.pipeline import run_processing

pytestmark = pytest.mark.kafka

ROOT = Path(__file__).resolve().parent.parent
A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "9d4e1a72-3c5b-4f8e-a1d2-6b7c8e9f0a13"
DELIVERY_TOPIC = "delivery.file"


@pytest.fixture
def work_dir():
    """Short temp root (Spark staging paths exceed Windows MAX_PATH under pytest's tmp_path)."""
    with tempfile.TemporaryDirectory(prefix="acck") as tmp:
        yield Path(tmp)


def _end_offsets(bootstrap: str, topic: str) -> dict[TopicPartition, int]:
    consumer = KafkaConsumer(bootstrap_servers=bootstrap, enable_auto_commit=False)
    try:
        partitions = [TopicPartition(topic, p) for p in sorted(consumer.partitions_for_topic(topic) or [])]
        assert partitions, f"topic {topic} does not exist; run scripts/create-topics.ps1"
        return consumer.end_offsets(partitions)
    finally:
        consumer.close()


def _read_since(bootstrap: str, start: dict[TopicPartition, int]) -> list:
    """All messages written after the recorded offsets, up to the current end."""
    consumer = KafkaConsumer(bootstrap_servers=bootstrap, enable_auto_commit=False)
    try:
        partitions = list(start)
        consumer.assign(partitions)
        end = consumer.end_offsets(partitions)
        for tp, offset in start.items():
            consumer.seek(tp, offset)
        records = []
        while any(consumer.position(tp) < end[tp] for tp in partitions):
            for batch in consumer.poll(timeout_ms=2000).values():
                records.extend(batch)
        return records
    finally:
        consumer.close()


def _write_landing(landing: Path, suffix: str) -> dict[str, str]:
    """Copy sample/events.jsonl with run-unique event ids. Returns {sample id: run id}."""
    landing.mkdir(parents=True)
    ids, lines = {}, []
    for line in (ROOT / "sample" / "events.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        ids[event["event_id"]] = f"{event['event_id']}-{suffix}"
        event["event_id"] = ids[event["event_id"]]
        lines.append(json.dumps(event))
    (landing / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ids


def _read_output(out_root: Path) -> dict[tuple, list[dict]]:
    found: dict[tuple, list[dict]] = {}
    for p in out_root.rglob("*.jsonl"):
        rel = p.relative_to(out_root).parts
        found.setdefault(rel[:-1], []).extend(
            json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line
        )
    return found


def test_acceptance_through_kafka(kafka_spark, kafka_broker, work_dir):
    suffix = uuid.uuid4().hex[:12]
    ids = _write_landing(work_dir / "landing", suffix)
    run_ids = set(ids.values())

    config = load_config(ROOT / "sample" / "config.yaml")
    config = dataclasses.replace(
        config,
        kafka=dataclasses.replace(config.kafka, bootstrap_servers=kafka_broker),
        output_root=str(work_dir / "out"),
        audit_root=str(work_dir / "audit"),
        rejected_root=str(work_dir / "rejected"),
    )

    delivery_start = _end_offsets(kafka_broker, DELIVERY_TOPIC)
    dlq_start = _end_offsets(kafka_broker, DLQ_TOPIC)

    # --- processing job publishes to delivery.file (10.3, 10.4)
    result = run_processing(kafka_spark, config, work_dir / "landing", work_dir / "ckpt-processing")
    assert result.isolation_violations == 0
    assert result.unreadable_files == ()

    published = [
        (r.key.decode("utf-8"), json.loads(r.value.decode("utf-8")))
        for r in _read_since(kafka_broker, delivery_start)
    ]
    mine = [(key, value) for key, value in published if value["event_id"] in run_ids]
    assert sorted(v["event_id"] for _, v in mine) == sorted([ids["e1"], ids["e2"], ids["e3"]])
    assert Counter(key for key, _ in mine) == Counter({f"{A}|dest-a-file": 2, f"{B}|dest-b-file": 1})

    # --- File_Sink_Worker drains delivery.file and writes files (10.5 to 10.8)
    run_worker(kafka_spark, config, work_dir / "ckpt-sink")

    out = _read_output(work_dir / "out")
    # Every written directory belongs to its configured owner (old messages included).
    assert set(out) <= {(A, "dest-a-file"), (B, "dest-b-file")}
    for (tenant, _dest), lines in out.items():
        assert all(line["tenant_id"] == tenant for line in lines)

    a_lines = [l for l in out.get((A, "dest-a-file"), []) if l["event_id"] in run_ids]
    b_lines = [l for l in out.get((B, "dest-b-file"), []) if l["event_id"] in run_ids]
    assert Counter(l["event_id"] for l in a_lines) == Counter({ids["e1"]: 1, ids["e2"]: 1})
    assert [l["event_id"] for l in b_lines] == [ids["e3"]]

    e1 = next(l for l in a_lines if l["event_id"] == ids["e1"])
    assert e1["payload"] == {"order_id": "o1", "amount": 10}  # internal_note not forwarded

    e2 = next(l for l in a_lines if l["event_id"] == ids["e2"])
    assert e2["payload"]["region"] == "eu"
    assert e2["payload"]["risk"] == 7
    assert e2["payload"]["alert_id"] == "a1"

    e3 = b_lines[0]
    assert e3["payload"] == {"order_id": "o2", "amount": 5}

    # --- e4 and e5 never reach a customer, and nothing of this run is dead-lettered (10.9)
    everything = json.dumps(list(out.values()))
    assert ids["e4"] not in everything and ids["e5"] not in everything
    dlq_mine = [r for r in _read_since(kafka_broker, dlq_start) if suffix.encode() in (r.value or b"")]
    assert dlq_mine == []
