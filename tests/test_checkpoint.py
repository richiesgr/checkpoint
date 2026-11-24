"""Checkpoint restart and incremental processing (no Kafka).

``run_processing`` runs twice over one checkpoint with a local capture ``batch_sink`` (a Spark write
to a temp directory). Validates: Requirements 1.10, 5.13, 12.13, 12.14.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from event_forwarding.config_model import load_config
from event_forwarding.pipeline import run_processing

ROOT = Path(__file__).resolve().parent.parent
A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "9d4e1a72-3c5b-4f8e-a1d2-6b7c8e9f0a13"

# One new tenant B purchase event, routed to dest-b-file by sample/config.yaml.
NEW_EVENT = {
    "event_id": "e6",
    "product": "sales",
    "schema": "purchase",
    "tenant_id": B,
    "data": {"order_id": "o9", "amount": 42},
}


@pytest.fixture
def work_dir():
    """Short temp root (Spark paths exceed Windows MAX_PATH under pytest's tmp_path)."""
    with tempfile.TemporaryDirectory(prefix="ckp") as tmp:
        yield Path(tmp)


@pytest.fixture
def setup(work_dir):
    landing = work_dir / "landing"
    landing.mkdir()
    shutil.copy(ROOT / "sample" / "events.jsonl", landing / "events-1.jsonl")
    config = load_config(ROOT / "sample" / "config.yaml")
    config = dataclasses.replace(
        config,
        output_root=str(work_dir / "out"),
        audit_root=str(work_dir / "audit"),
        rejected_root=str(work_dir / "rejected"),
    )
    return config, landing, work_dir / "checkpoint", work_dir / "captured"


def _capture_sink(captured: Path):
    def sink(df):
        df.select("topic", "key", "value").write.mode("append").json(str(captured.as_posix()))

    return sink


def _captured_event_ids(spark, captured: Path) -> list[str]:
    if not captured.exists():
        return []
    rows = spark.read.schema("topic string, key string, value string").json(captured.as_posix())
    return sorted(json.loads(r["value"])["event_id"] for r in rows.collect())


def test_second_run_with_same_checkpoint_processes_nothing(spark, setup):
    config, landing, checkpoint, captured = setup
    sink = _capture_sink(captured)

    first = run_processing(spark, config, landing, checkpoint, batch_sink=sink)
    assert first.batches >= 1
    assert _captured_event_ids(spark, captured) == ["e1", "e2", "e3"]

    second = run_processing(spark, config, landing, checkpoint, batch_sink=sink)
    assert second.batches == 0
    assert second.exit_code == 0
    assert _captured_event_ids(spark, captured) == ["e1", "e2", "e3"]


def test_new_file_between_runs_processes_only_its_events(spark, setup):
    config, landing, checkpoint, captured = setup
    sink = _capture_sink(captured)

    run_processing(spark, config, landing, checkpoint, batch_sink=sink)
    before = _captured_event_ids(spark, captured)
    assert before == ["e1", "e2", "e3"]

    (landing / "events-2.jsonl").write_text(json.dumps(NEW_EVENT) + "\n", encoding="utf-8")
    second = run_processing(spark, config, landing, checkpoint, batch_sink=sink)

    assert second.batches >= 1
    # Only the new event was added; e1 to e3 were not published again.
    assert _captured_event_ids(spark, captured) == ["e1", "e2", "e3", "e6"]
