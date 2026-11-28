"""End-to-end test through the real broker (task 13.2).

generator -> landing zone (5 files) -> run_processing -> delivery.file
-> run_worker (sink_transform, file write) -> out/<tenant_id>/<destination_id>/

The topics are the fixed shared ones. The test records their end offsets before the run, reads
only what was written after them, and starts the worker at those offsets, so messages from
earlier runs are never read. The run directory is kept for inspection (see settings.py).

Validates: Requirements 3.1, 3.2, 4.1, 4.4, 5.1, 8.5, 8.9, 8.11, 15.3, 15.11.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

import pytest

from event_forwarding.cli_file_sink_worker import run_worker
from event_forwarding.config_model import load_config
from event_forwarding.log_util import read_total_counters
from event_forwarding.pipeline import run_processing
from tests.end_to_end import settings as s
from tests.end_to_end.generator import generate
from tests.end_to_end.kafka_helpers import end_offsets, read_since, starting_offsets_json

pytestmark = pytest.mark.kafka
log = logging.getLogger("tests.end_to_end")


def _files(directory: Path, pattern: str) -> list[Path]:
    """Files only (Spark staging directories are named ``file_name=....jsonl``)."""
    return sorted(p for p in directory.rglob(pattern) if p.is_file())


def _json_lines(directory: Path, pattern: str) -> list[dict]:
    return [
        json.loads(line)
        for p in _files(directory, pattern)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _canon(lines: list[dict]) -> list[str]:
    return sorted(json.dumps(line, sort_keys=True) for line in lines)


def test_end_to_end_through_kafka(kafka_spark, kafka_broker):
    run = generate()
    paths = run.paths
    log.info("end-to-end run %s in %s", run.run_id, run.run_dir)
    expected = json.loads(paths["expected"].read_text(encoding="utf-8"))
    tenant_of = {d["id"]: d["tenant_id"] for d in run.config["destinations"]}

    config = load_config(paths["config"])  # the generated config passes validation
    delivery_start = end_offsets(kafka_broker, s.DELIVERY_TOPIC)
    dlq_start = end_offsets(kafka_broker, s.DLQ_TOPIC)

    # --- processing job
    result = run_processing(kafka_spark, config, paths["landing"], paths["ckpt_processing"])
    assert result.isolation_violations == 0
    assert result.unreadable_files == ()

    published = read_since(kafka_broker, delivery_start)
    assert len(published) == expected["delivered_records"]
    assert {r.topic for r in published} == {s.DELIVERY_TOPIC}
    assert Counter(r.key.decode() for r in published) == Counter(
        {f"{tenant_of[d]}|{d}": n for d, n in expected["records_per_destination"].items()}
    )

    totals = read_total_counters(paths["audit"] / "run_summary")
    assert totals["read"] == expected["lines"]
    assert totals["rejected"] == s.BAD_LINES
    assert totals["published"] == expected["delivered_records"]

    # Unmatched events are dropped with no_matching_rule, every one of them.
    audit = _json_lines(paths["audit"] / "processing", "part-*.json")
    no_rule = [a["event_id"] for a in audit if a["reason"] == "no_matching_rule"]
    assert Counter(no_rule) == Counter(run.unmatched_ids)

    # Every bad line is in the Rejected_Output with its reason, and nowhere else.
    rejected = _json_lines(paths["rejected"], "part-*.json")
    assert Counter((r["line"], r["reason"]) for r in rejected) == Counter(run.expected_rejected)

    # --- File_Sink_Worker, from this run's offsets only
    violations = run_worker(
        kafka_spark, config, paths["ckpt_sink"], starting_offsets=starting_offsets_json(delivery_start)
    )
    assert violations == 0

    out = paths["out"]
    written_dirs = {p.parent.relative_to(out).parts for p in _files(out, "*.jsonl")}
    assert written_dirs == {(tenant_of[d], d) for d in run.expected_files}
    leftovers = [p for p in out.rglob("*") if ".tmp" in p.relative_to(out).parts and p.is_file()]
    assert leftovers == [], "staging files left in customer directories"
    for dest_id, expected_lines in run.expected_files.items():
        got = _json_lines(out / tenant_of[dest_id] / dest_id, "*.jsonl")
        assert _canon(got) == _canon(expected_lines), f"content of {dest_id}"
        assert all(line["tenant_id"] == tenant_of[dest_id] for line in got)
    for dest_id in expected["never_written_destinations"]:
        assert _files(out / tenant_of[dest_id] / dest_id, "*.jsonl") == [], f"{dest_id} must not be written"

    everything = "\n".join(p.read_text(encoding="utf-8") for p in _files(out, "*.jsonl"))
    assert "internal_note" not in "".join(
        json.dumps(l) for d in ("dest-t0-file", "dest-t1-file", "dest-t2-file", "dest-t3-file")
        for l in _json_lines(out / tenant_of[d] / d, "*.jsonl")
    )
    assert not any(event_id in everything for event_id in run.unmatched_ids)
    assert f"{run.run_id}-bad-" not in everything

    worker_totals = read_total_counters(paths["audit"] / "worker_summary")
    assert worker_totals == {"delivered": expected["delivered_records"], "dead_lettered": 0}
    assert read_since(kafka_broker, dlq_start) == []
    log.info("end-to-end run %s passed: %s", run.run_id, json.dumps(expected["records_per_destination"]))
