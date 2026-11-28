"""Test-side Kafka inspection with kafka-python (never used by the pipeline)."""
from __future__ import annotations

import json

from kafka import KafkaConsumer, TopicPartition


def end_offsets(bootstrap: str, topic: str) -> dict[TopicPartition, int]:
    consumer = KafkaConsumer(bootstrap_servers=bootstrap, enable_auto_commit=False)
    try:
        partitions = [TopicPartition(topic, p) for p in sorted(consumer.partitions_for_topic(topic) or [])]
        assert partitions, f"topic {topic} does not exist; run scripts/create-topics.ps1"
        return consumer.end_offsets(partitions)
    finally:
        consumer.close()


def starting_offsets_json(offsets: dict[TopicPartition, int]) -> str:
    """Spark Kafka source ``startingOffsets`` JSON for the given per-partition offsets."""
    by_topic: dict[str, dict[str, int]] = {}
    for tp, offset in offsets.items():
        by_topic.setdefault(tp.topic, {})[str(tp.partition)] = offset
    return json.dumps(by_topic)


def read_since(bootstrap: str, start: dict[TopicPartition, int]) -> list:
    """All records written after ``start``, up to the current end of each partition."""
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
