"""Unit tests for the File_Sink_Worker write step (``process_micro_batch``), no Kafka."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from event_forwarding import file_sink_write
from event_forwarding.config_model import Config, Destination, KafkaSettings, Rule
from event_forwarding.file_sink_write import REASON_WRITE_FAILED, process_micro_batch

A = "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90"
B = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
OWNERS = {"dest-a1": A, "dest-b1": B}


@pytest.fixture
def tmp_path():
    """Short temp root. pytest's own tmp_path is deep enough that the Spark staging path
    (``.tmp/<batch>-<uuid>/file_name=<name>/part-<uuid>.c000.txt``) exceeds Windows MAX_PATH."""
    with tempfile.TemporaryDirectory(prefix="sw") as tmp:
        yield Path(tmp)


def _config(root: Path) -> Config:
    return Config(
        kafka=KafkaSettings(bootstrap_servers="localhost:9092"),
        output_root=str(root / "out"),
        audit_root=str(root / "audit"),
        destinations=tuple(
            Destination(id=d, tenant_id=t, kind="file", format="jsonl", settings={})
            for d, t in OWNERS.items()
        ),
        rules=(Rule(tenant_id=A, product="sales", schema="purchase", destinations=("dest-a1",)),),
    )


def _row(dest: str, idx: int, payload=None):
    tenant = OWNERS[dest]
    value = json.dumps(
        {
            "tenant_id": tenant,
            "destination_id": dest,
            "product": "sales",
            "schema": "purchase",
            "event_id": f"ev-{idx}",
            "payload": payload if payload is not None else {"n": idx},
        },
        ensure_ascii=False,
    )
    return (f"{tenant}|{dest}", value, "delivery.file", 0, idx)


def _files(root: Path, dest: str) -> list[Path]:
    d = root / "out" / OWNERS[dest] / dest
    return sorted(p for p in d.glob("*") if p.is_file()) if d.exists() else []


class _Capture:
    """Capture ``publish_dlq``: collects (key, value, reason) rows as Python tuples."""

    def __init__(self):
        self.rows: list[tuple[bytes, bytes, str]] = []

    def __call__(self, df):
        self.rows.extend((bytes(r["key"]), bytes(r["value"]), r["reason"]) for r in df.collect())


def test_same_batch_twice_gives_one_file_without_duplicates(spark, make_kafka_rows, tmp_path):
    """Req 12.15, 5.6: replaying a batch overwrites the same deterministic file."""
    config = _config(tmp_path)
    rows = [_row("dest-a1", 0), _row("dest-a1", 1), _row("dest-a1", 2)]
    for _ in range(2):
        process_micro_batch(make_kafka_rows(rows), 1, config, publish_dlq=_Capture())

    files = _files(tmp_path, "dest-a1")
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    ids = [json.loads(line)["event_id"] for line in lines]
    assert sorted(ids) == ["ev-0", "ev-1", "ev-2"]
    assert len(set(ids)) == len(ids)


def test_one_file_per_batch_not_per_message(spark, make_kafka_rows, tmp_path):
    """Req 5.2, 9.3: many messages for one Destination and partition share one file."""
    config = _config(tmp_path)
    rows = [_row("dest-a1", i) for i in range(5)] + [_row("dest-b1", i) for i in range(5, 8)]
    capture = _Capture()
    process_micro_batch(make_kafka_rows(rows), 7, config, publish_dlq=capture)

    assert capture.rows == []
    a_files, b_files = _files(tmp_path, "dest-a1"), _files(tmp_path, "dest-b1")
    assert len(a_files) == 1 and len(b_files) == 1
    assert a_files[0].name == "dest-a1-delivery.file-0-0-4.jsonl"
    assert b_files[0].name == "dest-b1-delivery.file-0-5-7.jsonl"
    assert len(a_files[0].read_text(encoding="utf-8").splitlines()) == 5
    assert len(b_files[0].read_text(encoding="utf-8").splitlines()) == 3


def test_line_format_utf8_and_newline_separators(spark, make_kafka_rows, tmp_path):
    """Req 5.3, 5.11: each line is envelope fields plus payload, UTF-8, separated by ``\\n``."""
    config = _config(tmp_path)
    payload = {"name": "Zoë – 日本語", "nested": {"k": [1, 2]}, "amount": 12.5}
    process_micro_batch(
        make_kafka_rows([_row("dest-a1", 0, payload), _row("dest-a1", 1)]),
        1,
        config,
        publish_dlq=_Capture(),
    )

    (path,) = _files(tmp_path, "dest-a1")
    raw = path.read_bytes()
    text = raw.decode("utf-8")  # raises if not valid UTF-8
    assert "\r" not in text
    assert text.endswith("\n")
    lines = text[:-1].split("\n")
    assert len(lines) == 2
    by_id = {json.loads(line)["event_id"]: json.loads(line) for line in lines}
    first = by_id["ev-0"]
    assert set(first) == {"event_id", "product", "schema", "tenant_id", "payload"}
    assert first["tenant_id"] == A
    assert first["product"] == "sales" and first["schema"] == "purchase"
    assert first["payload"] == payload
    assert "Zoë – 日本語" in text  # not escaped away; written as UTF-8


def test_failed_destination_is_dead_lettered_others_still_written(
    spark, make_kafka_rows, tmp_path, monkeypatch
):
    """Req 12.18, 5.6: only the failing Destination's rows reach the DLQ, unchanged."""
    config = _config(tmp_path)
    real = file_sink_write._write_destination
    failing_dir = tmp_path / "out" / B / "dest-b1"

    def flaky(dest_df, final_dir, tmp_root):
        if Path(final_dir) == failing_dir:
            raise OSError("disk full")
        real(dest_df, final_dir, tmp_root)

    monkeypatch.setattr(file_sink_write, "_write_destination", flaky)

    rows = [_row("dest-a1", 0), _row("dest-a1", 1), _row("dest-b1", 2), _row("dest-b1", 3)]
    capture = _Capture()
    process_micro_batch(make_kafka_rows(rows), 1, config, publish_dlq=capture)

    # Other Destination written.
    (a_file,) = _files(tmp_path, "dest-a1")
    assert len(a_file.read_text(encoding="utf-8").splitlines()) == 2
    # Failed Destination: no final file, no leftover staging dir.
    assert _files(tmp_path, "dest-b1") == []
    tmp_dir = failing_dir / ".tmp"
    assert not tmp_dir.exists() or not any(tmp_dir.iterdir())
    # DLQ holds exactly the failed Destination's rows with the original key and value.
    expected = {(r[0].encode(), r[1].encode(), REASON_WRITE_FAILED) for r in rows[2:]}
    assert len(capture.rows) == 2
    assert set(capture.rows) == expected


def test_dlq_publish_failure_fails_the_batch(spark, make_kafka_rows, tmp_path):
    """The batch must raise when the DLQ publish fails so the checkpoint does not advance."""
    config = _config(tmp_path)

    def boom(_df):
        raise RuntimeError("kafka down")

    with pytest.raises(file_sink_write.DlqPublishError):
        process_micro_batch(make_kafka_rows([_row("dest-a1", 0)]), 1, config, publish_dlq=boom)
